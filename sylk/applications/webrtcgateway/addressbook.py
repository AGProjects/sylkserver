import json

from twisted.internet import defer, reactor
from twisted.web.client import Agent, readBody
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer

from .configuration import GeneralConfig
from .logger import log
from .models import xcap
from .storage import FileAddressBookStorage

__all__ = 'get_addressbook, update_addressbook'


agent = Agent(reactor)
headers = Headers({'User-Agent': ['SylkServer'],
                   'Content-Type': ['application/json']})

class XCAPRoutes:
    """
    Centralized route resolver for XCAP API endpoints.
    Use resolve() to get full URLs from route names and parameters.
    """

    ROUTES = {
        "GET": {"addressbook": "/api/v1/users/{user}/addressbook",
                "contact": "/api/v1/users/{user}/addressbook/contacts/{contact_id}"},
        "PUT": {"contact": "/api/v1/users/{user}/addressbook/contacts/{contact_id}"},
        "POST": {"contact": "/api/v1/users/{user}/addressbook/contacts"},
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


def get_addressbook(account):
    if not GeneralConfig.xcap_url:
        return _fetch_addressbook(account)
    return _send_fetch_addressbook(account, GeneralConfig.xcap_url)


def update_addressbook(account, request):
    if not GeneralConfig.xcap_url:
        return _update_addressbook(account, request)
    return _send_update_addressbook(account, request, GeneralConfig.xcap_url)


def _update_addressbook(account, request):
    storage = FileAddressBookStorage()
    if request.type == 'contact':
        storage.update_contact(account.id, request.data, request.type, action=request.action)


@defer.inlineCallbacks
def _send_update_addressbook(account, request, destination):
    routes = XCAPRoutes(destination)
    if request.type == 'contact':
        url = routes.resolve(request.type, action=request.action, user=account.id, contact_id=request.data.id)
    else:
        url = routes.resolve(request.type, action=request.action, user=account.id)

    try:
        resp = yield agent.request(routes.method.encode('utf-8'),
                                   url.encode('utf-8'),
                                   headers,
                                   BytesProducer(json.dumps(request.data.__data__).encode())
                                   )
    except defer.CancelledError:
        raise
    except Exception as e:
        log.warning("Error updating addressbook to %s: %s", destination, e)
        raise

    if resp.code not in (200, 204):
        body = yield readBody(resp)
        log.warning("Non-200 response (%s) updating addressbook to %s: %r", resp.code, destination, body)
        raise Exception(f"Non-200 response: {resp.code}, {body}")

    try:
        body = yield readBody(resp)
        payload = json.loads(body)
        return xcap.XCAPMapper.from_payload(payload, request.type)
    except (ValueError, TypeError) as e:
        log.warning("Invalid JSON from %s: %s", destination, e)
        raise


@defer.inlineCallbacks
def _fetch_addressbook(account):
    storage = FileAddressBookStorage()
    payload = yield storage[account.id]
    return xcap.XCAPMapper.from_payload(payload)


@defer.inlineCallbacks
def _send_fetch_addressbook(account, destination):
    routes = XCAPRoutes(destination)
    url = routes.resolve("addressbook", user=account.id)
    try:
        resp = yield agent.request(b'GET', url.encode('utf-8'), headers=headers)
    except defer.CancelledError:
        raise
    except Exception as e:
        log.warning("Error fetching addressbook from %s: %s", destination, e)
        return xcap.AddressBook([], [], [])

    if resp.code != 200:
        body = yield readBody(resp)
        log.warning("Non-200 response (%s) fetching addressbook from %s: %r", resp.code, destination, body)
        return xcap.AddressBook(contacts=[], groups=[], policies=[])

    try:
        body = yield readBody(resp)
        payload = json.loads(body)
        return xcap.XCAPMapper.from_payload(payload)
    except (ValueError, TypeError) as e:
        log.warning("Invalid JSON from %s: %s", destination, e)
        return xcap.AddressBook([], [], [])


