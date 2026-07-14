import json

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

    try:
        resp = yield agent.request(routes.method.encode('utf-8'),
                                   url.encode('utf-8'),
                                   _make_headers(account),
                                   BytesProducer(json.dumps(request.data.__data__).encode())
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
        return xcap.XCAPMapper.from_payload(request.data.__data__, request.type)

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


