
"""
SIP backend using SylkServer's own SIP stack (SIP SIMPLE SDK).

SylkServer does the SIP signalling (REGISTER, INVITE through
sylk.session.Session) and Janus is used only for the media, through the
NoSIP plugin, which converts between the WebRTC PeerConnection and plain
RTP/SRTP.

Registration: every account of a WebRTC client registers a contact of its
own, pointing to this SylkServer:

    sip:<user>@<sylkserver>:<port>;transport=<t>;x-sylk-app=webrtcgateway;x-sylk-gw=<token>[;pn_...]

The x-sylk-app parameter makes SylkServer hand the requests sent to the
contact to the webrtcgateway application, and the x-sylk-gw token tells
which client connection and account they are for (see find_contact). The
push notification parameters given by the client (account-devicetoken) are
added to the contact URI, as the Janus SIP plugin does.

Not implemented yet: calls. Outgoing calls are refused and incoming calls
are rejected with 480.
"""

import secrets

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.core import ContactHeader, Credentials, FromHeader, Header, Registration, RouteHeader, SIPURI, SIPCoreError
from sipsimple.threading import run_in_twisted_thread
from zope.interface import implementer

from sylk.accounts import DefaultAccount

from .base import SIPAccountBackend, SIPBackend, SIPBackendError


__all__ = 'SIPSimpleBackend', 'CONTACT_TOKEN_PARAMETER', 'contact_token', 'find_contact'


CONTACT_APP_PARAMETER = 'x-sylk-app'
CONTACT_TOKEN_PARAMETER = 'x-sylk-gw'

REGISTRATION_DURATION = 600  # seconds, same as the Janus SIP plugin's register_ttl

_contacts = {}  # contact token -> SIPSimpleAccount


def contact_token(uri):
    """The x-sylk-gw token of a SIP URI (a request URI), or None"""
    parameters = getattr(uri, 'parameters', None) or {}
    for key in (CONTACT_TOKEN_PARAMETER, CONTACT_TOKEN_PARAMETER.encode()):
        try:
            value = parameters[key]
        except (KeyError, TypeError):
            continue
        return value.decode() if isinstance(value, bytes) else value
    return None


def find_contact(uri):
    """The SIPSimpleAccount a request URI was sent to, or None if it is unknown (stale)"""
    token = contact_token(uri)
    return _contacts.get(token) if token is not None else None


def user_agent(account_info):
    """The client's User-Agent, made safe for use as a SIP header value, or None"""
    return ' '.join(str(account_info.user_agent or '').split())[:200] or None


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


@implementer(IObserver)
class SIPSimpleAccount(SIPAccountBackend):
    def __init__(self, backend, account_info):
        super(SIPSimpleAccount, self).__init__(backend, account_info)
        self.token = secrets.token_hex(8)
        self.contact_header = None
        self.route = None
        self._registration = None
        self._registered = False

    @property
    def registration(self):
        if self._registration is None:
            from_uri = SIPURI.parse(self.account_info.uri)
            credentials = Credentials(username=_text(from_uri.user), password=self.account_info.password.encode('utf-8'), digest=True)  # the password is the HA1 hash
            self._registration = Registration(FromHeader(from_uri, self.account_info.display_name), credentials=credentials, duration=REGISTRATION_DURATION, extra_headers=self.extra_headers)
            NotificationCenter().add_observer(self, sender=self._registration)
        return self._registration

    @property
    def extra_headers(self):
        # The requests sent on behalf of the client carry the client's
        # User-Agent (the one it gave in account-add), as with the Janus SIP
        # plugin, instead of SylkServer's. The SIP stack only adds its own
        # User-Agent header when the request has none.
        return [Header('User-Agent', user_agent(self.account_info))] if user_agent(self.account_info) else []

    def lookup_proxy(self):
        # REGISTER goes where the account's other SIP requests go (the Thor
        # node responsible for the account, the outbound proxy or the
        # domain's DNS records), never to the local server.
        return self.handler._lookup_sip_target_route(self.account_info.id, sender=self.account_info.id, local=False)

    def reset(self):
        # The same Registration (and contact) is reused for every attempt, so
        # a new attempt refreshes the existing binding instead of adding one.
        pass

    def register(self, proxy):
        self.route = proxy
        self.contact_header = self._build_contact_header(proxy)
        _contacts[self.token] = self
        self.handler.post_backend_event(self.handler.sip_account_registering, self.account_info)
        self.log.debug('sipsimple: REGISTER {} with contact {} via {}'.format(self.account_info.id, self.contact_header.uri, proxy.uri))
        self.registration.register(self.contact_header, RouteHeader(proxy.uri), timeout=10)

    def release(self):
        self._end()

    def connection_lost(self):
        self._end()

    def _end(self):
        _contacts.pop(self.token, None)
        registration, self._registered = self._registration, False
        if registration is None:
            return
        if registration.is_registered:
            try:
                registration.end(timeout=2)
            except SIPCoreError:
                pass
            else:
                return  # keep observing it until the unREGISTER is answered, see _NH_SIPRegistrationDidEnd
        NotificationCenter().discard_observer(self, sender=registration)
        self._registration = None

    def _build_contact_header(self, route):
        base = DefaultAccount().contact[route]  # the SIP address of this server for the route's transport
        user = SIPURI.parse(self.account_info.uri).user
        parameters = dict(base.parameters)
        parameters[CONTACT_APP_PARAMETER] = 'webrtcgateway'
        parameters[CONTACT_TOKEN_PARAMETER] = self.token
        for name, value in self.account_info.contact_params.items():
            if value is not None:
                parameters[name] = str(value)
        uri = SIPURI(user=user, host=base.host, port=base.port, parameters=parameters)
        contact_header = ContactHeader(uri)
        contact_header.parameters['+sip.instance'] = '"<urn:uuid:{}>"'.format(self.handler.sip_instance_id)
        return contact_header

    # Registration notifications (from the SIP stack's thread)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPRegistrationDidSucceed(self, notification):
        if self.token not in _contacts:  # released meanwhile, the unREGISTER is on its way
            return
        self._registered = True
        self.log.debug('sipsimple: registered {} for {}s, {} contact(s) bound to the account'.format(
            self.account_info.id, notification.data.expires_in, len(notification.data.contact_header_list)))
        self.handler.post_backend_event(self.handler.sip_account_registered, self.account_info)

    def _NH_SIPRegistrationDidFail(self, notification):
        if self.token not in _contacts:
            return
        self._registered = False
        reason = '{} {}'.format(notification.data.code, _text(notification.data.reason)) if notification.data.code else _text(notification.data.reason)
        self.handler.post_backend_event(self.handler.sip_account_registration_failed, self.account_info, reason)

    def _NH_SIPRegistrationWillExpire(self, notification):
        if self.token not in _contacts or self.contact_header is None:
            return
        self.registration.register(self.contact_header, RouteHeader(self.route.uri), timeout=10)

    def _NH_SIPRegistrationDidEnd(self, notification):
        if self.token in _contacts and notification.data.expired:
            # the binding expired without being refreshed (e.g. the refresh could not be sent)
            self._registered = False
            self.handler.post_backend_event(self.handler.sip_account_registration_failed, self.account_info, 'registration expired')
        elif self.token not in _contacts:
            NotificationCenter().discard_observer(self, sender=notification.sender)
            if notification.sender is self._registration:
                self._registration = None

    def _NH_SIPRegistrationDidNotEnd(self, notification):
        if self.token not in _contacts:
            NotificationCenter().discard_observer(self, sender=notification.sender)
            if notification.sender is self._registration:
                self._registration = None


class SIPSimpleBackend(SIPBackend):
    name = 'sipsimple'

    def create_account(self, account_info):
        return SIPSimpleAccount(self, account_info)

    def create_outgoing_session(self, account_info, session_info, uri, sdp, headers):
        raise SIPBackendError('calls are not implemented yet in the sipsimple SIP backend')

    @staticmethod
    def incoming_session(session):
        """An INVITE sent to a contact registered by this backend (see the webrtcgateway application)"""
        account = find_contact(session.request_uri)
        if account is None:
            session.reject(480)  # stale contact, the client is gone
        else:
            account.log.info('sipsimple: rejecting incoming call from {}, calls are not implemented yet'.format(session.remote_identity.uri))
            session.reject(480)
