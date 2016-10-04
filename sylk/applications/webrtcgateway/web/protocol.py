
import json

from autobahn.twisted.websocket import WebSocketServerProtocol
from sipsimple.util import ISOTimestamp

try:
    from autobahn.websocket.http import HttpException
except ImportError:
    # AutoBahn 0.12 changed this
    from autobahn.websocket import ConnectionDeny as HttpException

from sylk.applications.webrtcgateway.configuration import GeneralConfig
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.web.handler import ConnectionHandler


SYLK_WS_PROTOCOL = 'sylkRTC-1'


class SylkWebSocketServerProtocol(WebSocketServerProtocol):
    backend = None
    connection_handler = None

    def onConnect(self, request):
        log.msg('Incoming connection from %s (origin %s)' % (request.peer, request.origin))
        if SYLK_WS_PROTOCOL not in request.protocols:
            log.msg('Rejecting connection, remote does not support our sub-protocol')
            raise HttpException(406, 'No compatible protocol specified')
        if not self.backend.ready:
            log.msg('Rejecting connection, backend is not connected')
            raise HttpException(503, 'Backend is not connected')
        return SYLK_WS_PROTOCOL

    def onOpen(self):
        log.msg('Connection from %s open' % self.transport.getPeer())
        self.factory.connections.add(self)
        self.connection_handler = ConnectionHandler(self)
        self.connection_handler.start()

    def onMessage(self, payload, is_binary):
        if is_binary:
            log.warn('Received invalid binary message')
            return
        if GeneralConfig.trace_websocket:
            self.factory.ws_logger.msg("IN", ISOTimestamp.now(), payload)
        try:
            data = json.loads(payload)
        except Exception, e:
            log.warn('Error parsing WebSocket payload: %s' % e)
            return
        self.connection_handler.handle_message(data)

    def onClose(self, clean, code, reason):
        if self.connection_handler is None:
            # Very early connection closed, onOpen wasn't even called
            return
        log.msg('Connection from %s closed' % self.transport.getPeer())
        self.factory.connections.discard(self)
        self.connection_handler.stop()
        self.connection_handler = None

    def disconnect(self, code=1000, reason=u''):
        self.sendClose(code, reason)

