# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import hashlib
import os
import random
import re
import shutil
import string
import weakref

from collections import defaultdict, deque
from glob import glob
from itertools import chain, count, cycle

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import makedirs
from eventlib import api, coros, proc
from sipsimple.account import AccountManager
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioConference, WavePlayer, WavePlayerError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import Engine, SIPCoreError, SIPCoreInvalidStateError, SIPURI
from sipsimple.core import Header, ContactHeader, FromHeader, ToHeader
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads import conference
from sipsimple.session import IllegalStateError
from sipsimple.streams import FileTransferStream
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.streams.msrp import ChatStreamError, FileSelector
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications.conference.configuration import get_room_config, ConferenceConfig
from sylk.applications.conference.logger import log
from sylk.bonjour import BonjourServices
from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig
from sylk.configuration.datatypes import ResourcePath, URL
from sylk.session import Session


def format_identity(identity):
    uri = identity.uri
    if identity.display_name:
        return u'%s <%s@%s>' % (identity.display_name, uri.user, uri.host)
    else:
        return u'%s@%s' % (uri.user, uri.host)


class ScreenImage(object):
    def __init__(self, room, sender):
        self.room = weakref.ref(room)
        self.room_uri = room.uri
        self.sender = sender
        self.filename = os.path.join(ConferenceConfig.screen_sharing_dir, room.uri, '%s@%s_%s.jpg' % (sender.uri.user, sender.uri.host, ''.join(random.sample(string.letters+string.digits, 10))))
        from sylk.applications.conference import ConferenceApplication
        port = ConferenceApplication().screen_sharing_web_server.port
        scheme = 'https' if ConferenceConfig.screen_sharing_use_https else 'http'
        self.url = URL('%s://%s:%s/' % (scheme, ConferenceConfig.screen_sharing_ip.normalized, port))
        self.url.query_items['image'] = os.path.join(room.uri, os.path.basename(self.filename))
        self.state = None
        self.timer = None

    @property
    def active(self):
        return self.state == 'active'

    @property
    def idle(self):
        return self.state == 'idle'

    @run_in_thread('file-io')
    def save(self, image):
        makedirs(os.path.dirname(self.filename))
        tmp_filename = self.filename + '.tmp'
        try:
            with open(tmp_filename, 'wb') as file:
                file.write(image)
        except EnvironmentError, e:
            log.msg('Room %s - cannot write screen sharing image: %s: %s' % (self.room_uri, self.filename, e))
        else:
            try:
                os.rename(tmp_filename, self.filename)
            except EnvironmentError:
                pass
            self.advertise()

    @run_in_twisted_thread
    def advertise(self):
        if self.state == 'active':
            self.timer.reset(10)
        else:
            if self.timer is not None and self.timer.active():
                self.timer.cancel()
            self.state = 'active'
            self.timer = reactor.callLater(10, self.stop_advertising)
            room = self.room() or Null
            room.dispatch_conference_info()
            txt = 'Room %s - %s is sharing the screen at %s' % (self.room_uri, format_identity(self.sender), self.url)
            room.dispatch_server_message(txt)
            log.msg(txt)

    @run_in_twisted_thread
    def stop_advertising(self):
        if self.state != 'idle':
            if self.timer is not None and self.timer.active():
                self.timer.cancel()
            self.state = 'idle'
            self.timer = None
            room = self.room() or Null
            room.dispatch_conference_info()
            txt = '%s stopped sharing the screen' % format_identity(self.sender)
            room.dispatch_server_message(txt)
            log.msg(txt)


class Room(object):
    """
    Object representing a conference room, it will handle the message dispatching
    among all the participants.
    """
    implements(IObserver)

    def __init__(self, uri):
        self.config = get_room_config(uri)
        self.uri = uri
        self.identity = CPIMIdentity(SIPURI.parse('sip:%s' % self.uri), display_name='Conference Room')
        self.files = []
        self.screen_images = {}
        self.sessions = []
        self.subscriptions = []
        self.transfer_handlers = weakref.WeakSet()
        self.state = 'stopped'
        self.incoming_message_queue = coros.queue()
        self.message_dispatcher = None
        self.audio_conference = None
        self.moh_player = None
        self.conference_info_payload = None
        self.conference_info_version = count(1)
        self.bonjour_services = Null()
        self.session_nickname_map = {}
        self.last_nicknames_map = {}
        self.participants_counter = defaultdict(lambda: 0)
        self.history = deque(maxlen=ConferenceConfig.history_size)

    @property
    def empty(self):
        return len(self.sessions) == 0

    @property
    def started(self):
        return self.state == 'started'

    @property
    def stopping(self):
        return self.state in ('stopping', 'stopped')

    @property
    def active_media(self):
        return set((stream.type for stream in chain(*(session.streams for session in self.sessions if session.streams))))

    @property
    def conference_info(self):
        if self.conference_info_payload is None:
            settings = SIPSimpleSettings()
            conference_description = conference.ConferenceDescription(display_text='Ad-hoc conference', free_text='Hosted by %s' % settings.user_agent)
            conference_description.conf_uris = conference.ConfUris()
            conference_description.conf_uris.add(conference.ConfUrisEntry('sip:%s' % self.uri, purpose='participation'))
            if self.config.advertise_xmpp_support:
                conference_description.conf_uris.add(conference.ConfUrisEntry('xmpp:%s' % self.uri, purpose='participation'))
                # TODO: add grouptextchat service uri
            for number in self.config.pstn_access_numbers:
                conference_description.conf_uris.add(conference.ConfUrisEntry('tel:%s' % number, purpose='participation'))
            host_info = conference.HostInfo(web_page=conference.WebPage('http://sylkserver.com'))
            self.conference_info_payload = conference.Conference(self.identity.uri, conference_description=conference_description, host_info=host_info, users=conference.Users())
        self.conference_info_payload.version = next(self.conference_info_version)
        user_count = len(self.participants_counter.keys())
        self.conference_info_payload.conference_state = conference.ConferenceState(user_count=user_count, active=True)
        users = conference.Users()
        for session in (session for session in self.sessions if not (len(session.streams) == 1 and session.streams[0].type == 'file-transfer')):
            try:
                user = next(user for user in users if user.entity == str(session.remote_identity.uri))
            except StopIteration:
                display_text = self.last_nicknames_map.get(str(session.remote_identity.uri), session.remote_identity.display_name)
                user = conference.User(str(session.remote_identity.uri), display_text=display_text)
                user_uri = '%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)
                screen_image = self.screen_images.get(user_uri, None)
                if screen_image is not None and screen_image.active:
                    user.screen_image_url = screen_image.url
                users.add(user)
            joining_info = conference.JoiningInfo(when=session.start_time)
            holdable_streams = [stream for stream in session.streams if stream.hold_supported]
            session_on_hold = holdable_streams and all(stream.on_hold_by_remote for stream in holdable_streams)
            hold_status = conference.EndpointStatus('on-hold' if session_on_hold else 'connected')
            display_text = self.session_nickname_map.get(session, session.remote_identity.display_name)
            endpoint = conference.Endpoint(str(session._invitation.remote_contact_header.uri), display_text=display_text, joining_info=joining_info, status=hold_status)
            for stream in session.streams:
                if stream.type == 'file-transfer':
                    continue
                endpoint.add(conference.Media(id(stream), media_type=self.format_conference_stream_type(stream)))
            user.add(endpoint)
        self.conference_info_payload.users = users
        if self.files:
            files = conference.FileResources(conference.FileResource(os.path.basename(file.name), file.hash, file.size, file.sender, file.status) for file in self.files)
            self.conference_info_payload.conference_description.resources = conference.Resources(files=files)
        return self.conference_info_payload.toxml()

    def start(self):
        if self.started:
            return
        if ServerConfig.enable_bonjour and self.identity.uri.user != 'conference':
            room_user = self.identity.uri.user
            self.bonjour_services = BonjourServices(service='sipuri', name='Conference Room %s' % room_user, uri_user=room_user)
            self.bonjour_services.start()
        self.message_dispatcher = proc.spawn(self._message_dispatcher)
        self.audio_conference = AudioConference()
        self.audio_conference.hold()
        self.moh_player = MoHPlayer(self.audio_conference)
        self.moh_player.start()
        self.state = 'started'

    def stop(self):
        if not self.started:
            return
        self.state = 'stopping'
        self.bonjour_services.stop()
        self.bonjour_services = None
        self.incoming_message_queue.send_exception(api.GreenletExit)
        self.incoming_message_queue = None
        self.message_dispatcher.kill(proc.ProcExit)
        self.message_dispatcher = None
        self.moh_player.stop()
        self.moh_player = None
        self.audio_conference = None
        [handler.stop() for handler in self.transfer_handlers]
        notification_center = NotificationCenter()
        for subscription in self.subscriptions:
            notification_center.remove_observer(self, sender=subscription)
            subscription.end()
        self.subscriptions = []
        self.cleanup_files()
        self.conference_info_payload = None
        self.state = 'stopped'

    @run_in_thread('file-io')
    def cleanup_files(self):
        path = os.path.join(ConferenceConfig.file_transfer_dir, self.uri)
        try:
            shutil.rmtree(path)
        except EnvironmentError:
            pass
        path = os.path.join(ConferenceConfig.screen_sharing_dir, self.uri)
        try:
            shutil.rmtree(path)
        except EnvironmentError:
            pass

    def _message_dispatcher(self):
        """Read from self.incoming_message_queue and dispatch the messages to other participants"""
        while True:
            session, message_type, data = self.incoming_message_queue.wait()
            if message_type == 'message':
                message = data.message
                if message.sender.uri != session.remote_identity.uri:
                    continue
                if message.body.startswith('?OTR:'):
                    continue
                if message.timestamp is None:
                    message.timestamp = ISOTimestamp.utcnow()
                message.sender.display_name = self.last_nicknames_map.get(str(session.remote_identity.uri), message.sender.display_name)
                recipient = message.recipients[0]
                private = len(message.recipients) == 1 and '%s@%s' % (recipient.uri.user, recipient.uri.host) != self.uri
                if private:
                    self.dispatch_private_message(session, message)
                else:
                    self.history.append(message)
                    self.dispatch_message(session, message)
            elif message_type == 'composing_indication':
                if data.sender.uri != session.remote_identity.uri:
                    continue
                recipient = data.recipients[0]
                private = len(data.recipients) == 1 and '%s@%s' % (recipient.uri.user, recipient.uri.host) != self.uri
                if private:
                    self.dispatch_private_iscomposing(session, data)
                else:
                    self.dispatch_iscomposing(session, data)

    def dispatch_message(self, session, message):
        for s in (s for s in self.sessions if s is not session):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            try:
                chat_stream.send_message(message.body, message.content_type, local_identity=message.sender, recipients=[self.identity], timestamp=message.timestamp, additional_headers=message.additional_headers)
            except ChatStreamError, e:
                log.error(u'Error dispatching message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_private_message(self, session, message):
        # Private messages are delivered to all sessions matching the recipient but also to the sender,
        # for replication in clients
        recipient = message.recipients[0]
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri in (recipient.uri, session.remote_identity.uri)):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            try:
                chat_stream.send_message(message.body, message.content_type, local_identity=message.sender, recipients=[recipient], timestamp=message.timestamp, additional_headers=message.additional_headers)
            except ChatStreamError, e:
                log.error(u'Error dispatching private message to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_iscomposing(self, session, data):
        for s in (s for s in self.sessions if s is not session):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            identity = CPIMIdentity(session.remote_identity.uri, session.remote_identity.display_name)
            try:
                chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity, recipients=[self.identity])
            except ChatStreamError, e:
                log.error(u'Error dispatching composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_private_iscomposing(self, session, data):
        recipient_uri = data.recipients[0].uri
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri == recipient_uri):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            identity = CPIMIdentity(session.remote_identity.uri, session.remote_identity.display_name)
            try:
                chat_stream.send_composing_indication(data.state, data.refresh, local_identity=identity)
            except ChatStreamError, e:
                log.error(u'Error dispatching private composing indication to %s: %s' % (s.remote_identity.uri, e))

    def dispatch_server_message(self, body, content_type='text/plain', exclude=None):
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
            except StopIteration:
                continue
            chat_stream.send_message(body, content_type, local_identity=self.identity, recipients=[self.identity])

    def dispatch_conference_info(self):
        data = self.conference_info
        for subscription in (subscription for subscription in self.subscriptions if subscription.state == 'active'):
            try:
                subscription.push_content(conference.ConferenceDocument.content_type, data)
            except (SIPCoreError, SIPCoreInvalidStateError):
                pass

    def dispatch_file(self, file):
        sender_uri = file.sender.uri
        for uri in set(session.remote_identity.uri for session in self.sessions if str(session.remote_identity.uri) != str(sender_uri)):
            handler = OutgoingFileTransferHandler(self, uri, file)
            self.transfer_handlers.add(handler)
            handler.start()

    def add_session(self, session):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        self.sessions.append(session)
        remote_uri = str(session.remote_identity.uri)
        self.participants_counter[remote_uri] += 1
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
            log.msg(u'Room %s - audio stream %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (self.uri, audio_stream.codec, audio_stream.sample_rate,
                                                                                      'encrypted' if audio_stream.srtp_active else 'unencrypted',
                                                                                      audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                      audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
        try:
            transfer_stream = next(stream for stream in session.streams if stream.type == 'file-transfer')
        except StopIteration:
            pass
        else:
            if transfer_stream.direction == 'recvonly':
                transfer_handler = IncomingFileTransferHandler(self, session)
                transfer_handler.start()
                txt = u'Room %s - %s is uploading file %s (%s)' % (self.uri, format_identity(session.remote_identity), transfer_stream.file_selector.name.decode('utf-8'), self.format_file_size(transfer_stream.file_selector.size))
            else:
                transfer_handler = OutgoingFileTransferRequestHandler(self, session)
                transfer_handler.start()
                txt = u'Room %s - %s requested file %s' % (self.uri, format_identity(session.remote_identity), transfer_stream.file_selector.name.decode('utf-8'))
            log.msg(txt)
            self.dispatch_server_message(txt)
            if len(session.streams) == 1:
                return

        welcome_handler = WelcomeHandler(self, session)
        welcome_handler.start()
        self.dispatch_conference_info()

        if len(self.sessions) == 1:
            log.msg(u'Room %s - started by %s with %s' % (self.uri, format_identity(session.remote_identity), self.format_stream_types(session.streams)))
        else:
            log.msg(u'Room %s - %s joined with %s' % (self.uri, format_identity(session.remote_identity), self.format_stream_types(session.streams)))
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has joined the room %s' % (format_identity(session.remote_identity), self.format_stream_types(session.streams)), exclude=session)

        if ServerConfig.enable_bonjour:
            self._update_bonjour_presence()

    def remove_session(self, session):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=session)
        self.sessions.remove(session)
        self.session_nickname_map.pop(session, None)
        remote_uri = str(session.remote_identity.uri)
        self.participants_counter[remote_uri] -= 1
        if self.participants_counter[remote_uri] <= 0:
            del self.participants_counter[remote_uri]
            self.last_nicknames_map.pop(remote_uri, None)
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
                self.moh_player.pause()
                self.audio_conference.hold()
            elif len(self.audio_conference.streams) == 1:
                self.moh_player.play()
        try:
            next(stream for stream in session.streams if stream.type == 'file-transfer')
        except StopIteration:
            pass
        else:
            if len(session.streams) == 1:
                return

        self.dispatch_conference_info()
        log.msg(u'Room %s - %s left conference after %s' % (self.uri, format_identity(session.remote_identity), self.format_session_duration(session)))
        if not self.sessions:
            log.msg(u'Room %s - Last participant left conference' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), self.format_session_duration(session)))

        if ServerConfig.enable_bonjour:
            self._update_bonjour_presence()

    def terminate_sessions(self, uri):
        if not self.started:
            return
        for session in (session for session in self.sessions if session.remote_identity.uri == uri):
            session.end()

    def handle_incoming_subscription(self, subscribe_request, data):
        log.msg('Room %s - subscription from %s' % (self.uri, data.headers['From'].uri))
        if subscribe_request.event != 'conference':
            log.msg('Room %s - Subscription rejected: only conference event is supported' % self.uri)
            subscribe_request.reject(489)
            return
        NotificationCenter().add_observer(self, sender=subscribe_request)
        self.subscriptions.append(subscribe_request)
        subscribe_request.accept(conference.ConferenceDocument.content_type, self.conference_info)

    def accept_proposal(self, session, streams):
        if session.state == 'received_proposal':
            try:
                session.accept_proposal(streams)
            except IllegalStateError:
                pass

    def add_file(self, file):
        if file.status == 'INCOMPLETE':
            self.dispatch_server_message('%s has cancelled upload of file %s (%s)' % (format_identity(file.sender), os.path.basename(file.name), self.format_file_size(file.size)))
        else:
            self.dispatch_server_message('%s has uploaded file %s (%s)' % (format_identity(file.sender), os.path.basename(file.name), self.format_file_size(file.size)))
            self.files.append(file)
            self.dispatch_conference_info()
            if ConferenceConfig.push_file_transfer:
                self.dispatch_file(file)

    def add_screen_image(self, sender, image):
        sender_uri = '%s@%s' % (sender.uri.user, sender.uri.host)
        screen_image = self.screen_images.setdefault(sender_uri, ScreenImage(self, sender))
        screen_image.save(image)

    def _update_bonjour_presence(self):
        num = len(self.sessions)
        if num == 0:
            num_str = 'No'
        elif num == 1:
            num_str = 'One'
        elif num == 2:
            num_str = 'Two'
        else:
            num_str = str(num)
        txt = u'%s participant%s' % (num_str, '' if num==1 else 's')
        presence_state = BonjourPresenceState('available', txt)
        if self.bonjour_services is Null:
            # This is the room being published all the time
            from sylk.applications.conference import ConferenceApplication
            ConferenceApplication().bonjour_room_service.presence_state = presence_state
        else:
            self.bonjour_services.presence_state = presence_state

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AudioStreamDidTimeout(self, notification):
        stream = notification.sender
        session = stream.session
        log.msg(u'Room %s - audio stream for session %s timed out' % (self.uri, format_identity(session.remote_identity)))
        if session.streams == [stream]:
            session.end()

    def _NH_ChatStreamGotMessage(self, notification):
        stream = notification.sender
        stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
        data = notification.data
        session = notification.sender.session
        message = data.message
        content_type = message.content_type.lower()
        if content_type.startswith('text/'):
            self.incoming_message_queue.send((session, 'message', data))
        elif content_type == 'application/blink-screensharing':
            self.add_screen_image(message.sender, message.body)

    def _NH_ChatStreamGotComposingIndication(self, notification):
        stream = notification.sender
        stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
        data = notification.data
        session = notification.sender.session
        self.incoming_message_queue.send((session, 'composing_indication', data))

    def _NH_ChatStreamGotNicknameRequest(self, notification):
        nickname = notification.data.nickname
        session = notification.sender.session
        chunk = notification.data.chunk
        if nickname:
            if nickname in self.session_nickname_map.values() and (session not in self.session_nickname_map or self.session_nickname_map[session] != nickname):
                notification.sender.reject_nickname(chunk, 425, 'Nickname reserved or already in use')
                return
            self.session_nickname_map[session] = nickname
            self.last_nicknames_map[str(session.remote_identity.uri)] = nickname
        else:
            self.session_nickname_map.pop(session, None)
            self.last_nicknames_map.pop(str(session.remote_identity.uri), None)
        notification.sender.accept_nickname(chunk)
        self.dispatch_conference_info()

    def _NH_SIPIncomingSubscriptionDidEnd(self, notification):
        subscription = notification.sender
        try:
            self.subscriptions.remove(subscription)
        except ValueError:
            pass
        else:
            notification.center.remove_observer(self, sender=subscription)

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        session = notification.sender
        if notification.data.originator == 'remote':
            if notification.data.on_hold:
                log.msg(u'Room %s - %s has put the audio session on hold' % (self.uri, format_identity(session.remote_identity)))
            else:
                log.msg(u'Room %s - %s has taken the audio session out of hold' % (self.uri, format_identity(session.remote_identity)))
            self.dispatch_conference_info()

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            audio_streams = [stream for stream in notification.data.proposed_streams if stream.type=='audio']
            chat_streams = [stream for stream in notification.data.proposed_streams if stream.type=='chat']
            if not audio_streams and not chat_streams:
                session.reject_proposal()
                return
            streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
            reactor.callLater(4, self.accept_proposal, session, streams)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        session = notification.sender
        for stream in notification.data.added_streams:
            notification.center.add_observer(self, sender=stream)
            txt = u'%s has added %s' % (format_identity(session.remote_identity), stream.type)
            log.msg(u'Room %s - %s' % (self.uri, txt))
            self.dispatch_server_message(txt, exclude=session)
            if stream.type == 'audio':
                log.msg(u'Room %s - audio stream %s/%sHz (%s), end-points: %s:%d <-> %s:%d' % (self.uri, stream.codec, stream.sample_rate,
                                                                                               'encrypted' if stream.srtp_active else 'unencrypted',
                                                                                               stream.local_rtp_address, stream.local_rtp_port,
                                                                                               stream.remote_rtp_address, stream.remote_rtp_port))
            welcome_handler = WelcomeHandler(self, session)
            welcome_handler.start(welcome_prompt=False)

        for stream in notification.data.removed_streams:
            notification.center.remove_observer(self, sender=stream)
            txt = u'%s has removed %s' % (format_identity(session.remote_identity), stream.type)
            log.msg(u'Room %s - %s' % (self.uri, txt))
            self.dispatch_server_message(txt, exclude=session)
            if stream.type == 'audio':
                try:
                    self.audio_conference.remove(stream)
                except ValueError:
                    # User may hangup before getting bridged into the conference
                    pass
                if len(self.audio_conference.streams) == 0:
                    self.moh_player.pause()
                    self.audio_conference.hold()
                elif len(self.audio_conference.streams) == 1:
                    self.moh_player.play()
            if not session.streams:
                log.msg(u'Room %s - %s has removed all streams, session will be terminated' % (self.uri, format_identity(session.remote_identity)))
                session.end()
        self.dispatch_conference_info()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        log.msg(u'Room %s - Call transfer request rejected, REFER must be out of dialog (RFC4579 5.5)' % self.uri)
        notification.sender.reject_transfer(403)

    @staticmethod
    def format_stream_types(streams):
        if not streams:
            return ''
        if len(streams) == 1:
            txt = 'with %s' % streams[0].type
        else:
            txt = 'with %s' % ','.join(stream.type for stream in streams[:-1])
            txt += ' and %s' % streams[-1:][0].type
        return txt

    @staticmethod
    def format_conference_stream_type(stream):
        if stream.type == 'chat':
            return 'message'
        return stream.type

    @staticmethod
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

    @staticmethod
    def format_file_size(size):
        infinite = float('infinity')
        boundaries = [(             1024, '%d bytes',               1),
                        (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                        (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                        (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
        for boundary, format, divisor in boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size


class MoHPlayer(object):
    implements(IObserver)

    def __init__(self, conference):
        self.conference = conference
        self.files = None
        self.paused = None
        self._player = None

    def start(self):
        files = glob('%s/*.wav' % ResourcePath('sounds/moh').normalized)
        if not files:
            log.error(u'No files found, MoH is disabled')
            return
        random.shuffle(files)
        self.files = cycle(files)
        self._player = WavePlayer(SIPApplication.voice_audio_mixer, '', pause_time=1, initial_delay=1, volume=20)
        self.paused = True
        self.conference.bridge.add(self._player)
        NotificationCenter().add_observer(self, sender=self._player)

    def stop(self):
        if self._player is None:
            return
        NotificationCenter().remove_observer(self, sender=self._player)
        self._player.stop()
        self.paused = True
        self.conference.bridge.remove(self._player)
        self.conference = None

    def play(self):
        if self._player is not None and self.paused:
            self.paused = False
            self._play_next_file()

    def pause(self):
        if self._player is not None and not self.paused:
            self.paused = True
            self._player.stop()

    def _play_next_file(self):
        self._player.filename = next(self.files)
        self._player.play()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_WavePlayerDidFail(self, notification):
        if not self.paused:
            self._play_next_file()

    _NH_WavePlayerDidEnd = _NH_WavePlayerDidFail


class WelcomeHandler(object):
    implements(IObserver)

    def __init__(self, room, session):
        self.room = room
        self.session = session
        self.proc = None

    @run_in_green_thread
    def start(self, welcome_prompt=True):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)

        self.render_chat_welcome()
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
        except WavePlayerError, e:
            log.warning(u"Error playing file %s: %s" % (file, e))

    def play_audio_welcome(self, welcome_prompt):
        try:
            audio_stream = next(stream for stream in self.session.streams if stream.type == 'audio')
        except StopIteration:
            return
        player = WavePlayer(audio_stream.mixer, '', pause_time=1, initial_delay=1, volume=50)
        audio_stream.bridge.add(player)
        try:
            if welcome_prompt:
                file = ResourcePath('sounds/co_welcome_conference.wav').normalized
                self.play_file_in_player(player, file, 1)
            user_count = len(set(str(s.remote_identity.uri) for s in self.room.sessions if any(stream for stream in s.streams if stream.type == 'audio')) - set([str(self.session.remote_identity.uri)]))
            if user_count == 0:
                file = ResourcePath('sounds/co_only_one.wav').normalized
                self.play_file_in_player(player, file, 0.5)
            elif user_count == 1:
                file = ResourcePath('sounds/co_there_is_one.wav').normalized
                self.play_file_in_player(player, file, 0.5)
            elif user_count < 100:
                file = ResourcePath('sounds/co_there_are.wav').normalized
                self.play_file_in_player(player, file, 0.2)
                if user_count <= 24:
                    file = ResourcePath('sounds/bi_%d.wav' % user_count).normalized
                    self.play_file_in_player(player, file, 0.1)
                else:
                    file = ResourcePath('sounds/bi_%d0.wav' % (user_count / 10)).normalized
                    self.play_file_in_player(player, file, 0.1)
                    file = ResourcePath('sounds/bi_%d.wav' % (user_count % 10)).normalized
                    self.play_file_in_player(player, file, 0.1)
                file = ResourcePath('sounds/co_more_participants.wav').normalized
                self.play_file_in_player(player, file, 0)
            file = ResourcePath('sounds/connected_tone.wav').normalized
            self.play_file_in_player(player, file, 0.1)
        except proc.ProcExit:
            # No need to remove the bridge from the stream, it's done automatically
            pass
        else:
            audio_stream.bridge.remove(player)
            self.room.audio_conference.add(audio_stream)
            self.room.audio_conference.unhold()
            if len(self.room.audio_conference.streams) == 1:
                self.room.moh_player.play()
            else:
                self.room.moh_player.pause()
        finally:
            player.stop()

    def render_chat_welcome(self):
        try:
            chat_stream = next(stream for stream in self.session.streams if stream.type == 'chat')
        except StopIteration:
            return
        txt = 'Welcome to SylkServer!'
        user_count = len(set(str(s.remote_identity.uri) for s in self.room.sessions) - set([str(self.session.remote_identity.uri)]))
        if user_count == 0:
            txt += ' You are the first participant'
        else:
            if user_count == 1:
                txt += ' There is one more participant'
            else:
                txt += ' There are %s more participants' % user_count
        txt +=  ' in this conference room.'
        if not ServerConfig.enable_bonjour:
            if self.room.config.advertise_xmpp_support or self.room.config.pstn_access_numbers:
                txt += '\n\nOther participants can join at these addresses:\n\n'
                if self.room.config.pstn_access_numbers:
                    if len(self.room.config.pstn_access_numbers) == 1:
                        nums = self.room.config.pstn_access_numbers[0]
                    else:
                        nums = ', '.join(self.room.config.pstn_access_numbers[:-1]) + ' or %s' % self.room.config.pstn_access_numbers[-1]
                    txt += '    - Using a landline or mobile phone, dial %s (audio)\n' % nums
                if self.room.config.advertise_xmpp_support:
                    txt += '    - Using an XMPP client, connect to group chat room %s (chat)\n' % self.room.uri
                    txt += '    - Using an XMPP Jingle capable client, add contact %s and call it (audio)\n' % self.room.uri
                txt += '    - Using a SIP client, initiate a session to %s (audio and chat)\n' % self.room.uri
        chat_stream.send_message(txt, 'text/plain', local_identity=self.room.identity, recipients=[self.room.identity])
        for msg in self.room.history:
            chat_stream.send_message(msg.body, msg.content_type, local_identity=msg.sender, recipients=[self.room.identity], timestamp=msg.timestamp)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionWillEnd(self, notification):
        self.proc.kill()


class RoomFile(object):

    def __init__(self, name, hash, size, sender, status):
        self.name = name
        self.hash = hash
        self.size = size
        self.sender = sender
        self.status = status

    @property
    def file_selector(self):
        return FileSelector.for_file(self.name.encode('utf-8'), hash=self.hash)


class IncomingFileTransferHandler(object):
    implements(IObserver)

    def __init__(self, room, session):
        self.room = weakref.ref(room)
        self.room_uri = room.uri
        self.session = session
        self.stream = next(stream for stream in self.session.streams if stream.type == 'file-transfer' and stream.direction == 'recvonly')
        self.error = False
        self.ended = False
        self.file = None
        self.file_selector = None
        self.filename = None
        self.hash = None
        self.status = None
        self.timer = None
        self.transfer_finished = False

    def start(self):
        self.file_selector = self.stream.file_selector
        path = os.path.join(ConferenceConfig.file_transfer_dir, self.room_uri)
        makedirs(path)
        self.filename = filename = os.path.join(path, self.file_selector.name.decode('utf-8'))
        basename, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(filename):
            filename = '%s_%d%s' % (basename, i, ext)
            i += 1
        self.filename = filename
        try:
            self.file = open(self.filename, 'wb')
        except EnvironmentError:
            log.msg('Room %s - cannot write destination filename: %s' % (self.room_uri, self.filename))
            self.session.end()
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)
        self.hash = hashlib.sha1()

    @run_in_thread('file-transfer')
    def write_chunk(self, data):
        notification_center = NotificationCenter()
        if data is not None:
            try:
                self.file.write(data)
            except EnvironmentError, e:
                notification_center.post_notification('IncomingFileTransferHandlerGotError', sender=self, data=NotificationData(error=str(e)))
            else:
                self.hash.update(data)
        else:
            self.file.close()
            if self.error:
                notification_center.post_notification('IncomingFileTransferHandlerDidFail', sender=self)
            else:
                notification_center.post_notification('IncomingFileTransferHandlerDidEnd', sender=self)

    @run_in_thread('file-io')
    def remove_bogus_file(self, filename):
        try:
            os.unlink(filename)
        except OSError:
            pass

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidEnd(self, notification):
        self.ended = True
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None

        notification.center.remove_observer(self, sender=self.stream)
        notification.center.remove_observer(self, sender=self.session)

        # Mark end of write operation
        self.write_chunk(None)

    def _NH_FileTransferStreamGotChunk(self, notification):
        self.write_chunk(notification.data.content)

    def _NH_FileTransferStreamDidFinish(self, notification):
        self.transfer_finished = True
        if self.timer is None:
            self.timer = reactor.callLater(5, self.session.end)

    def _NH_IncomingFileTransferHandlerGotError(self, notification):
        log.error('Error while handling incoming file transfer: %s' % notification.data.error)
        self.error = True
        self.status = notification.data.error
        if not self.ended and self.timer is None:
            self.timer = reactor.callLater(5, self.session.end)

    def _NH_IncomingFileTransferHandlerDidEnd(self, notification):
        notification.center.remove_observer(self, sender=self)

        remote_hash = self.file_selector.hash
        if not self.transfer_finished:
            log.msg('File transfer of %s cancelled' % os.path.basename(self.filename))
            self.remove_bogus_file(self.filename)
            self.status = 'INCOMPLETE'
        else:
            local_hash = 'sha1:' + ':'.join(re.findall(r'..', self.hash.hexdigest().upper()))
            if local_hash != remote_hash:
                log.warning('Hash of transferred file does not match the remote hash (file may have changed).')
                self.status = 'Hash missmatch'
                self.remove_bogus_file(self.filename)
            else:
                self.status = 'OK'

        sender = CPIMIdentity(self.session.remote_identity.uri, self.session.remote_identity.display_name)
        file = RoomFile(self.filename, remote_hash, self.file_selector.size, sender, self.status)
        room = self.room() or Null
        room.add_file(file)

        self.session = None
        self.stream = None

    def _NH_IncomingFileTransferHandlerDidFail(self, notification):
        notification.center.remove_observer(self, sender=self)

        sender = CPIMIdentity(self.session.remote_identity.uri, self.session.remote_identity.display_name)
        file = RoomFile(self.filename, self.file_selector.hash, self.file_selector.size, sender, self.status)
        room = self.room() or Null
        room.add_file(file)

        self.session = None
        self.stream = None


class OutgoingFileTransferRequestHandler(object):
    implements(IObserver)

    def __init__(self, room, session):
        self.room = weakref.ref(room)
        self.session = session
        self.stream = next(stream for stream in self.session.streams if stream.type == 'file-transfer')
        self.timer = None

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_FileTransferStreamDidFinish(self, notification):
        if self.timer is None:
            self.timer = reactor.callLater(2, self.session.end)

    def _NH_SIPSessionDidEnd(self, notification):
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.stream = None

    _NH_SIPSessionDidFail = _NH_SIPSessionDidEnd


class InterruptFileTransfer(Exception): pass

class OutgoingFileTransferHandler(object):
    implements(IObserver)

    def __init__(self, room, destination, file):
        self.room_uri = room.identity.uri
        self.destination = destination
        self.file = file
        self.session = None
        self.stream = None
        self.timer = None

    @run_in_green_thread
    def start(self):
        self.greenlet = api.getcurrent()
        settings = SIPSimpleSettings()
        account = AccountManager().sylkserver_account
        if account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host,
                            port=account.sip.outbound_proxy.port,
                            parameters={'transport': account.sip.outbound_proxy.transport})
        else:
            uri = SIPURI.new(self.destination)
        lookup = DNSLookup()
        try:
            routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
        except DNSLookupError:
            return

        notification_center = NotificationCenter()
        self.session = Session(account)
        self.stream = FileTransferStream(self.file.file_selector, 'sendonly')
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)
        subject = u'File uploaded by %s' % self.file.sender
        from_header = FromHeader(SIPURI.new(self.room_uri), u'Conference File Transfer')
        to_header = ToHeader(SIPURI.new(self.destination))
        transport = routes[0].transport
        parameters = {} if transport=='udp' else {'transport': transport}
        contact_header = ContactHeader(SIPURI(user=self.room_uri.user, host=SIPConfig.local_ip.normalized, port=getattr(Engine(), '%s_port' % transport), parameters=parameters))
        extra_headers = []
        if ThorNodeConfig.enabled:
            extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
        extra_headers.append(Header('X-Originator-From', str(self.file.sender.uri)))
        self.session.connect(from_header, to_header, contact_header=contact_header, routes=routes, streams=[self.stream], is_focus=True, subject=subject, extra_headers=extra_headers)

    def stop(self):
        if self.session is not None:
            self.session.end()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_FileTransferStreamDidFinish(self, notification):
        if self.timer is None:
            self.timer = reactor.callLater(2, self.session.end)

    def _NH_SIPSessionDidEnd(self, notification):
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.stream = None

    _NH_SIPSessionDidFail = _NH_SIPSessionDidEnd

