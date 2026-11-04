
import json

from application.notification import NotificationCenter, NotificationData
from autobahn.twisted.websocket import WebSocketServerProtocol
from autobahn.websocket import ConnectionDeny
from autobahn.exception import Disconnected

from .configuration import GeneralConfig
from .handler import ConnectionHandler
from .janus import JanusBackend
from .logger import log, connections_log
from .metrics import Metrics


SYLK_WS_PROTOCOL = 'sylkRTC-2'


class SylkWebSocketServerProtocol(WebSocketServerProtocol):
    janus_backend = JanusBackend()
    connection_handler = None
    ping_transactions = frozenset()  # transactions of the pings left out of the client API log, to leave out their acks too

    notification_center = NotificationCenter()

    def onConnect(self, request):
        if SYLK_WS_PROTOCOL not in request.protocols:
            log.debug('Connection from {} request: {}'.format(self.peer, request))
            connections_log.info('Rejecting connection from {}, client uses unsupported protocol: {}'.format(self.peer, ','.join(request.protocols)))
            raise ConnectionDeny(406, 'No compatible protocol specified')
        if not self.janus_backend.ready:
            connections_log.warning('Rejecting connection from {}, Janus backend is not connected'.format(self.peer))
            raise ConnectionDeny(503, 'Backend is not connected')
        return SYLK_WS_PROTOCOL

    def onOpen(self):
        self.factory.connections.add(self)
        Metrics().increment('connections')
        self.connection_handler = ConnectionHandler(self)
        self.connection_handler.start()
        self.connection_handler.log.connections.info('websocket connected from: {address}'.format(address=self.peer))

    def onMessage(self, payload, is_binary):
        if is_binary:
            self.connection_handler.log.error('received invalid binary message')
            return
        try:
            data = json.loads(payload)
        except Exception as e:
            self.notification_center.post_notification('WebRTCClientTrace', sender=self, data=NotificationData(direction='INCOMING', message=payload, peer=self.peer))
            self.connection_handler.log.error('could not parse WebSocket payload: {exception!s}'.format(exception=e))
        else:
            if not GeneralConfig.log_webrtc_client_api_ping and isinstance(data, dict) and data.get('sylkrtc') == 'ping':
                self.ping_transactions = self.ping_transactions | {data.get('transaction')}
            else:
                self.notification_center.post_notification('WebRTCClientTrace', sender=self, data=NotificationData(direction='INCOMING', message=payload, peer=self.peer))
            self.connection_handler.handle_message(data)

    def onClose(self, clean, code, reason):
        if self.connection_handler is None:  # Connection was closed very early before onOpen was even called
            return
        self.connection_handler.log.debug('websocket disconnected from {address}'.format(address=self.peer))
        self.factory.connections.discard(self)
        self.connection_handler.stop()
        self.connection_handler = None

    def sendMessage(self, payload, *args, **kw):
        if not self._is_ping_ack(payload):
            self.notification_center.post_notification('WebRTCClientTrace', sender=self, data=NotificationData(direction='OUTGOING', message=payload, peer=self.peer))
        #log.info('Sending %s to web socket %s' % (payload, self.peer));
        try:
            super(SylkWebSocketServerProtocol, self).sendMessage(payload.encode(), *args, **kw)
        except Disconnected as e:
            pass

    def _is_ping_ack(self, payload):
        if not self.ping_transactions or '"ack"' not in payload:
            return False
        try:
            transaction = json.loads(payload).get('transaction')
        except Exception:
            return False
        if transaction not in self.ping_transactions:
            return False
        self.ping_transactions = self.ping_transactions - {transaction}
        return True

    def disconnect(self, code=1000, reason=''):
        self.sendClose(code, reason)
