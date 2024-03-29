
import random
import urllib.request, urllib.parse, urllib.error

import lxml.html
import lxml.html.clean

from itertools import count

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from eventlib import coros, proc
from sipsimple.audio import AudioConference, WavePlayer, WavePlayerError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError, SIPCoreInvalidStateError
from sipsimple.payloads.conference import Conference, ConferenceDocument, ConferenceDescription, ConferenceState, Endpoint, EndpointStatus, HostInfo, JoiningInfo, Media, User, Users, WebPage
from sipsimple.streams.msrp.chat import ChatIdentity
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import protocol, reactor
from twisted.words.protocols import irc
from zope.interface import implementer

from sylk.applications.ircconference.configuration import get_room_configuration
from sylk.applications.ircconference.logger import log
from sylk.resources import Resources


def format_identity(identity):
    uri = identity.uri
    if identity.display_name:
        return '%s <sip:%s@%s>' % (identity.display_name, uri.user, uri.host)
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

def html2text(data):
    try:
        doc = lxml.html.document_fromstring(data)
        cleaner = lxml.html.clean.Cleaner(style=True)
        doc = cleaner.clean_html(doc)
        return doc.text_content().strip('\n')
    except Exception:
        return ''


class IRCMessage(object):
    def __init__(self, username, uri, content, content_type='text/plain'):
        self.sender = ChatIdentity(uri, display_name=username)
        self.content = content
        self.content_type = content_type


@implementer(IObserver)
class IRCRoom(object, metaclass=Singleton):
    """
    Object representing a conference room, it will handle the message dispatching
    among all the participants.
    """

    def __init__(self, uri):
        self.uri = uri
        self.identity = ChatIdentity.parse('<sip:%s>' % self.uri)
        self.sessions = []
        self.sessions_with_proposals = []
        self.subscriptions = []
        self.pending_messages = []
        self.state = 'stopped'
        self.incoming_message_queue = coros.queue()
        self.message_dispatcher = None
        self.audio_conference = None
        self.conference_info_payload = None
        self.conference_info_version = count(1)
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
        identity = ChatIdentity.parse(format_identity(session.remote_identity))
        for s in (s for s in self.sessions if s is not session):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                pass
            else:
                chat_stream.send_message(message.content, message.content_type, sender=identity, recipients=[self.identity], timestamp=message.timestamp)

    def dispatch_irc_message(self, message):
        for session in self.sessions:
            try:
                chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
            except StopIteration:
                pass
            else:
                chat_stream.send_message(message.content, message.content_type, sender=message.sender, recipients=[self.identity])

    def dispatch_server_message(self, content, content_type='text/plain', exclude=None):
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
            except StopIteration:
                pass
            else:
                chat_stream.send_message(content, content_type, sender=self.identity, recipients=[self.identity])

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
               subscription.push_content(ConferenceDocument.content_type, data)
           except (SIPCoreError, SIPCoreInvalidStateError):
               pass

    def build_conference_info_payload(self, irc_participants):
        irc_configuration = get_room_configuration(self.uri.split('@')[0])
        if self.conference_info_payload is None:
            settings = SIPSimpleSettings()
            conference_description = ConferenceDescription(display_text='#%s on %s' % (irc_configuration.channel, irc_configuration.server[0]), free_text='Hosted by %s' % settings.user_agent)
            host_info = HostInfo(web_page=WebPage(irc_configuration.website))
            self.conference_info_payload = Conference(self.identity.uri, conference_description=conference_description, host_info=host_info, users=Users())
        self.conference_info_payload.version = next(self.conference_info_version)
        user_count = len({str(s.remote_identity.uri) for s in self.sessions}) + len(irc_participants)
        self.conference_info_payload.conference_state = ConferenceState(user_count=user_count, active=True)
        users = Users()
        for session in self.sessions:
            try:
                user = next(user for user in users if user.entity == str(session.remote_identity.uri))
            except StopIteration:
                user = User(str(session.remote_identity.uri), display_text=session.remote_identity.display_name)
                users.add(user)
            joining_info = JoiningInfo(when=session.start_time)
            holdable_streams = [stream for stream in session.streams if stream.hold_supported]
            session_on_hold = holdable_streams and all(stream.on_hold_by_remote for stream in holdable_streams)
            hold_status = EndpointStatus('on-hold' if session_on_hold else 'connected')
            endpoint = Endpoint(str(session._invitation.remote_contact_header.uri), display_text=session.remote_identity.display_name, joining_info=joining_info, status=hold_status)
            for stream in session.streams:
                endpoint.add(Media(id(stream), media_type=format_conference_stream_type(stream)))
            user.add(endpoint)
        for nick in irc_participants:
            irc_uri = '%s@%s' % (urllib.parse.quote(nick), irc_configuration.server[0])
            user = User(irc_uri, display_text=nick)
            users.add(user)
            endpoint = Endpoint(irc_uri, display_text=nick)
            endpoint.add(Media(random.randint(100000000, 999999999), media_type='message'))
            user.add(endpoint)
        self.conference_info_payload.users = users
        return self.conference_info_payload.toxml()

    def add_session(self, session):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        self.sessions.append(session)
        try:
            chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
        except StopIteration:
            pass
        else:
            notification_center.add_observer(self, sender=chat_stream)
        try:
            audio_stream = next(stream for stream in session.streams if stream.type == 'audio')
        except StopIteration:
            pass
        else:
            notification_center.add_observer(self, sender=audio_stream)
            log.info('Audio stream using %s/%sHz, end-points: %s:%d <-> %s:%d' % (audio_stream.codec, audio_stream.sample_rate,
                                                                                  audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                  audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
            welcome_handler = WelcomeHandler(self, session)
            welcome_handler.start()
        self.get_conference_info()
        if len(self.sessions) == 1:
            log.info('%s started conference %s %s' % (format_identity(session.remote_identity), self.uri, format_stream_types(session.streams)))
        else:
            log.info('%s joined conference %s %s' % (format_identity(session.remote_identity), self.uri, format_stream_types(session.streams)))
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has joined the room %s' % (format_identity(session.remote_identity), format_stream_types(session.streams)), exclude=session)

    def remove_session(self, session):
        notification_center = NotificationCenter()
        try:
            chat_stream = next(stream for stream in session.streams or [] if stream.type == 'chat')
        except StopIteration:
            pass
        else:
            notification_center.remove_observer(self, sender=chat_stream)
        try:
            audio_stream = next(stream for stream in session.streams or [] if stream.type == 'audio')
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
        log.info('%s left conference %s after %s' % (format_identity(session.remote_identity), self.uri, format_session_duration(session)))
        if not self.sessions:
            log.info('Last participant left conference %s' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), format_session_duration(session)))

    def accept_proposal(self, session, streams):
        if session in self.sessions_with_proposals:
            session.accept_proposal(streams)
            self.sessions_with_proposals.remove(session)

    def handle_incoming_subscription(self, subscribe_request, data):
        if subscribe_request.event != 'conference':
            subscribe_request.reject(489)
            return
        NotificationCenter().add_observer(self, sender=subscribe_request)
        self.subscriptions.append(subscribe_request)
        try:
            subscribe_request.accept()
        except SIPCoreError as e:
            log.warning('Error accepting SIP subscription: %s' % e)
            subscribe_request.end()
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
                log.info('%s has put the audio session on hold' % format_identity(session.remote_identity))
            else:
                log.info('%s has taken the audio session out of hold' % format_identity(session.remote_identity))
            self.get_conference_info()

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            audio_streams = [stream for stream in notification.data.proposed_streams if stream.type=='audio']
            chat_streams = [stream for stream in notification.data.proposed_streams if stream.type=='chat']
            if not audio_streams and not chat_streams:
                session.reject_proposal()
                return
            if chat_streams:
                chat_streams[0].chatroom_capabilities = []
            streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
            self.sessions_with_proposals.append(session)
            reactor.callLater(4, self.accept_proposal, session, streams)

    def _NH_SIPSessionProposalRejected(self, notification):
        session = notification.sender
        self.sessions_with_proposals.remove(session)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        session = notification.sender
        for stream in notification.data.added_streams:
            notification.center.add_observer(self, sender=stream)
            log.info('%s has added %s to %s' % (format_identity(session.remote_identity), stream.type, self.uri))
            self.dispatch_server_message('%s has added %s' % (format_identity(session.remote_identity), stream.type), exclude=session)
            if stream.type == 'audio':
                log.info('Audio stream using %s/%sHz, end-points: %s:%d <-> %s:%d' % (stream.codec, stream.sample_rate,
                                                                                      stream.local_rtp_address, stream.local_rtp_port,
                                                                                      stream.remote_rtp_address, stream.remote_rtp_port))
                welcome_handler = WelcomeHandler(self, session)
                welcome_handler.start(welcome_prompt=False)

        for stream in notification.data.removed_streams:
            notification.center.remove_observer(self, sender=stream)
            log.info('%s has removed %s from %s' % (format_identity(session.remote_identity), stream.type, self.uri))
            self.dispatch_server_message('%s has removed %s' % (format_identity(session.remote_identity), stream.type), exclude=session)
            if stream.type == 'audio':
                try:
                    self.audio_conference.remove(stream)
                except ValueError:
                    # User may hangup before getting bridged into the conference
                    pass
                if len(self.audio_conference.streams) == 0:
                    self.audio_conference.hold()
            if not session.streams:
                log.info('%s has removed all streams from %s, session will be terminated' % (format_identity(session.remote_identity), self.uri))
                session.end()
        self.get_conference_info()

    def _NH_RTPStreamDidTimeout(self, notification):
        stream = notification.sender
        if stream.type != 'audio':
            return
        session = stream.session
        log.info('Audio stream for session %s timed out' % format_identity(session.remote_identity))
        if session.streams == [stream]:
            session.end()

    def _NH_ChatStreamGotMessage(self, notification):
        stream = notification.sender
        message = notification.data.message
        if message.content_type not in ('text/html', 'text/plain'):
            log.info('Unsupported content type: %s, ignoring message' % message.content_type)
            stream.msrp_session.send_report(notification.data.chunk, 413, 'Unwanted message')
            return
        stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
        # Send MSRP chat message to other participants
        session = stream.session
        self.incoming_message_queue.send((session, 'msrp_message', message))
        # Send MSRP chat message to IRC chat room
        if message.content_type == 'text/html':
            content = html2text(message.content)
        elif message.content_type == 'text/plain':
            content = message.content
        else:
            log.warning('unexpected message type: %s' % message.content_type)
            return
        sender = message.sender
        irc_message = '%s: %s' % (format_identity(sender), content)
        if self.irc_protocol is not None:
            self.irc_protocol.send_message(irc_message.encode('utf-8'))
        else:
            self.pending_messages.append(irc_message)

    def _NH_ChatStreamGotNicknameRequest(self, notification):
        # Discard the nickname but pretend we accept it so that XMPP clients can work
        chunk = notification.data.chunk
        notification.sender.accept_nickname(chunk)

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


@implementer(IObserver)
class WelcomeHandler(object):

    def __init__(self, room, session):
        self.room = room
        self.session = session
        self.proc = None

    @run_in_green_thread
    def start(self, welcome_prompt=True):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)

        self.proc = proc.spawn(self.play_audio_welcome, welcome_prompt)
        self.proc.wait()

        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.room = None
        self.proc = None

    def play_file_in_player(self, player, file, delay):
        player.filename = file
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError as e:
            log.warning('Error playing file %s: %s' % (file, e))

    def play_audio_welcome(self, welcome_prompt):
        try:
            audio_stream = next(stream for stream in self.session.streams if stream.type == 'audio')
        except StopIteration:
            return
        player = WavePlayer(audio_stream.mixer, '', pause_time=1, initial_delay=1, volume=50)
        audio_stream.bridge.add(player)
        try:
            if welcome_prompt:
                file = Resources.get('sounds/co_welcome_conference.wav')
                self.play_file_in_player(player, file, 1)
            user_count = len({str(s.remote_identity.uri) for s in self.room.sessions if s.remote_identity.uri != self.session.remote_identity.uri and any(stream for stream in s.streams if stream.type == 'audio')})
            if user_count == 0:
                file = Resources.get('sounds/co_only_one.wav')
                self.play_file_in_player(player, file, 0.5)
            elif user_count == 1:
                file = Resources.get('sounds/co_there_is_one.wav')
                self.play_file_in_player(player, file, 0.5)
            elif user_count < 100:
                file = Resources.get('sounds/co_there_are.wav')
                self.play_file_in_player(player, file, 0.2)
                if user_count <= 24:
                    file = Resources.get('sounds/bi_%d.wav' % user_count)
                    self.play_file_in_player(player, file, 0.1)
                else:
                    file = Resources.get('sounds/bi_%d0.wav' % (user_count / 10))
                    self.play_file_in_player(player, file, 0.1)
                    file = Resources.get('sounds/bi_%d.wav' % (user_count % 10))
                    self.play_file_in_player(player, file, 0.1)
                file = Resources.get('sounds/co_more_participants.wav')
                self.play_file_in_player(player, file, 0)
            file = Resources.get('sounds/connected_tone.wav')
            self.play_file_in_player(player, file, 0.1)
        except proc.ProcExit:
            # No need to remove the bridge from the stream, it's done automatically
            pass
        else:
            audio_stream.bridge.remove(player)
            self.room.audio_conference.add(audio_stream)
            self.room.audio_conference.unhold()
        finally:
            player.stop()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionWillEnd(self, notification):
        self.proc.kill()


class IRCBot(irc.IRCClient):

    nickname = 'SylkServer'

    def __init__(self):
        self._nick_collector = []
        self.nicks = []

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)
        log.info('Connection to IRC has been established')
        NotificationCenter().post_notification('IRCBotGotConnected', self.factory, NotificationData(protocol=self))

    def connectionLost(self, failure):
        irc.IRCClient.connectionLost(self, failure)
        NotificationCenter().post_notification('IRCBotGotDisconnected', self.factory, NotificationData())

    def signedOn(self):
        log.info('Logging into %s channel...' % self.factory.channel)
        self.join(self.factory.channel)

    def kickedFrom(self, channel, kicker, message):
        log.info('Got kicked from %s by %s: %s. Rejoining...' % (channel, kicker, message))
        self.join(self.factory.channel)

    def joined(self, channel):
        log.info('Logged into %s channel' % channel)
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
        uri = SIPURI.parse('sip:%s@%s' % (urllib.parse.quote(username), self.factory.config.server[0]))
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
        log.info('Disconnected from IRC: %s' % failure.getErrorMessage())
        if not self.stop_requested:
            log.info('Reconnecting...')
            connector.connect()

    def clientConnectionFailed(self, connector, failure):
        log.error('Connection to IRC server failed: %s' % failure.getErrorMessage())


