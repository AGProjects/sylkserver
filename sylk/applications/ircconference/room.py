# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

import random

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python.util import Null, Singleton
from eventlet import coros, proc
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreInvalidStateError
from sipsimple.payloads.conference import Conference, ConferenceDescription, ConferenceState, Endpoint, EndpointStatus, HostInfo, JoiningInfo, Media, User, Users, WebPage
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.streams.msrp import ChatStreamError
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import protocol, reactor
from twisted.words.protocols import irc
from zope.interface import implements

from sylk.applications.ircconference.configuration import get_room_configuration
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

def format_conference_stream_type(stream):
    if stream.type == 'chat':
        return 'message'
    return stream.type


class IRCMessage(object):
    def __init__(self, username, uri, body, content_type='text/plain'):
        self.sender = CPIMIdentity(uri, display_name=username)
        self.body = body
        self.content_type = content_type


class IRCRoom(object):
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
        self.pending_messages = []
        self.state = 'stopped'
        self.incoming_message_queue = coros.queue()
        self.message_dispatcher = None
        self.audio_conference = None
        self.conference_info_payload = None
        self.irc_connector = None
        self.irc_protocol = None

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
        config = get_room_configuration(self.uri.split('@')[0])
        factory = IRCBotFactory(config)
        host, port = config.server
        self.irc_connector = reactor.connectTCP(host, port, factory)
        NotificationCenter().add_observer(self, sender=self.irc_connector.factory)
        self.message_dispatcher = proc.spawn(self._message_dispatcher)
        self.audio_conference = AudioConference()
        self.audio_conference.hold()
        self.state = 'started'

    def stop(self):
        if self.state != 'started':
            return
        self.state = 'stopped'
        NotificationCenter().remove_observer(self, sender=self.irc_connector.factory)
        self.irc_connector.factory.stop_requested = True
        self.irc_connector.disconnect()
        self.irc_connector = None
        self.message_dispatcher.kill(proc.ProcExit)
        self.moh_player = None
        self.audio_conference = None

    def _message_dispatcher(self):
        """Read from self.incoming_message_queue and dispatch the messages to other participants"""
        while True:
            session, message_type, data = self.incoming_message_queue.wait()
            if message_type == 'msrp_message':
                if data.sender.uri != session.remote_identity.uri:
                    return
                self.dispatch_message(session, data)
            elif message_type == 'irc_message':
                self.dispatch_irc_message(data)

    def dispatch_message(self, session, message):
        for s in (s for s in self.sessions if s is not session):
            try:
                identity = CPIMIdentity.parse(format_identity(session.remote_identity, True))
                chat_stream = (stream for stream in s.streams if stream.type == 'chat').next()
                chat_stream.send_message(message.body, message.content_type, local_identity=identity, recipients=[self.identity], timestamp=message.timestamp)
            except ChatStreamError, e:
                log.error(u'Error dispatching message to %s: %s' % (s.remote_identity.uri, e))
            except StopIteration:
                pass

    def dispatch_irc_message(self, message):
        for session in self.sessions:
            try:
                chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
                chat_stream.send_message(message.body, message.content_type, local_identity=message.sender, recipients=[self.identity])
            except ChatStreamError, e:
                log.error(u'Error dispatching message to %s: %s' % (session.remote_identity.uri, e))
            except StopIteration:
                pass

    def dispatch_server_message(self, body, content_type='text/plain', exclude=None):
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
                chat_stream.send_message(body, content_type, local_identity=self.identity, recipients=[self.identity])
            except ChatStreamError, e:
                log.error(u'Error dispatching message to %s: %s' % (session.remote_identity.uri, e))
            except StopIteration:
                pass

    def get_conference_info(self):
        # Send request to get participants list, we'll get a notification with it
        if self.irc_protocol is not None:
            self.irc_protocol.get_participants()
        else:
            self.dispatch_conference_info([])

    def dispatch_conference_info(self, irc_participants):
        data = self.build_conference_info_payload(irc_participants)
        for subscription in (subscription for subscription in self.subscriptions if subscription.state == 'active'):
           try:
               subscription.push_content(Conference.content_type, data)
           except SIPCoreInvalidStateError:
               pass

    def build_conference_info_payload(self, irc_participants):
        irc_configuration = get_room_configuration(self.uri.split('@')[0])
        if self.conference_info_payload is None:
            settings = SIPSimpleSettings()
            conference_description = ConferenceDescription(display_text='#%s on %s' % (irc_configuration.channel, irc_configuration.server[0]), free_text='Hosted by %s' % settings.user_agent)
            host_info = HostInfo(web_page=WebPage(irc_configuration.website))
            self.conference_info_payload = Conference(self.identity.uri, conference_description=conference_description, host_info=host_info, users=Users())
        user_count = len(set(str(s.remote_identity.uri) for s in self.sessions)) + len(irc_participants)
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
        for nick in irc_participants:
            irc_uri = '%s@%s' % (nick, irc_configuration.server[0])
            user = User(irc_uri, display_text=nick)
            users.append(user)
            endpoint = Endpoint(irc_uri, display_text=nick)
            endpoint.append(Media(random.randint(100000000, 999999999), media_type='message'))
            user.append(endpoint)
        self.conference_info_payload.users = users
        return self.conference_info_payload.toxml()

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
        self.get_conference_info()
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
                self.audio_conference.hold()
        notification_center.remove_observer(self, sender=session)
        self.sessions.remove(session)
        self.get_conference_info()
        log.msg(u'%s left conference %s after %s' % (format_identity(session.remote_identity), self.uri, format_session_duration(session)))
        if not self.sessions:
            log.msg(u'Last participant left conference %s' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), format_session_duration(session)))

    def accept_proposal(self, session, streams):
        if session in self.sessions_with_proposals:
            session.accept_proposal(streams)
            self.sessions_with_proposals.remove(session)

    def _play_file_in_player(self, player, file, delay):
        player.filename = file
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError, e:
            log.warning(u"Error playing file %s: %s" % (file, e))

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

    def handle_incoming_subscription(self, subscribe_request, data):
        NotificationCenter().add_observer(self, sender=subscribe_request)
        subscribe_request.accept()
        self.subscriptions.append(subscribe_request)
        self.get_conference_info()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

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
            self.get_conference_info()

    def _NH_SIPSessionGotProposal(self, notification):
        session = notification.sender
        audio_streams = [stream for stream in notification.data.streams if stream.type=='audio']
        chat_streams = [stream for stream in notification.data.streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            session.reject_proposal()
            return
        if chat_streams:
            chat_streams[0].chatroom_capabilities = []
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
                    self.audio_conference.hold()
                log.msg(u'%s has removed audio from %s' % (format_identity(session.remote_identity), self.uri))
                self.dispatch_server_message('%s has removed audio' % format_identity(session.remote_identity), exclude=session)
            if not session.streams:
                log.msg(u'%s has removed all streams from %s, session will be terminated' % (format_identity(session.remote_identity), self.uri))
                session.end()
        self.get_conference_info()

    def _NH_AudioStreamDidTimeout(self, notification):
        stream = notification.sender
        session = stream._session
        log.msg(u'Audio stream for session %s timed out' % format_identity(session.remote_identity))
        if session.streams == [stream]:
            session.end()

    def _NH_ChatStreamGotMessage(self, notification):
        # Send MSRP chat message to other participants
        message = notification.data.message
        session = notification.sender.session
        self.incoming_message_queue.send((session, 'msrp_message', message))
        # Send MSRP chat message to IRC chat room
        body = message.body
        sender = message.sender
        irc_message = '%s: %s' % (format_identity(sender), body)
        if self.irc_protocol is not None:
            self.irc_protocol.send_message(irc_message.encode('utf-8'))
        else:
            self.pending_messages.append(irc_message)

    def _NH_IRCBotGotConnected(self, notification):
        self.irc_protocol = notification.data.protocol
        # Send enqueued messages
        while self.pending_messages:
            message = self.pending_messages.pop(0)
            self.irc_protocol.send_message(message.encode('utf-8'))
        # Update participants list
        self.get_conference_info()

    def _NH_IRCBotGotDisconnected(self, notification):
        self.irc_protocol = None

    def _NH_IRCBotGotMessage(self, notification):
        message = notification.data.message
        self.incoming_message_queue.send((None, 'irc_message', message))

    def _NH_IRCBotGotParticipantsList(self, notification):
        self.dispatch_conference_info(notification.data.participants)

    def _NH_IRCBotJoinedChannel(self, notification):
        self.get_conference_info()

    def _NH_IRCBotUserJoined(self, notification):
        self.dispatch_server_message('%s joined the IRC channel' % notification.data.user)
        self.get_conference_info()

    def _NH_IRCBotUserLeft(self, notification):
        self.dispatch_server_message('%s left the IRC channel' % notification.data.user)
        self.get_conference_info()

    def _NH_IRCBotUserQuit(self, notification):
        self.dispatch_server_message('%s quit the IRC channel: %s' % (notification.data.user, notification.data.reason))
        self.get_conference_info()

    def _NH_IRCBotUserKicked(self, notification):
        data = notification.data
        self.dispatch_server_message('%s kicked %s out of the IRC channel: %s' % (data.kicker, data.kickee, data.reason))
        self.get_conference_info()

    def _NH_IRCBotUserRenamed(self, notification):
        self.dispatch_server_message('%s changed his name to %s' % (notification.data.oldname, notification.data.newname))
        self.get_conference_info()

    def _NH_IRCBotUserAction(self, notification):
        self.dispatch_server_message('%s %s' % (notification.data.user, notification.data.action))


class IRCBot(irc.IRCClient):

    nickname = 'SylkServer'

    def __init__(self):
        self._nick_collector = []
        self.nicks = []

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)
        log.msg('Connection to IRC has been established')
        NotificationCenter().post_notification('IRCBotGotConnected', self.factory, NotificationData(protocol=self))

    def connectionLost(self, failure):
        irc.IRCClient.connectionLost(self, failure)
        NotificationCenter().post_notification('IRCBotGotDisconnected', self.factory, NotificationData())

    def signedOn(self):
        log.msg('Logging into %s channel...' % self.factory.channel)
        self.join(self.factory.channel) 

    def kickedFrom(self, channel, kicker, message):
        log.msg('Got kicked from %s by %s: %s. Rejoining...' % (channel, kicker, message))
        self.join(self.factory.channel)

    def joined(self, channel):
        log.msg('Logged into %s channel' % channel)
        NotificationCenter().post_notification('IRCBotJoinedChannel', self.factory, NotificationData(channel=self.factory.channel))

    def privmsg(self, user, channel, message):
        if channel == '*':
            return
        username = user.split('!', 1)[0]
        if username == self.nickname:
            return
        if channel == self.nickname:
            self.msg(username, "Sorry, I don't support private messages, I'm a bot.")
            return
        uri = SIPURI.parse('sip:%s@%s' % (username, self.factory.config.server[0]))
        irc_message = IRCMessage(username, uri, message.decode('utf-8'))
        data = NotificationData(message=irc_message)
        NotificationCenter().post_notification('IRCBotGotMessage', self.factory, data)

    def send_message(self, message):
        self.say(self.factory.channel, message)

    def get_participants(self):
        self.sendLine("NAMES #%s" % self.factory.channel)

    def got_participants(self, nicks):
        data = NotificationData(participants=nicks)
        NotificationCenter().post_notification('IRCBotGotParticipantsList', self.factory, data)

    def irc_RPL_NAMREPLY(self, prefix, params):
        """Collect usernames from this channel. Several of these
           messages may be sent to cover the channel's full nicklist.
           An RPL_ENDOFNAMES signals the end of the list.
           """
        # We just separate these into individual nicks and stuff them in
        # the nickCollector, transferred to 'nicks' when we get the RPL_ENDOFNAMES.
        for name in params[3].split():
            # Remove operator and voice prefixes
            if name[0] in '@+':
                name = name[1:]
            if name != self.nickname:
                self._nick_collector.append(name)

    def irc_RPL_ENDOFNAMES(self, prefix, params):
        """This is sent after zero or more RPL_NAMREPLY commands to
           terminate the list of users in a channel.
           """
        self.nicks = self._nick_collector
        self._nick_collector = []
        self.got_participants(self.nicks)

    def userJoined(self, user, channel):
        if channel.strip('#') == self.factory.channel:
            data = NotificationData(user=user)
            NotificationCenter().post_notification('IRCBotUserJoined', self.factory, data)

    def userLeft(self, user, channel):
        if channel.strip('#') == self.factory.channel:
            data = NotificationData(user=user)
            NotificationCenter().post_notification('IRCBotUserLeft', self.factory, data)

    def userQuit(self, user, reason):
        data = NotificationData(user=user, reason=reason)
        NotificationCenter().post_notification('IRCBotUserQuit', self.factory, data)

    def userKicked(self, kickee, channel, kicker, message):
        if channel.strip('#') == self.factory.channel:
            data = NotificationData(kickee=kickee, kicker=kicker, reason=message)
            NotificationCenter().post_notification('IRCBotUserKicked', self.factory, data)

    def userRenamed(self, oldname, newname):
        data = NotificationData(oldname=oldname, newname=newname)
        NotificationCenter().post_notification('IRCBotUserRenamed', self.factory, data)

    def action(self, user, channel, data):
        if channel.strip('#') == self.factory.channel:
            username = user.split('!', 1)[0]
            data = NotificationData(user=username, action=data)
            NotificationCenter().post_notification('IRCBotUserAction', self.factory, data)


class IRCBotFactory(protocol.ClientFactory):

    protocol = IRCBot

    def __init__(self, config):
        self.config = config
        self.channel = config.channel
        self.stop_requested = False

    def clientConnectionLost(self, connector, failure):
        log.msg('Disconnected from IRC: %s' % failure.getErrorMessage())
        if not self.stop_requested:
            log.msg('Reconnecting...')
            connector.connect()

    def clientConnectionFailed(self, connector, failure):
        log.error('Connection to IRC server failed: %s' % failure.getErrorMessage())


