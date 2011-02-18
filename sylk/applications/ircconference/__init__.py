# Copyright (C) 2011 AG Projects. See LICENSE for details
#

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import ISylkApplication, sylk_application
from sylk.applications.ircconference.room import IRCRoom


@sylk_application
class IRCConferenceApplication(object):
    __metaclass__ = Singleton
    implements(ISylkApplication, IObserver)

    __appname__ = 'irc-conference'

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
        if chat_streams:
            # Disable private message capability
            chat_streams[0].chatroom_capabilities = []
        streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
        reactor.callLater(4 if audio_streams else 0, self.accept_session, session, streams)

    def incoming_subscription(self, subscribe_request, data):
        to_header = data.headers.get('To', Null)
        if to_header is Null:
           subscribe_request.reject(400)
           return
        room = IRCRoom.get_room(data.request_uri)
        if not room.started:
           room = IRCRoom.get_room(to_header.uri)
           if not room.started:
               subscribe_request.reject(480)
               return
        room.handle_incoming_subscription(subscribe_request, data)

    def incoming_sip_message(self, message_request, data):
        pass

    def accept_session(self, session, streams):
        if session in self.pending_sessions:
            session.accept(streams, is_focus=True)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        self.pending_sessions.remove(session)
        room = IRCRoom.get_room(session._invitation.request_uri)    # FIXME
        room.start()
        room.add_session(session)
        self.rooms.add(room)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.msg('Session from %s ended' % session.remote_identity.uri)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=session)
        room = IRCRoom.get_room(session._invitation.request_uri)    # FIXME
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


