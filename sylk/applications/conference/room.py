
import os
import random
import shutil
import string
import weakref

from collections import Counter, deque
from glob import glob
from itertools import chain, count, cycle

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.system import makedirs
from eventlib import api, coros, proc
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioConference, WavePlayer, WavePlayerError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPCoreInvalidStateError, SIPURI
from sipsimple.core import Header, FromHeader, ToHeader, SubjectHeader
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads import conference
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.msrp.chat import ChatIdentity, CPIMHeader, CPIMNamespace
from sipsimple.streams.msrp.filetransfer import FileSelector
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor
from zope.interface import implements

from sylk.accounts import DefaultAccount
from sylk.applications.conference.configuration import get_room_config, ConferenceConfig
from sylk.applications.conference.logger import log
from sylk.bonjour import BonjourService
from sylk.configuration import ServerConfig, ThorNodeConfig
from sylk.configuration.datatypes import URL
from sylk.resources import Resources
from sylk.session import Session, IllegalStateError
from sylk.web import server as web_server


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
        self.filename = os.path.join(ConferenceConfig.screensharing_images_dir, room.uri, '%s@%s_%s.jpg' % (sender.uri.user, sender.uri.host, ''.join(random.sample(string.letters+string.digits, 10))))
        self.url = URL(web_server.url + '/conference/' + room.uri + '/screensharing')
        self.url.query_items['image'] = os.path.basename(self.filename)
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
        except EnvironmentError as e:
            log.info('Room %s - cannot write screen sharing image: %s: %s' % (self.room_uri, self.filename, e))
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
            log.info(txt)

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
            log.info(txt)


class Room(object):
    """
    Object representing a conference room, it will handle the message dispatching
    among all the participants.
    """
    implements(IObserver)

    def __init__(self, uri):
        self.config = get_room_config(uri)
        self.uri = uri
        self.identity = ChatIdentity(SIPURI.parse('sip:%s' % self.uri), display_name='Conference Room')
        self.files = []
        self.screen_images = {}
        self.sessions = []
        self.subscriptions = []
        self.state = 'stopped'
        self.incoming_message_queue = coros.queue()
        self.message_dispatcher = None
        self.audio_conference = None
        self.moh_player = None
        self.conference_info_payload = None
        self.conference_info_version = count(1)
        self.bonjour_services = Null
        self.session_nickname_map = {}
        self.last_nicknames_map = {}
        self.participants_counter = Counter()
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
        user_count = len(self.participants_counter)
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
            files = conference.FileResources(conference.FileResource(os.path.basename(file.name), file.hash, file.size, file.sender, 'OK') for file in self.files)
            self.conference_info_payload.conference_description.resources = conference.Resources(files=files)
        return self.conference_info_payload.toxml()

    def start(self):
        if self.started:
            return
        if ServerConfig.enable_bonjour and self.identity.uri.user != 'conference':
            room_user = self.identity.uri.user
            self.bonjour_services = BonjourService(service='sipuri', name='Conference Room %s' % room_user, uri_user=room_user)
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
        path = os.path.join(ConferenceConfig.screensharing_images_dir, self.uri)
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
                if message.content.startswith('?OTR:'):
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
            chat_stream.send_message(message.content, message.content_type, sender=message.sender, recipients=[self.identity], timestamp=message.timestamp, additional_headers=message.additional_headers)

    def dispatch_private_message(self, session, message):
        # Private messages are delivered to all sessions matching the recipient but also to the sender,
        # for replication in clients
        recipient = message.recipients[0]
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri in (recipient.uri, session.remote_identity.uri)):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            chat_stream.send_message(message.content, message.content_type, sender=message.sender, recipients=[recipient], timestamp=message.timestamp, additional_headers=message.additional_headers)

    def dispatch_iscomposing(self, session, data):
        identity = ChatIdentity(session.remote_identity.uri, session.remote_identity.display_name)
        for s in (s for s in self.sessions if s is not session):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            chat_stream.send_composing_indication(data.state, data.refresh, sender=identity, recipients=[self.identity])

    def dispatch_private_iscomposing(self, session, data):
        identity = ChatIdentity(session.remote_identity.uri, session.remote_identity.display_name)
        recipient_uri = data.recipients[0].uri
        for s in (s for s in self.sessions if s is not session and s.remote_identity.uri == recipient_uri):
            try:
                chat_stream = next(stream for stream in s.streams if stream.type == 'chat')
            except StopIteration:
                continue
            chat_stream.send_composing_indication(data.state, data.refresh, sender=identity)

    def dispatch_server_message(self, content, content_type='text/plain', exclude=None):
        ns = CPIMNamespace('urn:ag-projects:xml:ns:cpim', prefix='agp')
        message_type = CPIMHeader('Message-Type', ns, 'status')
        for session in (session for session in self.sessions if session is not exclude):
            try:
                chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
            except StopIteration:
                continue
            chat_stream.send_message(content, content_type, sender=self.identity, recipients=[self.identity], additional_headers=[message_type])

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
            handler = FileTransferHandler(self)
            handler.init_outgoing(uri, file)

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
            log.info(u'Room %s - audio stream %s/%sHz, end-points: %s:%d <-> %s:%d' % (self.uri, audio_stream.codec, audio_stream.sample_rate,
                                                                                      audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                      audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
            if audio_stream.encryption.type != 'ZRTP':
                # We don't listen for stream notifications early enough
                if audio_stream.encryption.active:
                    log.info(u'Room %s - %s audio stream enabled %s encryption' % (self.uri,
                                                                                  format_identity(session.remote_identity),
                                                                                  audio_stream.encryption.type))
                else:
                    log.info(u'Room %s - %s audio stream did not enable encryption' % (self.uri,
                                                                                      format_identity(session.remote_identity)))
        try:
            transfer_stream = next(stream for stream in session.streams if stream.type == 'file-transfer')
        except StopIteration:
            pass
        else:
            transfer_handler = FileTransferHandler(self)
            transfer_handler.init_incoming(transfer_stream)
            if transfer_stream.direction == 'recvonly':
                filename = os.path.basename(os.path.splitext(transfer_stream.file_selector.name)[0])
                txt = u'Room %s - %s is uploading file %s (%s)' % (self.uri, format_identity(session.remote_identity), filename,self.format_file_size(transfer_stream.file_selector.size))
            else:
                filename = os.path.basename(transfer_stream.file_selector.name)
                txt = u'Room %s - %s requested file %s' % (self.uri, format_identity(session.remote_identity), filename)
            log.info(txt)
            self.dispatch_server_message(txt)
            if len(session.streams) == 1:
                return

        welcome_handler = WelcomeHandler(self, initial=True, session=session, streams=session.streams)
        welcome_handler.run()
        self.dispatch_conference_info()

        if len(self.sessions) == 1:
            log.info(u'Room %s - started by %s with %s' % (self.uri, format_identity(session.remote_identity), self.format_stream_types(session.streams)))
        else:
            log.info(u'Room %s - %s joined with %s' % (self.uri, format_identity(session.remote_identity), self.format_stream_types(session.streams)))
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
        if self.participants_counter[remote_uri] == 0:
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
        log.info(u'Room %s - %s left conference after %s' % (self.uri, format_identity(session.remote_identity), self.format_session_duration(session)))
        if not self.sessions:
            log.info(u'Room %s - Last participant left conference' % self.uri)
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
        log.info('Room %s - subscription from %s' % (self.uri, data.headers['From'].uri))
        if subscribe_request.event != 'conference':
            log.info('Room %s - Subscription for event %s rejected: only conference event is supported' % (self.uri, subscribe_request.event))
            subscribe_request.reject(489)
            return
        NotificationCenter().add_observer(self, sender=subscribe_request)
        self.subscriptions.append(subscribe_request)
        try:
            subscribe_request.accept(conference.ConferenceDocument.content_type, self.conference_info)
        except SIPCoreError as e:
            log.warning('Error accepting SIP subscription: %s' % e)
            subscribe_request.end()

    def _accept_proposal(self, session, streams):
        try:
            session.accept_proposal(streams)
        except IllegalStateError:
            pass
        session.proposal_timer = None

    def add_file(self, file):
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

    def _NH_RTPStreamDidEnableEncryption(self, notification):
        stream = notification.sender
        session = stream.session
        log.info(u'Room %s - %s %s stream enabled %s encryption' % (self.uri,
                                                                   format_identity(session.remote_identity),
                                                                   stream.type,
                                                                   stream.encryption.type))

    def _NH_RTPStreamDidNotEnableEncryption(self, notification):
        stream = notification.sender
        session = stream.session
        log.info(u'Room %s - %s %s stream did not enable encryption: %s' % (self.uri,
                                                                           format_identity(session.remote_identity),
                                                                           stream.type,
                                                                           notification.data.reason))

    def _NH_RTPStreamZRTPReceivedSAS(self, notification):
        if not self.config.zrtp_auto_verify:
            return

        stream = notification.sender
        session = stream.session
        sas = notification.data.sas
        # Send ZRTP SAS over the chat stream, if available
        try:
            chat_stream = next(stream for stream in session.streams if stream.type=='chat')
        except StopIteration:
            return
        # Only send the message if there are no relays in between
        secure_chat = chat_stream.transport == 'tls' and all(len(path)==1 for path in (chat_stream.msrp.full_local_path, chat_stream.msrp.full_remote_path))
        if secure_chat:
            txt = 'Received ZRTP Short Authentication String: %s' % sas
            # Don't set the remote identity, that way it will appear as a private message
            ns = CPIMNamespace('urn:ag-projects:xml:ns:cpim', prefix='agp')
            message_type = CPIMHeader('Message-Type', ns, 'status')
            chat_stream.send_message(txt, 'text/plain', sender=self.identity, additional_headers=[message_type])

    def _NH_RTPStreamDidTimeout(self, notification):
        stream = notification.sender
        if stream.type != 'audio':
            return
        session = stream.session
        log.info(u'Room %s - audio stream for session %s timed out' % (self.uri, format_identity(session.remote_identity)))
        if session.streams == [stream]:
            session.end()

    def _NH_ChatStreamGotMessage(self, notification):
        stream = notification.sender
        data = notification.data
        session = notification.sender.session
        message = data.message
        content_type = message.content_type.lower()
        if content_type.startswith(('text/', 'image/')):
            stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
            self.incoming_message_queue.send((session, 'message', data))
        elif content_type == 'application/blink-screensharing':
            stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
            self.add_screen_image(message.sender, message.content)
        elif content_type == 'application/blink-zrtp-sas':
            if not self.config.zrtp_auto_verify:
                stream.msrp_session.send_report(notification.data.chunk, 413, 'Unwanted message')
                return
            try:
                audio_stream = next(stream for stream in session.streams if stream.type=='audio' and stream.encryption.active and stream.encryption.type=='ZRTP')
            except StopIteration:
                stream.msrp_session.send_report(notification.data.chunk, 413, 'Unwanted message')
                return
            # Only trust it if there was a direct path and the transport is TLS
            secure_chat = stream.transport == 'tls' and all(len(path)==1 for path in (stream.msrp.full_local_path, stream.msrp.full_remote_path))
            remote_sas = str(message.content)
            if remote_sas == audio_stream.encryption.zrtp.sas and secure_chat:
                audio_stream.encryption.zrtp.verified = True
                stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
            else:
                stream.msrp_session.send_report(notification.data.chunk, 413, 'Unwanted message')
        else:
            stream.msrp_session.send_report(notification.data.chunk, 413, 'Unwanted message')

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
                log.info(u'Room %s - %s has put the audio session on hold' % (self.uri, format_identity(session.remote_identity)))
            else:
                log.info(u'Room %s - %s has taken the audio session out of hold' % (self.uri, format_identity(session.remote_identity)))
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
            timer = reactor.callLater(3, self._accept_proposal, session, streams)
            old_timer = getattr(session, 'proposal_timer', None)
            assert old_timer is None
            session.proposal_timer = timer

    def _NH_SIPSessionProposalRejected(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            timer = getattr(session, 'proposal_timer', None)
            if timer is not None:
                timer.cancel()
            session.proposal_timer = None

    def _NH_SIPSessionHadProposalFailure(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            timer = getattr(session, 'proposal_timer', None)
            assert timer is not None
            timer.cancel()
            session.proposal_timer = None

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        session = notification.sender
        for stream in notification.data.added_streams:
            notification.center.add_observer(self, sender=stream)
            txt = u'%s has added %s' % (format_identity(session.remote_identity), stream.type)
            log.info(u'Room %s - %s' % (self.uri, txt))
            self.dispatch_server_message(txt, exclude=session)
            if stream.type == 'audio':
                log.info(u'Room %s - audio stream %s/%sHz, end-points: %s:%d <-> %s:%d' % (self.uri, stream.codec, stream.sample_rate,
                                                                                          stream.local_rtp_address, stream.local_rtp_port,
                                                                                          stream.remote_rtp_address, stream.remote_rtp_port))
                if stream.encryption.type != 'ZRTP':
                    # We don't listen for stream notifications early enough
                    if stream.encryption.active:
                        log.info(u'Room %s - %s %s stream enabled %s encryption' % (self.uri,
                                                                                   format_identity(session.remote_identity),
                                                                                   stream.type,
                                                                                   stream.encryption.type))
                    else:
                        log.info(u'Room %s - %s %s stream did not enable encryption' % (self.uri,
                                                                                       format_identity(session.remote_identity),
                                                                                       stream.type))

        if notification.data.added_streams:
            welcome_handler = WelcomeHandler(self, initial=False, session=session, streams=notification.data.added_streams)
            welcome_handler.run()

        for stream in notification.data.removed_streams:
            notification.center.remove_observer(self, sender=stream)
            txt = u'%s has removed %s' % (format_identity(session.remote_identity), stream.type)
            log.info(u'Room %s - %s' % (self.uri, txt))
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
                log.info(u'Room %s - %s has removed all streams, session will be terminated' % (self.uri, format_identity(session.remote_identity)))
                session.end()
        self.dispatch_conference_info()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        log.info(u'Room %s - Call transfer request rejected, REFER must be out of dialog (RFC4579 5.5)' % self.uri)
        notification.sender.reject_transfer(403)

    def _NH_SIPSessionWillEnd(self, notification):
        session = notification.sender
        timer = getattr(session, 'proposal_timer', None)
        if timer is not None and timer.active():
            timer.cancel()
        session.proposal_timer = None

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
        files = glob('%s/*.wav' % Resources.get('sounds/moh'))
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

    def __init__(self, room, initial, session, streams):
        self.room = room
        self.initial = initial
        self.session = session
        self.streams = streams
        self.procs = proc.RunningProcSet()

    @run_in_green_thread
    def run(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)

        for stream in self.streams:
            if stream.type == 'audio':
                self.procs.spawn(self.audio_welcome, stream)
            elif stream.type == 'chat':
                self.procs.spawn(self.chat_welcome, stream)
        self.procs.waitall()

        notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.streams = None
        self.room = None
        self.procs = None

    def play_file_in_player(self, player, file, delay):
        player.filename = file
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError as e:
            log.warning(u'Error playing file %s: %s' % (file, e))

    def audio_welcome(self, stream):
        player = WavePlayer(stream.mixer, '', pause_time=1, initial_delay=1, volume=50)
        stream.bridge.add(player)
        try:
            if self.initial:
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
            stream.bridge.remove(player)
            self.room.audio_conference.add(stream)
            self.room.audio_conference.unhold()
            if len(self.room.audio_conference.streams) == 1:
                self.room.moh_player.play()
            else:
                self.room.moh_player.pause()
        finally:
            player.stop()

    def chat_welcome(self, stream):
        if self.initial:
            txt = 'Welcome to SylkServer!'
        else:
            txt = ''
        user_count = len({str(s.remote_identity.uri) for s in self.room.sessions if s.remote_identity.uri != self.session.remote_identity.uri})
        if user_count == 0:
            txt += ' You are the first participant'
        else:
            if user_count == 1:
                txt += ' There is one more participant'
            else:
                txt += ' There are %s more participants' % user_count
        txt +=  ' in this conference room.'
        if self.room.config.advertise_xmpp_support or self.room.config.pstn_access_numbers or self.room.config.webrtc_gateway_url:
            txt += '\n\nOther participants can join at these addresses:\n\n'
            txt += '    - Using a SIP client, initiate a session to %s (audio and chat)\n' % self.room.uri
            if self.room.config.webrtc_gateway_url:
                webrtc_url = str(self.room.config.webrtc_gateway_url).replace('$room', self.room.uri)
                txt += '    - Using a WebRTC enabled browser go to %s (audio only)\n' % webrtc_url
            if self.room.config.advertise_xmpp_support:
                txt += '    - Using an XMPP Jingle capable client, add contact %s and call it (audio and chat)\n' % self.room.uri
            if self.room.config.pstn_access_numbers:
                if len(self.room.config.pstn_access_numbers) == 1:
                    nums = self.room.config.pstn_access_numbers[0]
                else:
                    nums = ', '.join(self.room.config.pstn_access_numbers[:-1]) + ' or %s' % self.room.config.pstn_access_numbers[-1]
                txt += '    - Using a landline or mobile phone, dial %s (audio only)\n' % nums
        stream.send_message(txt, 'text/plain', sender=self.room.identity, recipients=[self.room.identity])
        for msg in self.room.history:
            stream.send_message(msg.content, msg.content_type, sender=msg.sender, recipients=[self.room.identity], timestamp=msg.timestamp)

        # Send ZRTP SAS over the chat stream, if applicable
        if self.room.config.zrtp_auto_verify:
            session = stream.session
            try:
                audio_stream = next(stream for stream in session.streams if stream.type=='audio')
            except StopIteration:
                pass
            else:
                if audio_stream.encryption.type == 'ZRTP' and audio_stream.encryption.active:
                    # Only send the message if there are no relays in between
                    secure_chat = stream.transport == 'tls' and all(len(path)==1 for path in (stream.msrp.full_local_path, stream.msrp.full_remote_path))
                    sas = audio_stream.encryption.zrtp.sas
                    if sas is not None and secure_chat:
                        txt = 'Received ZRTP Short Authentication String: %s' % sas
                        # Don't set the remote identity, that way it will appear as a private message
                        ns = CPIMNamespace('urn:ag-projects:xml:ns:cpim', prefix='agp')
                        message_type = CPIMHeader('Message-Type', ns, 'status')
                        stream.send_message(txt, 'text/plain', sender=self.room.identity, additional_headers=[message_type])

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionWillEnd(self, notification):
        self.procs.killall()


class RoomFile(object):
    def __init__(self, name, hash, size, sender):
        self.name = name
        self.hash = hash
        self.size = size
        self.sender = sender

    @property
    def file_selector(self):
        return FileSelector.for_file(self.name, hash=self.hash)


class FileTransferHandler(object):
    implements(IObserver)

    def __init__(self, room):
        self.room = weakref.ref(room)
        self.session = None
        self.stream = None
        self.handler = None
        self.direction = None

    def init_incoming(self, stream):
        self.direction = 'incoming'
        self.stream = stream
        self.session = stream.session
        self.handler = stream.handler
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.stream)
        notification_center.add_observer(self, sender=self.handler)

    @run_in_green_thread
    def init_outgoing(self, destination, file):
        self.direction = 'outgoing'

        room = self.room()
        if room is None:
            return

        settings = SIPSimpleSettings()
        account = DefaultAccount()
        if account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host,
                         port=account.sip.outbound_proxy.port,
                         parameters={'transport': account.sip.outbound_proxy.transport})
        else:
            uri = SIPURI.new(destination)
        lookup = DNSLookup()
        try:
            route = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()[0]
        except (DNSLookupError, IndexError):
            return

        self.session = Session(account)
        self.stream = MediaStreamRegistry.get('file-transfer')(file.file_selector, 'sendonly')
        self.handler = self.stream.handler
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.stream)
        notification_center.add_observer(self, sender=self.handler)

        from_header = FromHeader(SIPURI.new(room.identity.uri), u'Conference File Transfer')
        to_header = ToHeader(SIPURI.new(destination))
        extra_headers = []
        if ThorNodeConfig.enabled:
            extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
        extra_headers.append(Header('X-Originator-From', str(file.sender.uri)))
        extra_headers.append(SubjectHeader(u'File uploaded by %s' % file.sender))
        self.session.connect(from_header, to_header, route=route, streams=[self.stream], is_focus=True, extra_headers=extra_headers)

    def _terminate(self, failure_reason=None):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.handler)

        room = self.room()
        if room is not None:
            if failure_reason is None:
                if self.direction == 'incoming' and self.stream.direction == 'recvonly':
                    sender = ChatIdentity(self.session.remote_identity.uri, self.session.remote_identity.display_name)
                    file = RoomFile(self.stream.file_selector.name, self.stream.file_selector.hash, self.stream.file_selector.size, sender)
                    room.add_file(file)
            else:
                room.dispatch_server_message('File transfer for %s failed: %s' % (os.path.basename(self.stream.file_selector.name), failure_reason))

        self.session = None
        self.stream = None
        self.handler = None

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_MediaStreamDidNotInitialize(self, notification):
        self._terminate(failure_reason=notification.data.reason)

    def _NH_FileTransferHandlerDidEnd(self, notification):
        if self.direction == 'incoming':
            if self.stream.direction == 'sendonly':
                reactor.callLater(3, self.session.end)
            else:
                reactor.callLater(1, self.session.end)
        else:
            self.session.end()
        self._terminate(failure_reason=notification.data.reason)

