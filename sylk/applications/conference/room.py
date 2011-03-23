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
from itertools import chain
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPCoreInvalidStateError
from sipsimple.payloads.conference import Conference, ConferenceDescription, ConferenceState, Endpoint, EndpointStatus, HostInfo, JoiningInfo, Media, User, Users, WebPage
from sipsimple.streams.applications.chat import CPIMIdentity
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
        return u'%s <sip:%s@%s>' % (identity.display_name, uri.user, uri.host)
    elif cpim_format:
        return u'<sip:%s@%s>' % (uri.user, uri.host)
    else:
        return u'sip:%s@%s' % (uri.user, uri.host)

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

    @property
    def active_media(self):
        return set((stream.type for stream in chain(*(session.streams for session in self.sessions if session.streams))))

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
        self.moh_player.stop()
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
            except StopIteration:
                pass
            else:
                try:
                    chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[self.identity], timestamp=message.timestamp)
                except ChatStreamError, e:
                    log.error(u'Error dispatching message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_private_message(self, session, message):
        # Private messages are delivered to all sessions matching the recipient but also to the sender,
        # for replication in clients
        recipient = message.recipients[0]
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri in (recipient.uri, session.remote_identity.uri)):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                continue
            else:
                try:
                    chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[recipient], timestamp=message.timestamp)
                except ChatStreamError, e:
                    log.error(u'Error dispatching private message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_iscomposing(self, session, data):
        for s in (s for s in self.sessions if s is not session):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                try:
                    chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity, recipients=[self.identity])
                except ChatStreamError, e:
                    log.error(u'Error dispatching composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_private_iscomposing(self, session, data):
        recipient_uri = data.recipients[0].uri
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri == recipient_uri):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
            except StopIteration:
                continue
            else:
                try:
                    chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity)
                except ChatStreamError, e:
                    log.error(u'Error dispatching private composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_server_message(self, body, content_type='text/plain', exclude=None):
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                chat_stream.send_message(body, content_type, local_identity=self.identity, recipients=[self.identity])
        self_identity = format_identity(self.identity, cpim_format=True)
        database.async_save_message(self_identity, self.uri, body, content_type, self_identity, self_identity, datetime.now())

    def dispatch_conference_info(self):
        data = self.build_conference_info_payload()
        for subscription in (subscription for subscription in self.subscriptions if subscription.state == 'active'):
            try:
                subscription.push_content(Conference.content_type, data)
            except (SIPCoreError, SIPCoreInvalidStateError):
                pass

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
        player.filename = file
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError, e:
            log.warning(u"Error playing file %s: %s" % (file, e))

    @run_in_green_thread
    def play_audio_welcome(self, session, welcome_prompt=True):
        audio_stream = (stream for stream in session.streams if stream.type == 'audio').next()
        player = WavePlayer(audio_stream.mixer, '', pause_time=1, initial_play=False, volume=50)
        audio_stream.bridge.add(player)
        if welcome_prompt:
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
        file = ResourcePath('sounds/connected_tone.wav').normalized
        self._play_file_in_player(player, file, 0.1)
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
            log.msg(u'Audio stream using %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                      'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                      audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                      audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
            self.play_audio_welcome(session)
        self.dispatch_conference_info()
        if len(self.sessions) == 1:
            log.msg(u'%s started conference %s %s' % (format_identity(session.remote_identity), self.uri, format_stream_types(session.streams)))
        else:
            log.msg(u'%s joined conference %s %s' % (format_identity(session.remote_identity), self.uri, format_stream_types(session.streams)))
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
        log.msg(u'%s left conference %s after %s' % (format_identity(session.remote_identity), self.uri, format_session_duration(session)))
        if not self.sessions:
            log.msg(u'Last participant left conference %s' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), format_session_duration(session)))

    def terminate_sessions(self, uri):
        if not self.started:
            return
        for session in (session for session in self.sessions if session.remote_identity.uri == uri):
            session.end()

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
                user = (user for user in users if user.entity == str(session.remote_identity.uri)).next()
            except StopIteration:
                user = User(str(session.remote_identity.uri), display_text=session.remote_identity.display_name)
                users.append(user)
            joining_info = JoiningInfo(when=session.start_time)
            holdable_streams = [stream for stream in session.streams if stream.hold_supported]
            session_on_hold = holdable_streams and all(stream.on_hold_by_remote for stream in holdable_streams)
            hold_status = EndpointStatus('on-hold' if session_on_hold else 'connected')
            endpoint = Endpoint(str(session._invitation.remote_contact_header.uri), display_text=session.remote_identity.display_name, joining_info=joining_info, status=hold_status)
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
        log.msg(u'Audio stream for session %s timed out' % format_identity(session.remote_identity))
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
                log.msg(u'%s has put the audio session on hold' % format_identity(session.remote_identity))
            else:
                log.msg(u'%s has taken the audio session out of hold' % format_identity(session.remote_identity))
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
                log.msg(u'%s has added chat to %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has added chat' % format_identity(session.remote_identity), exclude=session)
            try:
                audio_stream = (stream for stream in streams if stream.type == 'audio').next()
            except StopIteration:
                pass
            else:
                notification_center.add_observer(self, sender=audio_stream)
                log.msg(u'Audio stream using %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                          'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                          audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                          audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
                log.msg(u'%s has added audio to %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has added audio' % format_identity(session.remote_identity), exclude=session)
                self.play_audio_welcome(session, False)
        elif notification.data.action == 'remove':
            try:
                chat_stream = (stream for stream in streams if stream.type == 'chat').next()
            except StopIteration:
                pass
            else:
                notification_center.remove_observer(self, sender=chat_stream)
                log.msg(u'%s has removed chat from %s' % (format_identity(session.remote_identity), self.uri))
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
                log.msg(u'%s has removed audio from %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has removed audio' % format_identity(session.remote_identity), exclude=session)
            if not session.streams:
                log.msg(u'%s has removed all streams from %s, session will be terminated' % (format_identity(session.remote_identity), self.uri))
                session.end()
        self.dispatch_conference_info()


class MoHPlayer(object):
    implements(IObserver)

    def __init__(self, conference):
        self.conference = conference
        self.files = None
        self.paused = True
        self._player = None

    def initialize(self):
        files = glob('%s/*.wav' % ResourcePath('sounds/moh').normalized)
        if not files:
            log.error(u'No files found, MoH is disabled')
            return
        random.shuffle(files)
        self.files = cycle(files)
        self._player = WavePlayer(SIPApplication.voice_audio_mixer, '', pause_time=1, initial_play=False, volume=20)
        self.conference.bridge.add(self._player)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._player)

    def stop(self):
        if self._player is None:
            return
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._player)
        self.conference.bridge.remove(self._player)
        self.conference = None
        self._player.stop()
        self._player = None

    def play(self):
        if self._player is not None and self.paused:
            self.paused = False
            self._play_next_file()
            log.msg(u'Started playing music on hold')

    def pause(self):
        if self._player is not None and not self.paused:
            self.paused = True
            self._player.stop()
            log.msg(u'Stopped playing music on hold')

    def _play_next_file(self):
        self._player.filename = self.files.next()
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

