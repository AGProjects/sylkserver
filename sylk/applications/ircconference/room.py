# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

import random

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python.util import Null, Singleton
from eventlet import coros, proc
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreInvalidStateError
from sipsimple.payloads.conference import Conference, ConferenceDescription, ConferenceState, Endpoint, HostInfo, JoiningInfo, Media, User, Users, WebPage
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.streams.msrp import ChatStreamError
from sipsimple.threading import run_in_twisted_thread
from twisted.internet import protocol, reactor
from twisted.words.protocols import irc
from zope.interface import implements

from sylk.applications.ircconference.configuration import get_room_configuration


def format_identity(identity, cpim_format=False):
    uri = identity.uri
    if identity.display_name:
        return u'%s <sip:%s@%s>' % (identity.display_name, uri.user, uri.host)
    elif cpim_format:
        return u'<sip:%s@%s>' % (uri.user, uri.host)
    else:
        return u'sip:%s@%s' % (uri.user, uri.host)

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
        self.state = 'started'

    def stop(self):
        if self.state != 'started':
            return
        self.state = 'stopped'
        NotificationCenter().remove_observer(self, sender=self.irc_connector.factory)
        self.irc_connector.factory.stop_requested = True
        self.irc_connector.disconnect()
        self.message_dispatcher.kill(proc.ProcExit)

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

    def dispatch_irc_message(self, message):
        for session in self.sessions:
            chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
            chat_stream.send_message(message.body, message.content_type, local_identity=message.sender, recipients=[self.identity])

    def dispatch_server_message(self, body, content_type='text/plain', exclude=None):
        for session in (session for session in self.sessions if session is not exclude):
            chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
            chat_stream.send_message(body, content_type, local_identity=self.identity, recipients=[self.identity])

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
            host_info = HostInfo(web_page=WebPage('http://sylkserver.com'))
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
            endpoint = Endpoint(str(session._invitation.remote_contact_header.uri), display_text=session.remote_identity.display_name, joining_info=joining_info)
            for stream in session.streams:
                endpoint.append(Media(id(stream), media_type='message'))
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
        chat_stream = (stream for stream in session.streams if stream.type == 'chat').next()
        notification_center.add_observer(self, sender=chat_stream)
        self.get_conference_info()
        if len(self.sessions) == 1:
            log.msg(u'%s started conference %s' % (format_identity(session.remote_identity), self.uri))
        else:
            log.msg(u'%s joined conference %s' % (format_identity(session.remote_identity), self.uri))
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has joined the room ' % format_identity(session.remote_identity), exclude=session)

    def remove_session(self, session):
        notification_center = NotificationCenter()
        chat_stream = (stream for stream in session.streams or [] if stream.type == 'chat').next()
        notification_center.remove_observer(self, sender=chat_stream)
        notification_center.remove_observer(self, sender=session)
        self.sessions.remove(session)
        self.get_conference_info()
        log.msg(u'%s left conference %s after %s' % (format_identity(session.remote_identity), self.uri, format_session_duration(session)))
        if not self.sessions:
            log.msg(u'Last participant left conference %s' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), format_session_duration(session)))

    def handle_incoming_subscription(self, subscribe_request, data):
        NotificationCenter().add_observer(self, sender=subscribe_request)
        subscribe_request.accept()
        self.subscriptions.append(subscribe_request)
        self.get_conference_info()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionGotProposal(self, notification):
        session = notification.sender
        session.reject_proposal()

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


