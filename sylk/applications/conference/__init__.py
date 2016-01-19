
import os
import re
import shutil

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.core import Header, FromHeader, ToHeader, SubjectHeader
from sipsimple.lookup import DNSLookup
from sipsimple.streams import MediaStreamRegistry
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from zope.interface import implements

from sylk.accounts import DefaultAccount
from sylk.applications import SylkApplication
from sylk.applications.conference.configuration import get_room_config, ConferenceConfig
from sylk.applications.conference.logger import log
from sylk.applications.conference.room import Room
from sylk.applications.conference.web import ConferenceWeb
from sylk.bonjour import BonjourService
from sylk.configuration import ServerConfig, ThorNodeConfig
from sylk.session import Session, IllegalStateError
from sylk.web import server as web_server


class ACLValidationError(Exception): pass

class RoomNotFoundError(Exception): pass


class ConferenceApplication(SylkApplication):
    implements(IObserver)

    def __init__(self):
        self._rooms = {}
        self.invited_participants_map = {}
        self.bonjour_focus_service = Null
        self.bonjour_room_service = Null
        self.web = Null

    def start(self):
        self.web = ConferenceWeb(self)
        web_server.register_resource('conference', self.web.resource())

        # cleanup old files
        for path in (ConferenceConfig.file_transfer_dir, ConferenceConfig.screensharing_images_dir):
            try:
                shutil.rmtree(path)
            except EnvironmentError:
                pass

        if ServerConfig.enable_bonjour and ServerConfig.default_application == 'conference':
            self.bonjour_focus_service = BonjourService(service='sipfocus')
            self.bonjour_focus_service.start()
            log.msg("Bonjour publication started for service 'sipfocus'")
            self.bonjour_room_service = BonjourService(service='sipuri', name='Conference Room', uri_user='conference')
            self.bonjour_room_service.start()
            self.bonjour_room_service.presence_state = BonjourPresenceState('available', u'No participants')
            log.msg("Bonjour publication started for service 'sipuri'")

    def stop(self):
        self.bonjour_focus_service.stop()
        self.bonjour_room_service.stop()

    def get_room(self, uri, create=False):
        room_uri = '%s@%s' % (uri.user, uri.host)
        try:
            room = self._rooms[room_uri]
        except KeyError:
            if create:
                room = Room(room_uri)
                self._rooms[room_uri] = room
                return room
            else:
                raise RoomNotFoundError
        else:
            return room

    def remove_room(self, uri):
        room_uri = '%s@%s' % (uri.user, uri.host)
        self._rooms.pop(room_uri, None)

    def validate_acl(self, room_uri, from_uri):
        room_uri = '%s@%s' % (room_uri.user, room_uri.host)
        cfg = get_room_config(room_uri)
        if cfg.access_policy == 'allow,deny':
            if cfg.allow.match(from_uri) and not cfg.deny.match(from_uri):
                return
            raise ACLValidationError
        else:
            if cfg.deny.match(from_uri) and not cfg.allow.match(from_uri):
                raise ACLValidationError

    def incoming_session(self, session):
        log.msg('New session from %s to %s' % (session.remote_identity.uri, session.local_identity.uri))
        audio_streams = [stream for stream in session.proposed_streams if stream.type=='audio']
        chat_streams = [stream for stream in session.proposed_streams if stream.type=='chat']
        transfer_streams = [stream for stream in session.proposed_streams if stream.type=='file-transfer']
        if not audio_streams and not chat_streams and not transfer_streams:
            log.msg(u'Session rejected: invalid media, only RTP audio and MSRP chat are supported')
            session.reject(488)
            return
        audio_stream = audio_streams[0] if audio_streams else None
        chat_stream = chat_streams[0] if chat_streams else None
        transfer_stream = transfer_streams[0] if transfer_streams else None

        try:
            self.validate_acl(session.request_uri, session.remote_identity.uri)
        except ACLValidationError:
            log.msg(u'Session rejected: unauthorized by access list')
            session.reject(403)
            return

        if transfer_stream is not None:
            try:
                room = self.get_room(session.request_uri)
            except RoomNotFoundError:
                log.msg(u'Session rejected: room not found')
                session.reject(404)
                return
            if transfer_stream.direction == 'sendonly':
                # file transfer 'pull'
                try:
                    file = next(file for file in room.files if file.hash == transfer_stream.file_selector.hash)
                except StopIteration:
                    log.msg(u'Session rejected: requested file not found')
                    session.reject(404)
                    return
                try:
                    transfer_stream.file_selector = file.file_selector
                except EnvironmentError, e:
                    log.msg(u'Session rejected: error opening requested file: %s' % e)
                    session.reject(404)
                    return
            else:
                transfer_stream.handler.save_directory = os.path.join(ConferenceConfig.file_transfer_dir.normalized, room.uri)

        NotificationCenter().add_observer(self, sender=session)
        if audio_stream:
            session.send_ring_indication()
        streams = [stream for stream in (audio_stream, chat_stream, transfer_stream) if stream]
        reactor.callLater(4 if audio_stream is not None else 0, self.accept_session, session, streams)

    def incoming_subscription(self, subscribe_request, data):
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        if Null in (from_header, to_header):
            subscribe_request.reject(400)
            return

        if subscribe_request.event != 'conference':
            log.msg(u'Subscription for event %s rejected: only conference event is supported' % subscribe_request.event)
            subscribe_request.reject(489)
            return

        try:
            self.validate_acl(data.request_uri, from_header.uri)
        except ACLValidationError:
            try:
                self.validate_acl(to_header.uri, from_header.uri)
            except ACLValidationError:
                # Check if we need to skip the ACL because this was an invited participant
                if not (str(from_header.uri) in self.invited_participants_map.get('%s@%s' % (data.request_uri.user, data.request_uri.host), {}) or
                        str(from_header.uri) in self.invited_participants_map.get('%s@%s' % (to_header.uri.user, to_header.uri.host), {})):
                    log.msg(u'Subscription rejected: unauthorized by access list')
                    subscribe_request.reject(403)
                    return
        try:
            room = self.get_room(data.request_uri)
        except RoomNotFoundError:
            try:
                room = self.get_room(to_header.uri)
            except RoomNotFoundError:
                log.msg(u'Subscription rejected: room not yet created')
                subscribe_request.reject(480)
                return
        if not room.started:
            log.msg(u'Subscription rejected: room not started yet')
            subscribe_request.reject(480)
        else:
            room.handle_incoming_subscription(subscribe_request, data)

    def incoming_referral(self, refer_request, data):
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        refer_to_header = data.headers.get('Refer-To', Null)
        if Null in (from_header, to_header, refer_to_header):
            refer_request.reject(400)
            return

        log.msg(u'Room %s - join request from %s to %s' % ('%s@%s' % (to_header.uri.user, to_header.uri.host), from_header.uri, refer_to_header.uri))

        try:
            self.validate_acl(data.request_uri, from_header.uri)
        except ACLValidationError:
            log.msg(u'Room %s - invite participant request rejected: unauthorized by access list' % data.request_uri)
            refer_request.reject(403)
            return
        referral_handler = IncomingReferralHandler(refer_request, data)
        referral_handler.start()

    def incoming_message(self, message_request, data):
        log.msg(u'SIP MESSAGE is not supported, use MSRP media instead')
        message_request.answer(405)

    def accept_session(self, session, streams):
        if session.state == 'incoming':
            try:
                session.accept(streams, is_focus=True)
            except IllegalStateError:
                pass

    def add_participant(self, session, room_uri):
        # Keep track of the invited participants, we must skip ACL policy
        # for SUBSCRIBE requests
        room_uri_str = '%s@%s' % (room_uri.user, room_uri.host)
        log.msg(u'Room %s - outgoing session to %s started' % (room_uri_str, session.remote_identity.uri))
        d = self.invited_participants_map.setdefault(room_uri_str, {})
        d.setdefault(str(session.remote_identity.uri), 0)
        d[str(session.remote_identity.uri)] += 1
        NotificationCenter().add_observer(self, sender=session)
        room = self.get_room(room_uri, True)
        room.start()
        room.add_session(session)

    def remove_participant(self, participant_uri, room_uri):
        try:
            room = self.get_room(room_uri)
        except RoomNotFoundError:
            pass
        else:
            log.msg('Room %s - %s removed from conference' % (room_uri, participant_uri))
            room.terminate_sessions(participant_uri)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        room = self.get_room(session.request_uri, True)
        room.start()
        room.add_session(session)

    @run_in_green_thread
    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        notification.center.remove_observer(self, sender=session)
        if session.direction == 'incoming':
            room_uri = session.request_uri
        else:
            # Clear invited participants mapping
            room_uri_str = '%s@%s' % (session.local_identity.uri.user, session.local_identity.uri.host)
            d = self.invited_participants_map[room_uri_str]
            d[str(session.remote_identity.uri)] -= 1
            if d[str(session.remote_identity.uri)] == 0:
                del d[str(session.remote_identity.uri)]
            room_uri = session.local_identity.uri
        # We could get this notifiction even if we didn't get SIPSessionDidStart
        try:
            room = self.get_room(room_uri)
        except RoomNotFoundError:
            return
        if session in room.sessions:
            room.remove_session(session)
        if not room.stopping and room.empty:
            self.remove_room(room_uri)
            room.stop()

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        notification.center.remove_observer(self, sender=session)
        log.msg(u'Session from %s failed: %s' % (session.remote_identity.uri, notification.data.reason))


class IncomingReferralHandler(object):
    implements(IObserver)

    def __init__(self, refer_request, data):
        self._refer_request = refer_request
        self._refer_headers = data.headers
        self.room_uri = data.request_uri
        self.room_uri_str = '%s@%s' % (self.room_uri.user, self.room_uri.host)
        self.refer_to_uri = re.sub('<|>', '', data.headers.get('Refer-To').uri)
        self.method = data.headers.get('Refer-To').parameters.get('method', 'INVITE').upper()
        self.session = None
        self.streams = []

    def start(self):
        if not self.refer_to_uri.startswith(('sip:', 'sips:')):
            self.refer_to_uri = 'sip:%s' % self.refer_to_uri
        try:
            self.refer_to_uri = SIPURI.parse(self.refer_to_uri)
        except SIPCoreError:
            log.msg('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
            self._refer_request.reject(488)
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._refer_request)
        if self.method == 'INVITE':
            self._refer_request.accept()
            settings = SIPSimpleSettings()
            account = DefaultAccount()
            if account.sip.outbound_proxy is not None:
                uri = SIPURI(host=account.sip.outbound_proxy.host,
                             port=account.sip.outbound_proxy.port,
                             parameters={'transport': account.sip.outbound_proxy.transport})
            else:
                uri = self.refer_to_uri
            lookup = DNSLookup()
            notification_center.add_observer(self, sender=lookup)
            lookup.lookup_sip_proxy(uri, settings.sip.transport_list)
        elif self.method == 'BYE':
            log.msg('Room %s - %s removed %s from the room' % (self.room_uri_str, self._refer_headers.get('From').uri, self.refer_to_uri))
            self._refer_request.accept()
            conference_application = ConferenceApplication()
            conference_application.remove_participant(self.refer_to_uri, self.room_uri)
            self._refer_request.end(200)
        else:
            self._refer_request.reject(488)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        account = DefaultAccount()
        conference_application = ConferenceApplication()
        try:
            room = conference_application.get_room(self.room_uri)
        except RoomNotFoundError:
            log.msg('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
            self._refer_request.end(500)
            return
        active_media = set(room.active_media).intersection(('audio', 'chat'))
        if not active_media:
            log.msg('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
            self._refer_request.end(500)
            return
        registry = MediaStreamRegistry()
        for stream_type in active_media:
            self.streams.append(registry.get(stream_type)())
        self.session = Session(account)
        notification_center.add_observer(self, sender=self.session)
        original_from_header = self._refer_headers.get('From')
        if original_from_header.display_name:
            original_identity = "%s <%s@%s>" % (original_from_header.display_name, original_from_header.uri.user, original_from_header.uri.host)
        else:
            original_identity = "%s@%s" % (original_from_header.uri.user, original_from_header.uri.host)
        from_header = FromHeader(SIPURI.new(self.room_uri), u'Conference Call')
        to_header = ToHeader(self.refer_to_uri)
        extra_headers = []
        if self._refer_headers.get('Referred-By', None) is not None:
            extra_headers.append(Header.new(self._refer_headers.get('Referred-By')))
        else:
            extra_headers.append(Header('Referred-By', str(original_from_header.uri)))
        if ThorNodeConfig.enabled:
            extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
        extra_headers.append(Header('X-Originator-From', str(original_from_header.uri)))
        extra_headers.append(SubjectHeader(u'Join conference request from %s' % original_identity))
        route = notification.data.result[0]
        self.session.connect(from_header, to_header, route=route, streams=self.streams, is_focus=True, extra_headers=extra_headers)

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)

    def _NH_SIPSessionGotRingIndication(self, notification):
        if self._refer_request is not None:
            self._refer_request.send_notify(180)

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        if self._refer_request is not None:
            self._refer_request.send_notify(notification.data.code, notification.data.reason)

    def _NH_SIPSessionDidStart(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        conference_application = ConferenceApplication()
        conference_application.add_participant(self.session, self.room_uri)
        log.msg('Room %s - %s added %s' % (self.room_uri_str, self._refer_headers.get('From').uri, self.refer_to_uri))
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidFail(self, notification):
        log.msg('Room %s - failed to add %s: %s' % (self.room_uri_str, self.refer_to_uri, notification.data.reason))
        notification.center.remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(notification.data.code or 500, notification.data.reason or  notification.data.code)
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidEnd(self, notification):
        # If any stream fails to start we won't get SIPSessionDidFail, we'll get here instead
        log.msg('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
        notification.center.remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        self.session = None
        self.streams = []

    def _NH_SIPIncomingReferralDidEnd(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self._refer_request = None


