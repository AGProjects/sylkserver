
import json

from application.python.types import Singleton
from autobahn.twisted.resource import WebSocketResource
from twisted.internet import reactor
from twisted.web.server import Site

from sylk import __version__ as sylk_version
from sylk.applications.webrtcgateway import push
from sylk.applications.webrtcgateway.configuration import GeneralConfig, JanusConfig
from sylk.applications.webrtcgateway.factory import SylkWebSocketServerFactory
from sylk.applications.webrtcgateway.janus import JanusBackend
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.protocol import SYLK_WS_PROTOCOL
from sylk.applications.webrtcgateway.storage import TokenStorage
from sylk.resources import Resources
from sylk.web import Klein, StaticFileResource, server


__all__ = 'WebHandler', 'AdminWebHandler'


class WebRTCGatewayWeb(object):
    __metaclass__ = Singleton

    app = Klein()

    def __init__(self, ws_factory):
        self._resource = self.app.resource()
        self._ws_resource = WebSocketResource(ws_factory)

    @property
    def resource(self):
        return self._resource

    @app.route('/', branch=True)
    def index(self, request):
        return StaticFileResource(Resources.get('html/webrtcgateway/'))

    @app.route('/ws')
    def ws(self, request):
        return self._ws_resource


class WebHandler(object):
    def __init__(self):
        self.backend = None
        self.factory = None
        self.resource = None
        self.web = None

    def start(self):
        ws_url = 'ws' + server.url[4:] + '/webrtcgateway/ws'
        self.factory = SylkWebSocketServerFactory(ws_url, protocols=[SYLK_WS_PROTOCOL], server='SylkServer/%s' % sylk_version)
        self.factory.setProtocolOptions(allowedOrigins=GeneralConfig.web_origins,
                                        autoPingInterval=GeneralConfig.websocket_ping_interval,
                                        autoPingTimeout=GeneralConfig.websocket_ping_interval/2)

        self.web = WebRTCGatewayWeb(self.factory)
        server.register_resource('webrtcgateway', self.web.resource)

        log.info('WebSocket handler started at %s' % ws_url)
        log.info('Allowed web origins: %s' % ', '.join(GeneralConfig.web_origins))
        log.info('Allowed SIP domains: %s' % ', '.join(GeneralConfig.sip_domains))
        log.info('Using Janus API: %s' % JanusConfig.api_url)

        self.backend = JanusBackend()
        self.backend.start()

    def stop(self):
        if self.factory is not None:
            for conn in self.factory.connections.copy():
                conn.dropConnection(abort=True)
            self.factory = None
        if self.backend is not None:
            self.backend.stop()
            self.backend = None


# TODO: This implementation is a prototype.  Moving forward it probably makes sense to provide admin API
# capabilities for other applications too.  This could be done in a number of ways:
#
# * On the main web server, under a /admin/ parent route.
# * On a separate web server, which could listen on a different IP and port.
#
# In either case, HTTPS aside, a token based authentication mechanism would be desired.
# Which one is best is not 100% clear at this point.

class AuthError(Exception): pass


class AdminWebHandler(object):
    __metaclass__ = Singleton

    app = Klein()

    def __init__(self):
        self.listener = None

    def start(self):
        host, port = GeneralConfig.http_management_interface
        # noinspection PyUnresolvedReferences
        self.listener = reactor.listenTCP(port, Site(self.app.resource()), interface=host)
        log.info('Admin web handler started at http://%s:%d' % (host, port))

    def stop(self):
        if self.listener is not None:
            self.listener.stopListening()
            self.listener = None

    # Admin web API

    def _check_auth(self, request):
        auth_secret = GeneralConfig.http_management_auth_secret
        if auth_secret:
            auth_headers = request.requestHeaders.getRawHeaders('Authorization', default=None)
            if not auth_headers or auth_headers[0] != auth_secret:
                raise AuthError()

    @app.handle_errors(AuthError)
    def auth_error(self, request, failure):
        request.setResponseCode(403)
        return 'Authentication error'

    @app.route('/incoming_call', methods=['POST'])
    def incoming_session(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        try:
            data = json.load(request.content)
            originator = data['originator']
            destination = data['destination']
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})
        else:
            storage = TokenStorage()
            tokens = storage[destination]
            push.incoming_call(originator, destination, tokens)
            return json.dumps({'success': True})

    @app.route('/missed_call', methods=['POST'])
    def missed_session(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        try:
            data = json.load(request.content)
            originator = data['originator']
            destination = data['destination']
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})
        else:
            storage = TokenStorage()
            tokens = storage[destination]
            push.missed_call(originator, destination, tokens)
            return json.dumps({'success': True})

    @app.route('/tokens/<string:account>')
    def get_tokens(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        storage = TokenStorage()
        tokens = storage[account]
        return json.dumps({'tokens': list(tokens)})

    @app.route('/tokens/<string:account>/<string:token>', methods=['POST', 'DELETE'])
    def process_token(self, request, account, token):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        storage = TokenStorage()
        if request.method == 'POST':
            storage.add(account, token)
        elif request.method == 'DELETE':
            storage.remove(account, token)
        return json.dumps({'success': True})
