
from application.notification import IObserver, NotificationCenter
from application.python import Null
from autobahn.twisted.websocket import WebSocketServerFactory
from zope.interface import implements

from .protocol import SylkWebSocketServerProtocol


class VideoroomContainer(object):
    def __init__(self):
        self._rooms = set()
        self._id_map = {}  # map videoroom.id -> videoroom and videoroom.uri -> videoroom

    def add(self, room):
        self._rooms.add(room)
        self._id_map[room.id] = self._id_map[room.uri] = room

    def discard(self, item):  # item can be any of room, room.id or room.uri
        room = self._id_map[item] if item in self._id_map else item if item in self._rooms else None
        if room is not None:
            self._rooms.discard(room)
            self._id_map.pop(room.id, None)
            self._id_map.pop(room.uri, None)

    def remove(self, item):  # item can be any of room, room.id or room.uri
        room = self._id_map[item] if item in self._id_map else item
        self._rooms.remove(room)
        self._id_map.pop(room.id)
        self._id_map.pop(room.uri)

    def pop(self, item):  # item can be any of room, room.id or room.uri
        room = self._id_map[item] if item in self._id_map else item
        self._rooms.remove(room)
        self._id_map.pop(room.id)
        self._id_map.pop(room.uri)
        return room

    def clear(self):
        self._rooms.clear()
        self._id_map.clear()

    def __len__(self):
        return len(self._rooms)

    def __iter__(self):
        return iter(self._rooms)

    def __getitem__(self, key):
        return self._id_map[key]

    def __contains__(self, item):
        return item in self._id_map or item in self._rooms


class SylkWebSocketServerFactory(WebSocketServerFactory):
    implements(IObserver)

    protocol = SylkWebSocketServerProtocol
    connections = set()
    videorooms = VideoroomContainer()

    def __init__(self, *args, **kw):
        super(SylkWebSocketServerFactory, self).__init__(*args, **kw)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='JanusBackendDisconnected')

    def buildProtocol(self, addr):
        protocol = self.protocol()
        protocol.factory = self
        return protocol

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_JanusBackendDisconnected(self, notification):
        for conn in self.connections.copy():
            conn.dropConnection(abort=True)
        self.videorooms.clear()
