
import json

from application.notification import NotificationCenter, NotificationData
from autobahn.twisted.websocket import WebSocketServerProtocol

try:
    from autobahn.websocket.http import HttpException
except ImportError:
    # AutoBahn 0.12 changed this
    from autobahn.websocket import ConnectionDeny as HttpException

from sylk.applications.webrtcgateway.handler import ConnectionHandler
from sylk.applications.webrtcgateway.janus import JanusBackend
from sylk.applications.webrtcgateway.logger import log


SYLK_WS_PROTOCOL = 'sylkRTC-2'


class SylkWebSocketServerProtocol(WebSocketServerProtocol):
    janus_backend = JanusBackend()
    connection_handler = None

    notification_center = NotificationCenter()

    def onConnect(self, request):
        if SYLK_WS_PROTOCOL not in request.protocols:
            log.info('Rejecting connection from %s, remote does not support our sub-protocol' % self.peer)
            raise HttpException(406, u'No compatible protocol specified')
        if not self.janus_backend.ready:
            log.info('Rejecting connection from %s, Janus backend is not connected' % self.peer)
            raise HttpException(503, u'Backend is not connected')
        return SYLK_WS_PROTOCOL

    def onOpen(self):
        self.factory.connections.add(self)
        self.connection_handler = ConnectionHandler(self)
        self.connection_handler.start()
        self.connection_handler.log.info('connected from {address}'.format(address=self.peer))

    def onMessage(self, payload, is_binary):
        if is_binary:
            self.connection_handler.log.error('received invalid binary message')
            return
        self.notification_center.post_notification('WebRTCClientTrace', sender=self, data=NotificationData(direction='INCOMING', message=payload, peer=self.peer))
        try:
            data = json.loads(payload)
        except Exception as e:
            self.connection_handler.log.error('could not parse WebSocket payload: {exception!s}'.format(exception=e))
        else:
            self.connection_handler.handle_message(data)

    def onClose(self, clean, code, reason):
        if self.connection_handler is None:  # Connection was closed very early before onOpen was even called
            return
        self.connection_handler.log.info('disconnected')
        self.factory.connections.discard(self)
        self.connection_handler.stop()
        self.connection_handler = None

    def sendMessage(self, payload, *args, **kw):
        self.notification_center.post_notification('WebRTCClientTrace', sender=self, data=NotificationData(direction='OUTGOING', message=payload, peer=self.peer))
        super(SylkWebSocketServerProtocol, self).sendMessage(payload, *args, **kw)

    def disconnect(self, code=1000, reason=u''):
        self.sendClose(code, reason)

