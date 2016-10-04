
from application.python.types import Singleton
from autobahn.twisted.resource import WebSocketResource

from sylk import __version__ as sylk_version
from sylk.applications.webrtcgateway.configuration import GeneralConfig, JanusConfig
from sylk.applications.webrtcgateway.janus.backend import JanusBackend
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.web.api import SYLK_WS_PROTOCOL, SylkWebSocketServerFactory
from sylk.applications.webrtcgateway.websocket_logger import Logger as WSLogger
from sylk.resources import Resources
from sylk.web import Klein, StaticFileResource, server


class WebRTCGatewayWeb(object):
    __metaclass__ = Singleton

    app = Klein()
    _resource = None
    _ws_resource = None

    def __init__(self, ws_factory):
        self._ws_resource = WebSocketResource(ws_factory)

    def resource(self):
        if self._resource is None:
            self._resource = self.app.resource()
        return self._resource

    @app.route('/')
    def index(self, request):
        path = Resources.get('html/webrtcgateway/index.html')
        r = StaticFileResource(path)
        r.isLeaf = True
        return r

    @app.route('/ws')
    def ws(self, request):
        return self._ws_resource


class WebHandler(object):
    def __init__(self):
        self.backend = None
        self.factory = None
        self.resource = None
        self.web = None
        self.ws_logger = WSLogger()

    def start(self):
        ws_url = 'ws' + server.url[4:] + '/webrtcgateway/ws'
        self.factory = SylkWebSocketServerFactory(ws_url, protocols=[SYLK_WS_PROTOCOL], server='SylkServer/%s' % sylk_version, debug=False)
        self.factory.setProtocolOptions(allowedOrigins=GeneralConfig.web_origins,
                                        autoPingInterval=GeneralConfig.websocket_ping_interval,
                                        autoPingTimeout=GeneralConfig.websocket_ping_interval/2)
        self.factory.ws_logger = self.ws_logger

        self.web = WebRTCGatewayWeb(self.factory)
        server.register_resource('webrtcgateway', self.web.resource())

        log.msg('WebSocket handler started at %s' % ws_url)
        log.msg('Allowed web origins: %s' % ', '.join(GeneralConfig.web_origins))
        log.msg('Allowed SIP domains: %s' % ', '.join(GeneralConfig.sip_domains))
        log.msg('Using Janus API: %s' % JanusConfig.api_url)

        self.ws_logger.start()

        self.backend = JanusBackend()
        self.backend.start()

        self.factory.backend = self.backend

    def stop(self):
        if self.factory is not None:
            for conn in self.factory.connections.copy():
                conn.failConnection()
            self.factory = None
        if self.backend is not None:
            self.backend.stop()
            self.backend = None
        self.ws_logger.stop()
