import hashlib
import json
import time

from twisted.internet import defer, reactor
from twisted.web.client import Agent, readBody
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer
from zope.interface import implementer

from .configuration import GeneralConfig
from .logger import log
from .models import xcap
from .storage import FileAddressBookStorage

__all__ = 'get_addressbook, update_addressbook'


class AddressbookUpdateError(Exception):
    """Carries whether a failed addressbook update is worth retrying.

    retryable=True  -> transient (XCAP unreachable, connection reset, 5xx,
                       408/429): the client should queue the change and re-push
                       when the server is back.
    retryable=False -> permanent (4xx validation, or a parse error AFTER the
                       write already succeeded): retrying won't help / could
                       duplicate, so the client should drop it.
    """
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


agent = Agent(reactor)


def _make_headers(account):
    # Use the original device's user agent (captured from the client's
    # account add request) so the XCAP server logs show who really made
    # the change, falling back to SylkServer when it is unknown.
    user_agent = getattr(account, 'user_agent', None) or 'SylkServer'
    try:
        user_agent.encode('latin-1')  # header values must be latin-1 safe
    except (UnicodeEncodeError, AttributeError):
        user_agent = 'SylkServer'
    return Headers({'User-Agent': [user_agent],
                    'Content-Type': ['application/json']})

# --- origin stamps ------------------------------------------------------------
# Blink and Sylk mobile stamp every contact and group they write with who
# changed it (attributes modified_by/_agent/_at/_reason/_hash, see Blink's
# AddressbookOrigin.py and sylk-mobile's addressbookOrigin.js). A WebRTC client
# that does not stamp leaves its writes anonymous: the other devices can only
# say "by a client that does not stamp". So a write that arrives here without
# a stamp for its own content is stamped on the client's behalf, with the user
# agent it registered with. The fingerprint must stay byte-identical to the
# clients' one, or every stamp made here reads as "after the last stamp".

STAMP_KEYS = ('modified_by', 'modified_agent', 'modified_at', 'modified_reason', 'modified_hash')


def _text(value):
    return '' if value is None else str(value)


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1')
    return bool(value)


def _event(handling):
    handling = handling or {}
    policy = handling.get('policy')
    return [_text('default' if policy is None else policy), _bool(handling.get('subscribe', False))]


def _digest(body):
    data = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha1(data.encode('utf-8')).hexdigest()[:16]


def contact_fingerprint(payload):
    uris = sorted([_text(uri.get('uri')).strip(), _text(uri.get('type'))]
                  for uri in (payload.get('uris') or ()))
    return _digest({'name': _text(payload.get('name')),
                    'uris': uris,
                    'presence': _event(payload.get('presence')),
                    'dialog': _event(payload.get('dialog'))})


def group_fingerprint(payload):
    members = [_text(item.get('id') if isinstance(item, dict) else item)
               for item in (payload.get('contacts') or ())]
    return _digest({'name': _text(payload.get('name')),
                    'contacts': sorted(members)})


def stamp_payload(kind, payload, agent, now=None):
    """Return the payload to send, stamped when the client did not stamp it.

    A client stamp that matches the content is left alone -- that client
    knows its own device id better than we do.
    """
    fingerprint = {'contact': contact_fingerprint, 'group': group_fingerprint}.get(kind)
    if fingerprint is None or not isinstance(payload, dict):
        return payload, False
    current = fingerprint(payload)
    attributes = dict(payload.get('attributes') or {})
    if attributes.get('modified_hash') == current and attributes.get('modified_by'):
        return payload, False
    attributes.update({'modified_by': 'sylkserver',
                       'modified_agent': _text(agent) or 'SylkServer',
                       'modified_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                    time.gmtime(time.time() if now is None else now)),
                       'modified_reason': 'stamped-by-sylkserver',
                       'modified_hash': current})
    return dict(payload, attributes=attributes), True


class XCAPRoutes:
    """
    Centralized route resolver for XCAP API endpoints.
    Use resolve() to get full URLs from route names and parameters.
    """

    ROUTES = {
        "GET": {"addressbook": "/api/v1/users/{user}/addressbook",
                "contact": "/api/v1/users/{user}/addressbook/contacts/{contact_id}",
                "group": "/api/v1/users/{user}/addressbook/groups/{group_id}",
                "policy": "/api/v1/users/{user}/addressbook/policies/{policy_id}"},
        "PUT": {"contact": "/api/v1/users/{user}/addressbook/contacts/{contact_id}",
                "group": "/api/v1/users/{user}/addressbook/groups/{group_id}",
                "policy": "/api/v1/users/{user}/addressbook/policies/{policy_id}"},
        "POST": {"contact": "/api/v1/users/{user}/addressbook/contacts",
                 "group": "/api/v1/users/{user}/addressbook/groups",
                 "policy": "/api/v1/users/{user}/addressbook/policies"},
        "DELETE": {"contact": "/api/v1/users/{user}/addressbook/contacts/{contact_id}",
                   "group": "/api/v1/users/{user}/addressbook/groups/{group_id}",
                   "policy": "/api/v1/users/{user}/addressbook/policies/{policy_id}"}
    }

    ACTION_MAP = {'add': 'POST',
                  'update': 'PUT',
                  'delete': 'DELETE'}

    def __init__(self, base_url: str):
        self.base_url = str(base_url).rstrip("/")
        self.method = 'GET'

    @staticmethod
    def _build_url(template: str, **params) -> str:
        """
        Replace placeholders like {user} with actual values.
        """
        url = template
        for k, v in params.items():
            url = url.replace(f"{{{k}}}", str(v))
        return url

    def resolve(self, model_name: str, method: str = "GET", action: str = None, **params) -> str:
        """
        Return the full URL for a given route name, HTTP method, and parameters.

        Example:
            routes = XCAPRoutes("https://xcap.example.com/")
            routes.resolve("addressbook", user="alice")
            → "https://xcap.example.com/api/v1/users/alice/addressbook/"
        """
        self.method = method.upper()

        if action:
            self.method = self.ACTION_MAP.get(action.lower(), self.method)

        try:
            route_template = self.ROUTES[self.method][model_name]
        except KeyError:
            raise ValueError(f"No route defined for method {method} and model {model_name}")
        return self.base_url + self._build_url(route_template, **params)


@implementer(IBodyProducer)
class BytesProducer(object):
    def __init__(self, data):
        self.body = data
        self.length = len(data)

    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
        pass


def get_addressbook(account, raise_on_error=False):
    # raise_on_error=True lets callers distinguish a FAILED fetch (XCAP
    # unreachable / non-200 / bad JSON) from a genuinely empty addressbook.
    # The default (False) preserves the legacy "return empty on failure"
    # behaviour for the initial/login fetch path.
    if not GeneralConfig.xcap_url:
        return _fetch_addressbook(account)
    return _send_fetch_addressbook(account, GeneralConfig.xcap_url, raise_on_error=raise_on_error)


def update_addressbook(account, request):
    if not GeneralConfig.xcap_url:
        return _update_addressbook(account, request)
    return _send_update_addressbook(account, request, GeneralConfig.xcap_url)


def _update_addressbook(account, request):
    storage = FileAddressBookStorage()
    if request.type in ('contact', 'group', 'policy'):
        storage.update(account.id, request.data, request.type, action=request.action)


@defer.inlineCallbacks
def _send_update_addressbook(account, request, destination):
    routes = XCAPRoutes(destination)
    if request.type == 'contact':
        url = routes.resolve(request.type, action=request.action, user=account.id, contact_id=request.data.id)
    elif request.type == 'group':
        url = routes.resolve(request.type, action=request.action, user=account.id, group_id=request.data.id)
    elif request.type == 'policy':
        url = routes.resolve(request.type, action=request.action, user=account.id, policy_id=request.data.id)
    else:
        url = routes.resolve(request.type, action=request.action, user=account.id)

    payload = request.data.__data__
    stamped = False
    if request.action in ('add', 'update'):
        try:
            payload, stamped = stamp_payload(request.type, payload, getattr(account, 'user_agent', None))
        except Exception as e:
            log.warning("Cannot stamp addressbook %s %s: %s", request.type, getattr(request.data, 'id', None), e)
    # Who changed what: a removal carries no stamp in the document, so this
    # line is the only record of which client deleted an entry.
    log.info("Addressbook %s %s %s for %s by %s%s", request.action, request.type,
             getattr(request.data, 'id', None), account.id,
             getattr(account, 'user_agent', None) or 'unknown client',
             ' (stamped here)' if stamped else '')

    try:
        resp = yield agent.request(routes.method.encode('utf-8'),
                                   url.encode('utf-8'),
                                   _make_headers(account),
                                   BytesProducer(json.dumps(payload).encode())
                                   )
    except defer.CancelledError:
        raise
    except Exception as e:
        # Transport-level failure (XCAP unreachable, DNS, connection reset) —
        # the write never landed, so it is safe and worthwhile to retry.
        log.warning("Error updating addressbook to %s: %s", destination, e)
        raise AddressbookUpdateError(str(e), retryable=True)

    if resp.code not in (200, 204):
        body = yield readBody(resp)
        body_text = body.decode('utf-8')
        log.warning("Non-200 response (%s) updating addressbook to %s for account %s, %s id %s: %r",
                    resp.code, destination, account.id, request.type, getattr(request.data, 'id', None), body_text)
        try:
            detail = json.loads(body_text).get('detail', body_text)
            if isinstance(detail, list):
                detail = ', '.join(e.get('msg', str(e)) for e in detail)
        except (ValueError, TypeError):
            detail = body_text
        # 5xx / 408 / 429 are transient; 4xx is a permanent rejection (bad data).
        retryable = resp.code >= 500 or resp.code in (408, 429)
        raise AddressbookUpdateError(f"Non-200 response: {resp.code}, {detail}", retryable=retryable)

    if resp.code == 204 and routes.method == 'DELETE':
        return xcap.XCAPMapper.from_payload(payload, request.type)

    try:
        body = yield readBody(resp)
        payload = json.loads(body)
        return xcap.XCAPMapper.from_payload(payload, request.type)
    except (ValueError, TypeError) as e:
        # The write SUCCEEDED (2xx); only parsing the echoed body failed.
        # Retrying would duplicate the change, so this is not retryable.
        log.warning("Invalid JSON from %s: %s", destination, e)
        raise AddressbookUpdateError(str(e), retryable=False)


@defer.inlineCallbacks
def _fetch_addressbook(account):
    storage = FileAddressBookStorage()
    payload = yield storage[account.id]
    if payload:
        return xcap.XCAPMapper.from_payload(payload)
    return xcap.AddressBook(contacts=[], groups=[], policies=[])


@defer.inlineCallbacks
def _send_fetch_addressbook(account, destination, raise_on_error=False):
    # When raise_on_error is True a failure propagates as a failed Deferred so
    # the caller can react (e.g. skip a broadcast) instead of being handed an
    # empty addressbook that masquerades as "no contacts" — which downstream
    # clients would treat as a mass deletion. When False, the legacy behaviour
    # of returning an empty addressbook on failure is preserved.
    routes = XCAPRoutes(destination)
    url = routes.resolve("addressbook", user=account.id)
    try:
        resp = yield agent.request(b'GET', url.encode('utf-8'), headers=_make_headers(account))
    except defer.CancelledError:
        raise
    except Exception as e:
        log.warning("Error fetching addressbook for account %s from %s: %s", account.id, url, e)
        if raise_on_error:
            raise
        return xcap.AddressBook(contacts=[], groups=[], policies=[])

    if resp.code == 404:
        yield readBody(resp)
        log.info("No addressbook found for account %s", account.id)
        if raise_on_error:
            raise Exception("No addressbook found for account %s" % account.id)
        return xcap.AddressBook(contacts=[], groups=[], policies=[])

    if resp.code != 200:
        body = yield readBody(resp)
        body_text = body.decode('utf-8')
        log.warning("Non-200 response (%s) fetching addressbook for account %s from %s: %r", resp.code, account.id, url, body_text)
        if raise_on_error:
            raise Exception("Non-200 response (%s) fetching addressbook" % resp.code)
        return xcap.AddressBook(contacts=[], groups=[], policies=[])

    try:
        body = yield readBody(resp)
        payload = json.loads(body)
        addressbook = xcap.XCAPMapper.from_payload(payload)
        log.info('Fetched addressbook for %s from %s: %d contacts',
                 account.id, destination, len(addressbook.contacts or []))
        return addressbook
    except (ValueError, TypeError) as e:
        log.warning("Invalid JSON from %s: %s", destination, e)
        if raise_on_error:
            raise
        return xcap.AddressBook(contacts=[], groups=[], policies=[])


