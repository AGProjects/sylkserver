
import weakref

from application.notification import IObserver, NotificationCenter
from application.python import Null
from autobahn.twisted.websocket import WebSocketServerFactory
from zope.interface import implements

from sylk.applications.webrtcgateway.web.protocol import SylkWebSocketServerProtocol, SYLK_WS_PROTOCOL


class VideoRoomContainer(object):
    def __init__(self):
        self._rooms = set()
        self._uri_map = weakref.WeakValueDictionary()
        self._id_map = weakref.WeakValueDictionary()

    def add(self, room):
        self._rooms.add(room)
        self._uri_map[room.uri] = room
        self._id_map[room.id] = room

    def remove(self, value):
        if isinstance(value, int):
            room = self._id_map.get(value, None)
        elif isinstance(value, basestring):
            room = self._uri_map.get(value, None)
        else:
            room = value
        self._rooms.discard(room)

    def clear(self):
        self._rooms.clear()

    def __len__(self):
        return len(self._rooms)

    def __iter__(self):
        return iter(self._rooms)

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._id_map[item]
        elif isinstance(item, basestring):
            return self._uri_map[item]
        else:
            raise KeyError('%s not found' % item)

    def __contains__(self, item):
        return item in self._id_map or item in self._uri_map or item in self._rooms


class SylkWebSocketServerFactory(WebSocketServerFactory):
    implements(IObserver)

    protocol = SylkWebSocketServerProtocol
    connections = set()
    videorooms = VideoRoomContainer()
    backend = None    # assigned by WebHandler

    def __init__(self, *args, **kw):
        super(SylkWebSocketServerFactory, self).__init__(*args, **kw)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='JanusBackendDisconnected')

    def buildProtocol(self, addr):
        protocol = self.protocol()
        protocol.factory = self
        protocol.backend = self.backend
        return protocol

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_JanusBackendDisconnected(self, notification):
        for conn in self.connections.copy():
            conn.dropConnection(abort=True)
        self.videorooms.clear()
