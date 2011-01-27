# Copyright (C) 2010-2011 AG Projects. See LICENSE for details
#

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import ISylkApplication, sylk_application
from sylk.applications.conference.configuration import ConferenceConfig
from sylk.applications.conference.room import Room

# Initialize database
from sylk.applications.conference import database


@sylk_application
class ConferenceApplication(object):
    __metaclass__ = Singleton
    implements(ISylkApplication, IObserver)

    __appname__ = 'conference'

    def __init__(self):
        self.rooms = set()
        self.pending_sessions = []

    def incoming_session(self, session):
        log.msg('New incoming session from %s' % session.remote_identity.uri)
        audio_streams = [stream for stream in session.proposed_streams if stream.type=='audio']
        chat_streams = [stream for stream in session.proposed_streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            session.reject(488)
            return
        self.pending_sessions.append(session)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        if audio_streams:
            session.send_ring_indication()
        streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
        reactor.callLater(4 if audio_streams else 0, self.accept_session, session, streams)

    def incoming_subscription(self, subscribe_request, data):
        to_header = data.headers.get('To', Null)
        if to_header is Null:
            subscribe_request.reject(400)
            return
        room = Room.get_room(data.request_uri)
        if not room.started:
            room = Room.get_room(to_header.uri)
            if not room.started:
                subscribe_request.reject(480)
                return
        room.handle_incoming_subscription(subscribe_request, data)

    def incoming_sip_message(self, message_request, data):
        if not ConferenceConfig.enable_sip_message:
            return
        room = Room.get_room(data.request_uri)
        if not room.started:
            message_request.answer(480)
            return
        room.handle_incoming_sip_message(message_request, data)

    def accept_session(self, session, streams):
        if session in self.pending_sessions:
            session.accept(streams, is_focus=True)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        self.pending_sessions.remove(session)
        room = Room.get_room(session.local_identity.uri)
        room.start()
        room.add_session(session)
        self.rooms.add(room)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.msg('Session from %s ended' % session.remote_identity.uri)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=session)
        room = Room.get_room(session.local_identity.uri)
        if session in room.sessions:
            # We could get this notifiction even if we didn't get SIPSessionDidStart
            room.remove_session(session)
        if room.empty:
            room.stop()
            try:
                self.rooms.remove(room)
            except KeyError:
                pass

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        self.pending_sessions.remove(session)
        log.msg('Session from %s failed' % session.remote_identity.uri)


