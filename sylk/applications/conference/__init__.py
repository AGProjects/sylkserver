# Copyright (C) 2010-2011 AG Projects. See LICENSE for details
#

import re

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError, Header, ContactHeader, FromHeader, ToHeader
from sipsimple.lookup import DNSLookup
from sipsimple.streams import AudioStream
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import ISylkApplication, sylk_application
from sylk.applications.conference.configuration import ConferenceConfig
from sylk.applications.conference.room import Room
from sylk.configuration import SIPConfig
from sylk.extensions import ChatStream
from sylk.session import ServerSession

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

    def incoming_referral(self, refer_request, data):
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        refer_to_header = data.headers.get('Refer-To', Null)
        if Null in (from_header, to_header, refer_to_header):
            refer_request.reject(400)
            return
        referral_handler = IncomingReferralHandler(refer_request, data)
        referral_handler.start()

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

    def add_participant(self, session, room_uri):
        log.msg('New outgoing session to %s' % session.remote_identity.uri)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        room = Room.get_room(room_uri)
        room.start()
        room.add_session(session)
        self.rooms.add(room)

    def remove_participant(self, participant_uri, room_uri):
        room = Room.get_room(room_uri)
        room.terminate_sessions(participant_uri)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        self.pending_sessions.remove(session)
        room = Room.get_room(session._invitation.request_uri)    # FIXME
        room.start()
        room.add_session(session)
        self.rooms.add(room)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.msg('Session from %s ended' % session.remote_identity.uri)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=session)
        if session.direction == 'incoming':
            room = Room.get_room(session._invitation.request_uri)    # FIXME
        else:
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


class IncomingReferralHandler(object):
    implements(IObserver)

    def __init__(self, refer_request, data):
        self._refer_request = refer_request
        self._refer_headers = data.headers
        self.room_uri = data.headers.get('To').uri
        self.refer_to_uri = data.headers.get('Refer-To').uri
        self.method = data.headers.get('Refer-To').parameters.get('method', 'invite').lower()
        self.session = None
        self.streams = []

    def start(self):
        if not re.match('^(sip:|sips:).*'):
            self.refer_to_uri = 'sip:%s' % self.refer_to_uri
        try:
            self.refer_to_uri = SIPURI.parse(self.refer_to_uri)
        except SIPCoreError:
            self._refer_request.reject(488)
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._refer_request)
        if self.method == 'invite':
            self._refer_request.accept()
            settings = SIPSimpleSettings()
            lookup = DNSLookup()
            notification_center.add_observer(self, sender=lookup)
            lookup.lookup_sip_proxy(self.refer_to_uri, settings.sip.transport_list)
        elif self.method == 'bye':
            self._refer_request.accept()
            try:
                conference_application = ConferenceApplication()
                conference_application.remove_participant(self.refer_to_uri, self.room_uri)
            except RoomError:
                self._refer_request.end(404)
            else:
                self._refer_request.end(200)
        else:
            self._refer_request.reject(488)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        account = AccountManager().default_account
        self.streams.append(AudioStream(account))
        self.streams.append(ChatStream(account))
        self.session = ServerSession(account)
        notification_center.add_observer(self, sender=self.session)
        original_from_header = self._refer_headers.get('From')
        from_header = FromHeader(SIPURI.new(self.room_uri))
        to_header = ToHeader(self.refer_to_uri)
        transport = notification.data.result[0].transport
        parameters = {} if transport=='udp' else {'transport': transport}
        contact_header = ContactHeader(SIPURI(user=self.room_uri.user, host=SIPConfig.local_ip, port=getattr(SIPConfig, 'local_%s_port' % transport), parameters=parameters))
        extra_headers = []
        if self._refer_headers.get('Referred-By', None) is not None:
            extra_headers.append(Header.new(self._refer_headers.get('Referred-By')))
        extra_headers.append(Header('Subject', 'Invitation to conference from: %s' % original_from_header.uri))
        self.session.connect(from_header, to_header, contact_header, routes=notification.data.result, streams=self.streams, is_focus=True, extra_headers=extra_headers)

    def _NH_DNSLookupDidFail(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)

    def _NH_SIPSessionGotRingIndication(self, notification):
        if self._refer_request is not None:
            self._refer_request.send_notify(180)

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        if self._refer_request is not None:
            self._refer_request.send_notify(notification.data.code)

    def _NH_SIPSessionDidStart(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        conference_application = ConferenceApplication()
        conference_application.add_participant(self.session, self.room_uri)
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidFail(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(notification.data.code or 500)
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidEnd(self, notification):
        # If any stream fails to start we won't get SIPSessionDidFail, we'll get here instead
        NotificationCenter().remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        self.session = None
        self.streams = []

    def _NH_SIPIncomingReferralDidEnd(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender)
        self._refer_request = None
        self._refer_headers = None

