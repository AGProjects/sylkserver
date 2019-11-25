
import json

from application.notification import NotificationCenter, NotificationData
from autobahn.twisted.websocket import WebSocketServerProtocol
from autobahn.websocket import ConnectionDeny

from .handler import ConnectionHandler
from .janus import JanusBackend
from .logger import log


SYLK_WS_PROTOCOL = 'sylkRTC-2'


class SylkWebSocketServerProtocol(WebSocketServerProtocol):
    janus_backend = JanusBackend()
    connection_handler = None

    notification_center = NotificationCenter()

    def onConnect(self, request):
        if SYLK_WS_PROTOCOL not in request.protocols:
            log.debug('Connection from {} request: {}'.format(self.peer, request))
            log.info('Rejecting connection from {}, client uses unsupported protocol: {}'.format(self.peer, ','.join(request.protocols)))
            raise ConnectionDeny(406, u'No compatible protocol specified')
        if not self.janus_backend.ready:
            log.warning('Rejecting connection from {}, Janus backend is not connected'.format(self.peer))
            raise ConnectionDeny(503, u'Backend is not connected')
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
