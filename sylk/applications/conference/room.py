# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import random

from datetime import datetime
from glob import glob
from itertools import cycle
from time import mktime

from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from eventlet import coros, proc
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FromHeader, ToHeader, RouteHeader, SIPURI, Message, SIPCoreInvalidStateError
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.conference import Conference, ConferenceDescription, ConferenceState, Endpoint, EndpointStatus, HostInfo, JoiningInfo, Media, User, Users, WebPage
from sipsimple.payloads.iscomposing import IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams.applications.chat import CPIMIdentity, CPIMMessage, CPIMParserError
from sipsimple.streams.msrp import ChatStreamError
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import Timestamp
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications.conference import database
from sylk.applications.conference.configuration import ConferenceConfig
from sylk.configuration.datatypes import ResourcePath


def format_identity(identity, cpim_format=False):
    uri = identity.uri
    if identity.display_name:
        return '%s <sip:%s@%s>' % (identity.display_name, uri.user, uri.host)
    elif cpim_format:
        return '<sip:%s@%s>' % (uri.user, uri.host)
    else:
        return 'sip:%s@%s' % (uri.user, uri.host)

def format_stream_types(streams):
    if not streams:
        return ''
    if len(streams) == 1:
        txt = 'with %s' % streams[0].type
    else:
        txt = 'with %s' % ','.join(stream.type for stream in streams[:-1])
        txt += ' and %s' % streams[-1:][0].type
    return txt

def format_conference_stream_type(stream):
    if stream.type == 'chat':
        return 'message'
    return stream.type

def format_identity_with_stream_types(identity, streams):
    return '%s %s' % (format_identity(identity), format_stream_types(streams))

def format_session_duration(session):
    if session.start_time:
        duration = session.end_time - session.start_time
        seconds = duration.seconds if duration.microseconds < 500000 else duration.seconds+1
        minutes, seconds = seconds / 60, seconds % 60
        hours, minutes = minutes / 60, minutes % 60
        hours += duration.days*24
        if not minutes and not hours:
            duration_text = '%d seconds' % seconds
        elif not hours:
            duration_text = '%02d:%02d' % (minutes, seconds)
        else:
            duration_text = '%02d:%02d:%02d' % (hours, minutes, seconds)
    else:
        duration_text = '0s'
    return duration_text

def chunks(text, size):
    for i in xrange(0, len(text), size):
        yield text[i:i+size]


class SIPMessage(object):
    def __init__(self, sender, recipient, content_type, body):
        self.sender = sender
        self.recipient = recipient
        self.content_type = content_type
        self.body = body
        self.timestamp = None


class Room(object):
    """
    Object representing a conference room, it will handle the message dispatching
    among all the participants.
    """
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self, uri):
        self.uri = uri
        self.identity = CPIMIdentity.parse('<sip:%s>' % self.uri)
        self.sessions = []
        self.sessions_with_proposals = []
        self.subscriptions = []
        self.state = 'stopped'
        self.incoming_message_queue = coros.queue()
        self.message_dispatcher = None
        self.audio_conference = None
        self.moh_player = None
        self.conference_info_payload = None

    @classmethod
    def get_room(cls, uri):
        room_uri = '%s@%s' % (uri.user, uri.host)
        room = cls(room_uri)
        return room

    @property
    def empty(self):
        return len(self.sessions) == 0

    @property
    def started(self):
        return self.state == 'started'

    def start(self):
        if self.state != 'stopped':
            return
        self.message_dispatcher = proc.spawn(self._message_dispatcher)
        self.audio_conference = AudioConference()
        self.audio_conference.hold()
        self.moh_player = MoHPlayer(self.audio_conference)
        self.moh_player.initialize()
        self.state = 'started'

    def stop(self):
        if self.state != 'started':
            return
        self.state = 'stopped'
        self.message_dispatcher.kill(proc.ProcExit)
        self.moh_player = None
        self.audio_conference = None

    def _message_dispatcher(self):
        """Read from self.incoming_message_queue and dispatch the messages to other participants"""
        while True:
            session, message_type, data = self.incoming_message_queue.wait()
            if message_type == 'message':
                if data.timestamp is not None and isinstance(data.timestamp, Timestamp):
                    timestamp = datetime.fromtimestamp(mktime(data.timestamp.timetuple()))
                else:
                    timestamp = datetime.now()
                if data.sender.uri != session.remote_identity.uri:
                    return
                recipient = data.recipients[0]
                database.async_save_message(format_identity(session.remote_identity, True), self.uri, data.body, data.content_type, unicode(data.sender), unicode(recipient), timestamp)
                if recipient.uri == self.identity.uri:
                    self.dispatch_message(session, data)
                else:
                    self.dispatch_private_message(session, data)
            elif message_type == 'sip_message':
                database.async_save_message(format_identity(session.remote_identity, True), self.uri, data.body, data.content_type, unicode(data.sender), data.recipient, data.timestamp)
                self.dispatch_message(session, data)
            elif message_type == 'composing_indication':
                if data.sender.uri != session.remote_identity.uri:
                    return
                recipient = data.recipients[0]
                if recipient.uri == self.identity.uri:
                    self.dispatch_iscomposing(session, data)
                else:
                    self.dispatch_private_iscomposing(session, data)

    def dispatch_message(self, session, message):
        for s in (s for s in self.sessions if s is not session):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
                chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[self.identity], timestamp=message.timestamp)
            except ChatStreamError, e:
                log.error('Error dispatching message to %s: %s' % (s.remote_identity.uri, e))
            except StopIteration:
                # This session doesn't have a chat stream, send him a SIP MESSAGE
                if ConferenceConfig.enable_sip_message:
                    self.send_sip_message(session.remote_identity.uri, s.remote_identity.uri, message.content_type, message.body)

    def dispatch_private_message(self, session, message):
        # Private messages are delivered to all sessions matching the recipient but also to the sender,
        # for replication in clients
        recipient = message.recipients[0]
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri in (recipient.uri, session.remote_identity.uri)):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
                chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[recipient], timestamp=message.timestamp)
            except ChatStreamError, e:
                log.error('Error dispatching private message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_iscomposing(self, session, data):
        for s in (s for s in self.sessions if s is not session):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
                chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity, recipients=[self.identity])
            except ChatStreamError, e:
                log.error('Error dispatching composing indication to %s: %s' % (s.remote_identity.uri, e))
            except StopIteration:
                # This session doesn't have a chat stream, send him a SIP MESSAGE
                if ConferenceConfig.enable_sip_message:
                    body = IsComposingMessage(state=State(data.state), refresh=Refresh(data.refresh), last_active=LastActive(data.last_active or datetime.now()), content_type=ContentType('text')).toxml()
                    self.send_sip_message(session.remote_identity.uri, s.remote_identity.uri, IsComposingMessage.content_type, body)

    def dispatch_private_iscomposing(self, session, data):
        recipient_uri = data.recipients[0].uri
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri == recipient_uri):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
                chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity)
            except ChatStreamError, e:
                log.error('Error dispatching private composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_server_message(self, body, content_type='text/plain', exclude=None):
        # When message is sent it will be encoded in UTF-8, so we must provide a unicode object
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
                chat_stream.send_message(body, content_type, local_identity=self.identity, recipients=[self.identity])
            except StopIteration:
                # This session doesn't have a chat stream, send him a SIP MESSAGE
                if ConferenceConfig.enable_sip_message:
                    self.send_sip_message(self.identity.uri, session.remote_identity.uri, content_type, body)
        self_identity = format_identity(self.identity, cpim_format=True)
        database.async_save_message(self_identity, self.uri, body, content_type, self_identity, self_identity, datetime.now())

    def dispatch_conference_info(self):
        data = self.build_conference_info_payload()
        for subscription in (subscription for subscription in self.subscriptions if subscription.state == 'active'):
            try:
                subscription.push_content(Conference.content_type, data)
            except SIPCoreInvalidStateError:
                pass

    @run_in_green_thread
    def send_sip_message(self, from_uri, to_uri, content_type, body):
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        try:
            routes = lookup.lookup_sip_proxy(to_uri, settings.sip.transport_list).wait()
        except DNSLookupError:
            log.warning('DNS lookup error while looking for %s proxy' % to_uri)
        else:
            route = routes.pop(0)
            from_header = FromHeader(self.identity.uri)
            to_header = ToHeader(SIPURI.new(to_uri))
            route_header = RouteHeader(route.get_uri())
            sender = CPIMIdentity(from_uri)
            for chunk in chunks(body, 1000):
                msg = CPIMMessage(chunk, content_type, sender=sender, recipients=[self.identity])
                message_request = Message(from_header, to_header, route_header, 'message/cpim', str(msg))
                message_request.send()

    def render_text_welcome(self, session):
        txt = 'Welcome to the conference.'
        user_count = len(set(str(s.remote_identity.uri) for s in self.sessions) - set([str(session.remote_identity.uri)]))
        if user_count == 0:
            txt += ' You are the first participant in the room.'
        else:
            if user_count == 1:
                txt += ' There is one more participant in the room.'
            else:
                txt += ' There are %s more participants in the room.' % user_count
        return txt

    def _play_file_in_player(self, player, file, delay):
        player.filename = str(file)
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError, e:
            log.warning("Error playing file %s: %s" % (file, e))

    @run_in_green_thread
    def play_audio_welcome(self, session, play_welcome=True):
        audio_stream = (stream for stream in session.streams if stream.type == 'audio').next()
        player = WavePlayer(audio_stream.mixer, '', pause_time=1, initial_play=False, volume=50)
        audio_stream.bridge.add(player)
        if play_welcome:
            file = ResourcePath('sounds/co_welcome_conference.wav').normalized
            self._play_file_in_player(player, file, 1)
        user_count = len(set(str(s.remote_identity.uri) for s in self.sessions if any(stream for stream in s.streams if stream.type == 'audio')) - set([str(session.remote_identity.uri)]))
        if user_count == 0:
            file = ResourcePath('sounds/co_only_one.wav').normalized
            self._play_file_in_player(player, file, 0.5)
        elif user_count == 1:
            file = ResourcePath('sounds/co_there_is.wav').normalized
            self._play_file_in_player(player, file, 0.5)
        elif user_count < 100:
            file = ResourcePath('sounds/co_there_are.wav').normalized
            self._play_file_in_player(player, file, 0.2)
            if user_count <= 24:
                file = ResourcePath('sounds/bi_%d.wav' % user_count).normalized
                self._play_file_in_player(player, file, 0.1)
            else:
                file = ResourcePath('sounds/bi_%d0.wav' % (user_count / 10)).normalized
                self._play_file_in_player(player, file, 0.1)
                file = ResourcePath('sounds/bi_%d.wav' % (user_count % 10)).normalized
                self._play_file_in_player(player, file, 0.1)
            file = ResourcePath('sounds/co_more_participants.wav').normalized
            self._play_file_in_player(player, file, 0)
        audio_stream.bridge.remove(player)
        self.audio_conference.add(audio_stream)
        self.audio_conference.unhold()
        if len(self.audio_conference.streams) == 1:
            # Play MoH
            self.moh_player.play()
        else:
            self.moh_player.pause()

    def add_session(self, session):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        self.sessions.append(session)
        try:
            chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
        except StopIteration:
            pass
        else:
            notification_center.add_observer(self, sender=chat_stream)
            remote_identity = CPIMIdentity.parse(format_identity(session.remote_identity, cpim_format=True))
            # getting last messages may take time, so new messages can arrive before messages the last message from history
            for msg in database.get_last_messages(self.uri, ConferenceConfig.replay_history):
                recipient = CPIMIdentity.parse(msg.cpim_recipient)
                sender = CPIMIdentity.parse(msg.cpim_sender)
                if recipient.uri in (self.identity.uri, remote_identity.uri) or sender.uri == remote_identity.uri:
                    chat_stream.send_message(msg.cpim_body, msg.cpim_content_type, local_identity=sender, recipients=[recipient], timestamp=msg.cpim_timestamp)
        try:
            audio_stream = (stream for stream in session.streams if stream.type == 'audio').next()
        except StopIteration:
            pass
        else:
            notification_center.add_observer(self, sender=audio_stream)
            log.msg('Audio stream using %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                      'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                      audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                      audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
            self.play_audio_welcome(session)
        self.dispatch_conference_info()
        if len(self.sessions) == 1:
            log.msg('%s started conference %s %s' % (format_identity(session.remote_identity), self.uri, format_stream_types(session.streams)))
        else:
            log.msg('%s joined conference %s %s' % (format_identity(session.remote_identity), self.uri, format_stream_types(session.streams)))
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has joined the room %s' % (format_identity(session.remote_identity), format_stream_types(session.streams)), exclude=session)

    def remove_session(self, session):
        notification_center = NotificationCenter()
        try:
            chat_stream = (stream for stream in session.streams or [] if stream.type == 'chat').next()
        except StopIteration:
            pass
        else:
            notification_center.remove_observer(self, sender=chat_stream)
        try:
            audio_stream = (stream for stream in session.streams or [] if stream.type == 'audio').next()
        except StopIteration:
            pass
        else:
            notification_center.remove_observer(self, sender=audio_stream)
            try:
                self.audio_conference.remove(audio_stream)
            except ValueError:
                # User may hangup before getting bridged into the conference
                pass
            if len(self.audio_conference.streams) == 0:
                self.moh_player.pause()
                self.audio_conference.hold()
            elif len(self.audio_conference.streams) == 1:
                self.moh_player.play()
        notification_center.remove_observer(self, sender=session)
        self.sessions.remove(session)
        self.dispatch_conference_info()
        log.msg('%s left conference %s after %s' % (format_identity(session.remote_identity), self.uri, format_session_duration(session)))
        if not self.sessions:
            log.msg('Last participant left conference %s' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), format_session_duration(session)))

    def handle_incoming_sip_message(self, message_request, data):
        content_type = data.headers.get('Content-Type', Null)[0]
        from_header = data.headers.get('From', Null)
        if content_type is Null or from_header is Null:
            message_request.answer(400)
            return
        try:
            # Take the first session which doesn't have a chat stream. This is needed because the 
            # seession picked up here will later be ignored. It doesn't matter if we ignore a session
            # without a chat stream, because that means we will send SIP MESSAGE, and it will fork, so
            # everyone will get it.
            session = (session for session in self.sessions if str(session.remote_identity.uri) == str(from_header.uri) and any(stream for stream in session.streams if stream.type != 'chat')).next()
        except StopIteration:
            # MESSAGE from a user which is not in this room
            message_request.answer(503)
            return
        if content_type == 'message/cpim':
            try:
                message = CPIMMessage.parse(data.body)
            except CPIMParserError:
                message_request.answer(500)
                return
            else:
                body = message.body
                content_type = message.content_type
                sender = message.sender or format_identity(from_header, cpim_format=True)
                if message.timestamp is not None and isinstance(message.timestamp, Timestamp):
                    timestamp = datetime.fromtimestamp(mktime(message.timestamp.timetuple()))
                else:
                    timestamp = datetime.now()
        else:
            body = data.body
            sender = format_identity(from_header, cpim_format=True)
            timestamp = datetime.now()
        message_request.answer(200)

        if content_type == IsComposingMessage.content_type:
            return

        log.msg('New incoming MESSAGE from %s' % session.remote_identity.uri)
        self_identity = format_identity(self.identity, cpim_format=True)
        message = SIPMessage(sender=sender, recipient=self_identity, content_type=content_type, body=body)
        message.timestamp = timestamp
        self.incoming_message_queue.send((session, 'sip_message', message))

    def build_conference_info_payload(self):
        if self.conference_info_payload is None:
            settings = SIPSimpleSettings()
            conference_description = ConferenceDescription(display_text='Ad-hoc conference', free_text='Hosted by %s' % settings.user_agent)
            host_info = HostInfo(web_page=WebPage('http://sylkserver.com'))
            self.conference_info_payload = Conference(self.identity.uri, conference_description=conference_description, host_info=host_info, users=Users())
        user_count = len(set(str(s.remote_identity.uri) for s in self.sessions))
        self.conference_info_payload.conference_state = ConferenceState(user_count=user_count, active=True)
        users = Users()
        for session in self.sessions:
            try:
                display_name = session.remote_identity.display_name.decode('utf-8')
            except UnicodeDecodeError:
                display_name = u''
            try:
                user = (user for user in users if user.entity == str(session.remote_identity.uri)).next()
            except StopIteration:
                user = User(str(session.remote_identity.uri), display_text=display_name)
                users.append(user)
            joining_info = JoiningInfo(when=session.start_time)
            holdable_streams = [stream for stream in session.streams if stream.hold_supported]
            session_on_hold = holdable_streams and all(stream.on_hold_by_remote for stream in holdable_streams)
            hold_status = EndpointStatus('on-hold' if session_on_hold else 'connected')
            endpoint = Endpoint(str(session._invitation.remote_contact_header.uri), display_text=display_name, joining_info=joining_info, status=hold_status)
            for stream in session.streams:
                endpoint.append(Media(id(stream), media_type=format_conference_stream_type(stream)))
            user.append(endpoint)
        self.conference_info_payload.users = users
        return self.conference_info_payload.toxml()

    def handle_incoming_subscription(self, subscribe_request, data):
        content = self.build_conference_info_payload()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=subscribe_request)
        subscribe_request.accept(Conference.content_type, content)
        self.subscriptions.append(subscribe_request)

    def accept_proposal(self, session, streams):
        if session in self.sessions_with_proposals:
            session.accept_proposal(streams)
            self.sessions_with_proposals.remove(session)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AudioStreamDidTimeout(self, notification):
        stream = notification.sender
        session = stream._session
        log.msg('Audio stream for session %s timed out' % format_identity(session.remote_identity))
        if session.streams == [stream]:
            session.end()

    def _NH_ChatStreamGotMessage(self, notification):
        data = notification.data.message
        session = notification.sender.session
        self.incoming_message_queue.send((session, 'message', data))

    def _NH_ChatStreamGotComposingIndication(self, notification):
        data = notification.data
        session = notification.sender.session
        self.incoming_message_queue.send((session, 'composing_indication', data))

    def _NH_SIPIncomingSubscriptionDidEnd(self, notification):
        subscription = notification.sender
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=subscription)
        self.subscriptions.remove(subscription)

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        session = notification.sender
        if notification.data.originator == 'remote':
            if notification.data.on_hold:
                log.msg('%s has put the audio session on hold' % format_identity(session.remote_identity))
            else:
                log.msg('%s has taken the audio session out of hold' % format_identity(session.remote_identity))
            self.dispatch_conference_info()

    def _NH_SIPSessionGotProposal(self, notification):
        session = notification.sender
        audio_streams = [stream for stream in notification.data.streams if stream.type=='audio']
        chat_streams = [stream for stream in notification.data.streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            session.reject_proposal()
            return
        streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
        self.sessions_with_proposals.append(session)
        reactor.callLater(4, self.accept_proposal, session, streams)

    def _NH_SIPSessionGotRejectProposal(self, notification):
        session = notification.sender
        self.sessions_with_proposals.remove(session)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        notification_center = NotificationCenter()
        session = notification.sender
        streams = notification.data.streams
        if notification.data.action == 'add':
            try:
                chat_stream = (stream for stream in streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                notification_center.add_observer(self, sender=chat_stream)
                remote_identity = CPIMIdentity.parse(format_identity(session.remote_identity, cpim_format=True))
                # getting last messages may take time, so new messages can arrive before messages the last message from history
                for msg in database.get_last_messages(self.uri, ConferenceConfig.replay_history):
                    recipient = CPIMIdentity.parse(msg.cpim_recipient)
                    sender = CPIMIdentity.parse(msg.cpim_sender)
                    if recipient.uri in (self.identity.uri, remote_identity.uri) or sender.uri == remote_identity.uri:
                        chat_stream.send_message(msg.cpim_body, msg.cpim_content_type, local_identity=sender, recipients=[recipient], timestamp=msg.cpim_timestamp)
                log.msg('%s has added chat to %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has added chat' % format_identity(session.remote_identity), exclude=session)
            try:
                audio_stream = (stream for stream in streams if stream.type == 'audio').next()
            except StopIteration:
                pass
            else:
                notification_center.add_observer(self, sender=audio_stream)
                log.msg('Audio stream using %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                          'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                          audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                          audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
                log.msg('%s has added audio to %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has added audio' % format_identity(session.remote_identity), exclude=session)
                self.play_audio_welcome(session, False)
        elif notification.data.action == 'remove':
            try:
                chat_stream = (stream for stream in streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                notification_center.remove_observer(self, sender=chat_stream)
                log.msg('%s has removed chat from %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has removed chat' % format_identity(session.remote_identity), exclude=session)
            try:
                audio_stream = (stream for stream in streams if stream.type == 'audio').next()
            except StopIteration:
                pass
            else:
                notification_center.remove_observer(self, sender=audio_stream)
                try:
                    self.audio_conference.remove(audio_stream)
                except ValueError:
                    # User may hangup before getting bridged into the conference
                    pass
                if len(self.audio_conference.streams) == 0:
                    self.moh_player.pause()
                    self.audio_conference.hold()
                elif len(self.audio_conference.streams) == 1:
                    self.moh_player.play()
                log.msg('%s has removed audio from %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has removed audio' % format_identity(session.remote_identity), exclude=session)
            if not session.streams:
                log.msg('%s has removed all streams from %s, session will be terminated' % (format_identity(session.remote_identity), self.uri))
                session.end()
        self.dispatch_conference_info()


class MoHPlayer(object):
    implements(IObserver)

    def __init__(self, conference):
        self.conference = conference
        self.disabled = False
        self.files = None
        self.paused = False
        self._player = None

    def initialize(self):
        files = glob('%s/*.wav' % ResourcePath('sounds/moh').normalized)
        if not files:
            log.error('No files found, MoH is disabled')
            self.disabled = True
            return
        random.shuffle(files)
        self.files = cycle(files)
        self._player = WavePlayer(SIPApplication.voice_audio_mixer, '', pause_time=1, initial_play=False, volume=20)
        self.conference.bridge.add(self._player)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._player)

    def stop(self):
        if self.disabled:
            return
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._player)
        self.conference.bridge.remove(self, self._player)
        self._player.stop()
        self._player = None

    def play(self):
        if not self.disabled:
            self.paused = False
            self._play_next_file()
            log.msg('Started playing music on hold')

    def pause(self):
        if not self.disabled:
            self.paused = True
            self._player.stop()
            log.msg('Stopped playing music on hold')

    def _play_next_file(self):
        file = self.files.next()
        self._player.filename = str(file)
        self._player.play()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_WavePlayerDidFail(self, notification):
        if not self.paused:
            self._play_next_file()

    def _NH_WavePlayerDidEnd(self, notification):
        if not self.paused:
            self._play_next_file()

