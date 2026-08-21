
"""
Transparent push proxy.

Mirrors the push server's /v2/tokens/<account> API on the main web server
under /webrtcgateway/push and relays requests to the push server named by
sylk_push_url: sending a push, and adding or removing a device token. The
request body and method are forwarded unmodified, the caller is acknowledged
before the forward completes, and the push server's response is logged rather
than relayed.

PushProxy.decide is the extension point for dropping or rewriting a request.
"""

import hmac
import ipaddress
import json

from itertools import chain
from urllib.parse import quote, urlsplit

from application.configuration.datatypes import NetworkRange
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from sipsimple.threading import run_in_twisted_thread
from twisted.internet import defer, reactor
from twisted.web.client import Agent, readBody
from twisted.web.http_headers import Headers
from zope.interface import implementer

from sylk.configuration import ThorNodeConfig

from .configuration import GeneralConfig
from .logger import log
from .push import BytesProducer

__all__ = 'PushProxy', 'PushProxyContext', 'PushDecision'


MAX_REQUEST_BODY = 64 * 1024
MAX_LOGGED_ERROR_BODY = 200


class PushProxyContext(object):
    """One inbound request. action is 'push', 'add' or 'remove'."""

    __slots__ = ('action', 'account', 'device_id', 'body', 'payload', 'source_ip')

    def __init__(self, action, account, device_id, body, payload, source_ip):
        self.action = action
        self.account = account
        self.device_id = device_id
        self.body = body          # raw bytes, forwarded as received
        self.payload = payload    # parsed body, for inspection only
        self.source_ip = source_ip

    @property
    def method(self):
        return b'DELETE' if self.action == 'remove' else b'POST'

    @property
    def description(self):
        if self.action == 'push':
            return '%s push %s (%s) for %s' % (self.event, self.call_id, self.media_type, self.account)
        return '%s token request for %s (app=%s device=%s)' % (
            self.action, self.account, self.payload.get('app-id'), self.payload.get('device-id'))

    @property
    def account_key(self):
        """Normalized account, for lookups. account keeps the spelling received."""
        return self.account.strip().lower()

    @property
    def event(self):
        return self.payload.get('event')

    @property
    def call_id(self):
        return self.payload.get('call-id')

    @property
    def media_type(self):
        return self.payload.get('media-type')

    @property
    def originator(self):
        return self.payload.get('from')

    def __repr__(self):
        return '<PushProxyContext {0.action} {0.description}>'.format(self)


class PushDecision(object):
    """Outcome of PushProxy.decide.

    forward -- False drops the request; the caller still gets a 2xx.
    reason  -- logged.
    body    -- replacement body; None forwards the original.
    """

    __slots__ = ('forward', 'reason', 'body')

    def __init__(self, forward=True, reason=None, body=None):
        self.forward = forward
        self.reason = reason
        self.body = body


@implementer(IObserver)
class PushProxy(object, metaclass=Singleton):

    def __init__(self):
        self._agent = None
        self._thor_nodes = []
        self._started = False

    def start(self):
        if not self._started:
            NotificationCenter().add_observer(self, name='ThorNetworkGotUpdate')
            self._started = True

    def stop(self):
        if self._started:
            NotificationCenter().remove_observer(self, name='ThorNetworkGotUpdate')
            self._started = False
        self._thor_nodes = []

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ThorNetworkGotUpdate(self, notification):
        self._thor_nodes = [NetworkRange(node.decode() if isinstance(node, bytes) else node)
                            for node in chain.from_iterable(n.nodes for n in list(notification.data.networks.values()))]

    # -- configuration --------------------------------------------------

    @property
    def enabled(self):
        return bool(GeneralConfig.sylk_push_proxy)

    @property
    def base_url(self):
        """scheme://host:port of sylk_push_url; its path is not used."""
        if not GeneralConfig.sylk_push_url:
            return None
        parts = urlsplit(GeneralConfig.sylk_push_url)
        if not parts.scheme or not parts.netloc:
            return None
        return '%s://%s' % (parts.scheme, parts.netloc)

    @property
    def include_thor(self):
        return getattr(GeneralConfig.sylk_push_proxy_allowed_ips, 'include_thor', False)

    @property
    def configured_peers(self):
        return list(GeneralConfig.sylk_push_proxy_allowed_ips or [])

    @property
    def trusted_parties(self):
        peers = self.configured_peers
        if ThorNodeConfig.enabled and self.include_thor:
            return list(self._thor_nodes) + peers
        return peers

    @property
    def acl_configured(self):
        # thor_network counts even with an empty node list: the ranges it
        # stands for arrive at runtime.
        return bool(self.configured_peers) or self.include_thor

    def configuration_error(self):
        """None when the proxy is safe to serve, otherwise why it is not."""
        if not self.base_url:
            return 'sylk_push_url is not set to a usable scheme://host[:port] URL'
        if not GeneralConfig.sylk_push_proxy_secret and not self.acl_configured:
            return ('neither sylk_push_proxy_secret nor sylk_push_proxy_allowed_ips is set; '
                    'refusing to run an unauthenticated push proxy on the public web server')
        if (self.include_thor and not ThorNodeConfig.enabled
                and not GeneralConfig.sylk_push_proxy_secret and not self.configured_peers):
            return ('sylk_push_proxy_allowed_ips is set to thor_network but SIPThor is '
                    'not enabled ([ThorNetwork] enabled in config.ini), so no request '
                    'can ever be authorized')
        return None

    # -- authentication -------------------------------------------------

    @staticmethod
    def client_ip(request):
        try:
            return request.getClientAddress().host
        except AttributeError:
            return request.getClientIP()

    @staticmethod
    def _address_as_long(ip_string):
        """IPv4 address as the 32 bit int NetworkRange compares against.

        IPv4-mapped IPv6 is unwrapped; anything else yields None.
        """
        try:
            address = ipaddress.ip_address(ip_string)
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped is None:
                return None
            address = address.ipv4_mapped
        return int(address)

    def authorize_source(self, request):
        """Match the source address against the ACL. Returns (allowed, reason)."""
        if not self.acl_configured:
            return True, None
        ip_string = self.client_ip(request)
        address = self._address_as_long(ip_string)
        if address is None:
            return False, 'source address %s is not an IPv4 address (ACL entries are IPv4)' % ip_string
        for base_address, network_mask in self.trusted_parties:
            if address & network_mask == base_address:
                return True, None
        if ThorNodeConfig.enabled and self.include_thor:
            return False, ('source IP %s not in any of %d thor nodes or %d configured ranges' %
                           (ip_string, len(self._thor_nodes), len(self.configured_peers)))
        if self.include_thor:
            return False, ('source IP %s not in any of %d configured ranges (thor_network '
                           'ignored: SIPThor is not enabled)' % (ip_string, len(self.configured_peers)))
        return False, ('source IP %s not in any of %d configured ranges' %
                       (ip_string, len(self.configured_peers)))

    def authorize(self, request, path_secret=None):
        """Shared secret and source address check. Returns (allowed, reason).

        The secret may be presented in an X-Sylk-Push-Auth header, an
        Authorization header (bare or 'Bearer <secret>'), or as path_secret
        taken from a /push/auth/<secret>/... URL. When a secret and an address
        ACL are both configured, both must match.
        """
        secret = GeneralConfig.sylk_push_proxy_secret
        if path_secret is not None and not secret:
            return False, 'secret presented in the URL but no sylk_push_proxy_secret is configured'
        if secret:
            presented = path_secret
            if presented is None:
                for name in ('X-Sylk-Push-Auth', 'Authorization'):
                    values = request.requestHeaders.getRawHeaders(name, default=None)
                    if values:
                        presented = values[0]
                        break
            if presented is None:
                return False, 'no shared secret presented'
            if presented.startswith('Bearer '):
                presented = presented[7:]
            if not hmac.compare_digest(presented, secret):
                return False, 'shared secret does not match'
        return self.authorize_source(request)

    # -- extension point ------------------------------------------------

    def decide(self, context):
        """Decide what to do with an inbound push.

        The default forwards the request unchanged. Runs in the reactor thread
        while the caller waits, so it must be fast and side-effect free; an
        exception is logged and the request forwarded unchanged.
        """
        return PushDecision(forward=True)

    # -- request handling -----------------------------------------------

    def handle(self, request, account, device_id=None, path_secret=None, token_request=False):
        """Serve one inbound request. Returns (status_code, response_dict).

        token_request selects the /v2/tokens/<account> endpoint (add on POST,
        remove on DELETE) instead of the push endpoint. The forward runs in the
        background; the caller is answered immediately.
        """
        if not self.enabled:
            return 404, {'success': False, 'error': 'push proxy is not enabled'}

        error = self.configuration_error()
        if error is not None:
            log.error('push proxy: %s' % error)
            return 503, {'success': False, 'error': 'push proxy is not configured'}

        allowed, denial_reason = self.authorize(request, path_secret)
        if not allowed:
            log.warning('push proxy: rejected request from %s for %s: %s' %
                        (self.client_ip(request), account, denial_reason))
            return 403, {'success': False, 'error': 'not authorized'}

        body = request.content.read() if request.content else b''
        if len(body) > MAX_REQUEST_BODY:
            log.warning('push proxy: rejected oversized request (%d bytes) from %s' %
                        (len(body), self.client_ip(request)))
            return 413, {'success': False, 'error': 'request body too large'}

        try:
            payload = json.loads(body.decode('utf-8')) if body else {}
        except (UnicodeDecodeError, ValueError):
            log.warning('push proxy: rejected non-JSON request from %s for %s' %
                        (self.client_ip(request), account))
            return 400, {'success': False, 'error': 'body is not valid JSON'}
        if not isinstance(payload, dict):
            return 400, {'success': False, 'error': 'body is not a JSON object'}

        if token_request:
            method = request.method.decode() if isinstance(request.method, bytes) else request.method
            action = 'remove' if method.upper() == 'DELETE' else 'add'
        else:
            action = 'push'

        context = PushProxyContext(action=action, account=account, device_id=device_id,
                                   body=body, payload=payload,
                                   source_ip=self.client_ip(request))

        try:
            decision = self.decide(context)
        except Exception:
            log.exception('push proxy: decision hook failed, forwarding unchanged')
            decision = PushDecision(forward=True)

        if not decision.forward:
            log.info('push proxy: dropped %s: %s' %
                     (context.description, decision.reason or 'no reason given'))
            return 202, {'success': True, 'queued': False, 'reason': decision.reason}

        self._forward(context, context.body if decision.body is None else decision.body)
        return 202, {'success': True, 'queued': True}

    def target_url(self, context):
        # Twisted percent-decoded the path segments before routing; ':' and '@'
        # are left unencoded to match what push clients send directly.
        account = quote(context.account, safe='@:')
        if context.action != 'push':
            return '%s/v2/tokens/%s' % (self.base_url, account)
        if context.device_id:
            return '%s/v2/tokens/%s/push/%s' % (self.base_url, account,
                                                quote(context.device_id, safe='@:'))
        return '%s/v2/tokens/%s/push' % (self.base_url, account)

    @property
    def agent(self):
        if self._agent is None:
            self._agent = Agent(reactor, connectTimeout=GeneralConfig.sylk_push_proxy_timeout)
        return self._agent

    @defer.inlineCallbacks
    def _forward(self, context, body):
        url = self.target_url(context)
        headers = Headers({'User-Agent': ['SylkServer'], 'Content-Type': ['application/json']})
        try:
            pending = self.agent.request(context.method, url.encode(), headers, BytesProducer(body))
            pending.addTimeout(GeneralConfig.sylk_push_proxy_timeout, reactor)
            response = yield pending
        except Exception as e:
            log.warning('push proxy: forwarding %s to %s failed: %s' % (context.description, url, e))
            return
        try:
            # read and discard: the connection is not reusable otherwise
            raw_body = yield readBody(response)
        except Exception as e:
            log.warning('push proxy: reading the response from %s failed: %s' % (url, e))
            raw_body = b''
        if 200 <= response.code < 300:
            log.info('push proxy: forwarded %s to %s, response %d' %
                     (context.description, url, response.code))
        else:
            log.warning('push proxy: forwarding %s to %s failed with %d: %s' %
                        (context.description, url, response.code,
                         raw_body.decode('utf-8', 'replace').strip()[:MAX_LOGGED_ERROR_BODY]))
