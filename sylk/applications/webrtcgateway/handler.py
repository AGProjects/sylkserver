
import hashlib
import json
import random
import re
import time
import uuid

from application.python import limit
from application.python.weakref import defaultweakobjectmap
from application.system import makedirs
from eventlib import coros, proc
from eventlib.twistedutil import block_on
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.threading.green import call_in_green_thread, run_in_green_thread
from string import maketrans
from twisted.internet import reactor

from sylk.applications.webrtcgateway.configuration import GeneralConfig, get_room_config
from sylk.applications.webrtcgateway.janus import JanusError
from sylk.applications.webrtcgateway.logger import ConnectionLogger, VideoroomLogger
from sylk.applications.webrtcgateway.models import sylkrtc
from sylk.applications.webrtcgateway.storage import TokenStorage
from sylk.applications.webrtcgateway.util import GreenEvent


SIP_PREFIX_RE = re.compile('^sips?:')


class AccountInfo(object):
    def __init__(self, id, password, display_name=None, user_agent=None):
        self.id = id
        self.password = password
        self.display_name = display_name
        self.user_agent = user_agent
        self.registration_state = None
        self.janus_handle_id = None

    @property
    def uri(self):
        return 'sip:%s' % self.id

    @property
    def user_data(self):
        return dict(username=self.uri, display_name=self.display_name, user_agent=self.user_agent, ha1_secret=self.password)


class SessionPartyIdentity(object):
    def __init__(self, uri, display_name=''):
        self.uri = uri
        self.display_name = display_name


# todo: might need to replace this auto-resetting descriptor with a timer in case we need to know when the slow link state expired
#

class SlowLinkState(object):
    def __init__(self):
        self.slow_link = False
        self.last_reported = 0


class SlowLinkDescriptor(object):
    __timeout__ = 30  # 30 seconds

    def __init__(self):
        self.values = defaultweakobjectmap(SlowLinkState)

    def __get__(self, instance, owner):
        if instance is None:
            return self
        state = self.values[instance]
        if state.slow_link and time.time() - state.last_reported > self.__timeout__:
            state.slow_link = False
        return state.slow_link

    def __set__(self, instance, value):
        state = self.values[instance]
        if value:
            state.last_reported = time.time()
        state.slow_link = bool(value)

    def __delete__(self, instance):
        raise AttributeError('Attribute cannot be deleted')


class SIPSessionInfo(object):
    slow_download = SlowLinkDescriptor()
    slow_upload = SlowLinkDescriptor()

    def __init__(self, id):
        self.id = id
        self.direction = None
        self.state = None
        self.account_id = None
        self.local_identity = None     # instance of SessionPartyIdentity
        self.remote_identity = None    # instance of SessionPartyIdentity
        self.janus_handle_id = None
        self.slow_download = False
        self.slow_upload = False

    def init_outgoing(self, account_id, destination):
        self.account_id = account_id
        self.direction = 'outgoing'
        self.state = 'connecting'
        self.local_identity = SessionPartyIdentity(account_id)
        self.remote_identity = SessionPartyIdentity(destination)

    def init_incoming(self, account_id, originator, originator_display_name=''):
        self.account_id = account_id
        self.direction = 'incoming'
        self.state = 'connecting'
        self.local_identity = SessionPartyIdentity(account_id)
        self.remote_identity = SessionPartyIdentity(originator, originator_display_name)


class VideoRoom(object):
    def __init__(self, uri):
        self.id = random.getrandbits(32)    # janus needs numeric room names
        self.uri = uri
        self.config = get_room_config(uri)
        self.log = VideoroomLogger(self)
        self._active_participants = []
        self._sessions = set()
        self._id_map = {}  # map session.id -> session and session.publisher_id -> session
        if self.config.record:
            makedirs(self.config.recording_dir, 0o755)
            self.log.info('created (recording on)')
        else:
            self.log.info('created')

    @property
    def active_participants(self):
        return self._active_participants

    @active_participants.setter
    def active_participants(self, participant_list):
        unknown_participants = set(participant_list).difference(self._id_map)
        if unknown_participants:
            raise ValueError('unknown participant session id: {}'.format(', '.join(unknown_participants)))
        if self._active_participants != participant_list:
            self._active_participants = participant_list
            self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
            self._update_bitrate()

    def add(self, session):
        assert session not in self._sessions
        assert session.publisher_id is not None
        assert session.publisher_id not in self._id_map and session.id not in self._id_map
        self._sessions.add(session)
        self._id_map[session.id] = self._id_map[session.publisher_id] = session
        self.log.info('{session.account_id} has joined the room'.format(session=session))
        self._update_bitrate()
        if self._active_participants:
            session.owner.notify(sylkrtc.VideoRoomConfigurationEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))

    def discard(self, session):
        if session in self._sessions:
            self._sessions.discard(session)
            self._id_map.pop(session.id, None)
            self._id_map.pop(session.publisher_id, None)
            self.log.info('{session.account_id} has left the room'.format(session=session))
            if session.id in self._active_participants:
                self._active_participants.remove(session.id)
                self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
                for session in self._sessions:
                    session.owner.notify(sylkrtc.VideoRoomConfigurationEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
            self._update_bitrate()

    def remove(self, session):
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.publisher_id)
        self.log.info('{session.account_id} has left the room'.format(session=session))
        if session.id in self._active_participants:
            self._active_participants.remove(session.id)
            self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
            for session in self._sessions:
                session.owner.notify(sylkrtc.VideoRoomConfigurationEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
        self._update_bitrate()

    def clear(self):
        for session in self._sessions:
            self.log.info('{session.account_id} has left the room'.format(session=session))
        self._active_participants = []
        self._sessions.clear()
        self._id_map.clear()

    def allow_uri(self, uri):
        config = self.config
        if config.access_policy == 'allow,deny':
            return config.allow.match(uri) and not config.deny.match(uri)
        else:
            return not config.deny.match(uri) or config.allow.match(uri)

    def _update_bitrate(self):
        if self._sessions:
            if self._active_participants:
                # todo: should we use max_bitrate / 2 or max_bitrate for each active participant if there are 2 active participants?
                active_participant_bitrate = self.config.max_bitrate // len(self._active_participants)
                other_participant_bitrate = 100000
                self.log.info('participant bitrate is {} (active) / {} (others)'.format(active_participant_bitrate, other_participant_bitrate))
                for session in self._sessions:
                    if session.id in self._active_participants:
                        bitrate = active_participant_bitrate
                    else:
                        bitrate = other_participant_bitrate
                    if session.bitrate != bitrate:
                        session.bitrate = bitrate
                        data = dict(request='configure', room=self.id, bitrate=bitrate)
                        session.owner.protocol.backend.janus_message(session.owner.janus_session_id, session.janus_handle_id, data)
            else:
                bitrate = self.config.max_bitrate // limit(len(self._sessions) - 1, min=1)
                self.log.info('participant bitrate is {}'.format(bitrate))
                for session in self._sessions:
                    if session.bitrate != bitrate:
                        session.bitrate = bitrate
                        data = dict(request='configure', room=self.id, bitrate=bitrate)
                        session.owner.protocol.backend.janus_message(session.owner.janus_session_id, session.janus_handle_id, data)

    # todo: make VideoRoom be a context manager that is retained/released on enter/exit and implement __nonzero__ to be different from __len__
    # todo: so that a videoroom is not accidentally released by the last participant leaving while a new participant waits to join
    # todo: this needs a new model for communication with janus and the client that is pseudo-synchronous (uses green threads)

    def __len__(self):
        return len(self._sessions)

    def __iter__(self):
        return iter(self._sessions)

    def __getitem__(self, key):
        return self._id_map[key]

    def __contains__(self, item):
        return item in self._id_map or item in self._sessions


class PublisherFeedContainer(object):
    """A container for the other participant's publisher sessions that we have subscribed to"""

    def __init__(self):
        self._publishers = set()
        self._id_map = {}  # map publisher.id -> publisher and publisher.publisher_id -> publisher

    def add(self, session):
        assert session not in self._publishers
        assert session.id not in self._id_map and session.publisher_id not in self._id_map
        self._publishers.add(session)
        self._id_map[session.id] = self._id_map[session.publisher_id] = session

    def discard(self, item):  # item can be any of session, session.id or session.publisher_id
        session = self._id_map[item] if item in self._id_map else item if item in self._publishers else None
        if session is not None:
            self._publishers.discard(session)
            self._id_map.pop(session.id, None)
            self._id_map.pop(session.publisher_id, None)

    def remove(self, item):  # item can be any of session, session.id or session.publisher_id
        session = self._id_map[item] if item in self._id_map else item
        self._publishers.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.publisher_id)

    def pop(self, item):  # item can be any of session, session.id or session.publisher_id
        session = self._id_map[item] if item in self._id_map else item
        self._publishers.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.publisher_id)
        return session

    def clear(self):
        self._publishers.clear()
        self._id_map.clear()

    def __len__(self):
        return len(self._publishers)

    def __iter__(self):
        return iter(self._publishers)

    def __getitem__(self, key):
        return self._id_map[key]

    def __contains__(self, item):
        return item in self._id_map or item in self._publishers


class VideoRoomSessionInfo(object):
    slow_download = SlowLinkDescriptor()
    slow_upload = SlowLinkDescriptor()

    def __init__(self, id, owner):
        self.id = id
        self.owner = owner
        self.account_id = None
        self.type = None          # publisher / subscriber
        self.publisher_id = None  # janus publisher ID for publishers / publisher session ID for subscribers
        self.janus_handle_id = None
        self.room = None
        self.bitrate = None
        self.parent_session = None
        self.slow_download = False
        self.slow_upload = False
        self.feeds = PublisherFeedContainer()  # keeps references to all the other participant's publisher feeds that we subscribed to

    def initialize(self, account_id, type, room):
        assert type in ('publisher', 'subscriber')
        self.account_id = account_id
        self.type = type
        self.room = room
        self.bitrate = room.config.max_bitrate

    def __repr__(self):
        return '<%s: id=%s janus_handle_id=%s type=%s>' % (self.__class__.__name__, self.id, self.janus_handle_id, self.type)


class SessionContainer(object):
    def __init__(self):
        self._sessions = set()
        self._id_map = {}  # map session.id -> session and session.janus_handle_id -> session

    def add(self, session):
        assert session not in self._sessions
        assert session.id not in self._id_map and session.janus_handle_id not in self._id_map
        self._sessions.add(session)
        self._id_map[session.id] = self._id_map[session.janus_handle_id] = session

    def discard(self, item):  # item can be any of session, session.id or session.janus_handle_id
        session = self._id_map[item] if item in self._id_map else item if item in self._sessions else None
        if session is not None:
            self._sessions.discard(session)
            self._id_map.pop(session.id, None)
            self._id_map.pop(session.janus_handle_id, None)

    def remove(self, item):  # item can be any of session, session.id or session.janus_handle_id
        session = self._id_map[item] if item in self._id_map else item
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.janus_handle_id)

    def pop(self, item):  # item can be any of session, session.id or session.janus_handle_id
        session = self._id_map[item] if item in self._id_map else item
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.janus_handle_id)
        return session

    def clear(self):
        self._sessions.clear()
        self._id_map.clear()

    def __len__(self):
        return len(self._sessions)

    def __iter__(self):
        return iter(self._sessions)

    def __getitem__(self, key):
        return self._id_map[key]

    def __contains__(self, item):
        return item in self._id_map or item in self._sessions


class OperationName(str):
    __normalizer__ = maketrans('-', '_')

    @property
    def normalized(self):
        return self.translate(self.__normalizer__)


class Operation(object):
    __slots__ = 'type', 'name', 'data'
    __types__ = 'request', 'event'

    # noinspection PyShadowingBuiltins
    def __init__(self, type, name, data):
        if type not in self.__types__:
            raise ValueError("Can't instantiate class {.__class__.__name__} with unknown type: {!r}".format(self, type))
        self.type = type
        self.name = OperationName(name)
        self.data = data


class APIError(Exception):
    pass


class ConnectionHandler(object):
    def __init__(self, protocol):
        self.protocol = protocol
        self.device_id = hashlib.md5(protocol.peer).digest().encode('base64').rstrip('=\n')
        self.janus_session_id = None
        self.accounts_map = {}  # account ID -> account
        self.account_handles_map = {}  # Janus handle ID -> account
        self.sip_sessions = SessionContainer()  # keeps references to all the SIP sessions created or received by this device
        self.videoroom_sessions = SessionContainer()  # keeps references to all the videoroom sessions created by this participant (as publisher and subscriber)
        self.ready_event = GreenEvent()
        self.resolver = DNSLookup()
        self.proc = proc.spawn(self._operations_handler)
        self.operations_queue = coros.queue()
        self.log = ConnectionLogger(self)
        self.state = None
        self._stop_pending = False

    @run_in_green_thread
    def start(self):
        self.state = 'starting'
        try:
            self.janus_session_id = block_on(self.protocol.backend.janus_create_session())
            self.protocol.backend.janus_start_keepalive(self.janus_session_id)
        except Exception as e:
            self.state = 'failed'
            self.log.warning('could not create session, disconnecting: %s' % e)
            if self._stop_pending:  # if stop was already called it means we were already disconnected
                self.stop()
            else:
                self.protocol.disconnect(3000, unicode(e))
        else:
            self.state = 'started'
            self.ready_event.set()
            if self._stop_pending:
                self.stop()
            else:
                self._send_response(sylkrtc.ReadyEvent())

    def stop(self):
        if self.state in (None, 'starting'):
            self._stop_pending = True
            return
        self.state = 'stopping'
        self._stop_pending = False
        if self.proc is not None:  # Kill the operation's handler proc first, in order to not have any operations active while we cleanup.
            self.proc.kill()        # Also proc.kill() will switch to another green thread, which is another reason to do it first so that
            self.proc = None        # we do not switch to another green thread in the middle of the cleanup with a partially deleted handler
        if self.ready_event.is_set():
            # Do not explicitly detach the janus plugin handles before destroying the janus session. Janus runs each request in a different
            # thread, so making detach and destroy request without waiting for the detach to finish can result in errors from race conditions.
            # Because we do not want to wait for them, we will rely instead on the fact that janus automatically detaches the plugin handles
            # when it destroys a session, so we only remove our event handlers and issue a destroy request for the session.
            for account_info in self.accounts_map.values():
                if account_info.janus_handle_id is not None:
                    self.protocol.backend.janus_set_event_handler(account_info.janus_handle_id, None)
            for session in self.sip_sessions:
                if session.janus_handle_id is not None:
                    self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)
            for session in self.videoroom_sessions:
                if session.janus_handle_id is not None:
                    self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)
                session.room.discard(session)
                session.feeds.clear()
            self.protocol.backend.janus_stop_keepalive(self.janus_session_id)
            self.protocol.backend.janus_destroy_session(self.janus_session_id)  # this automatically detaches all plugin handles associated with it, not need to manually do it
        # cleanup
        self.ready_event.clear()
        self.accounts_map.clear()
        self.account_handles_map.clear()
        self.sip_sessions.clear()
        self.videoroom_sessions.clear()
        self.janus_session_id = None
        self.protocol = None
        self.state = 'stopped'

    def handle_message(self, message):
        try:
            request = sylkrtc.SylkRTCRequest.from_message(message)
        except sylkrtc.ProtocolError as e:
            self.log.error(str(e))
        except Exception as e:
            self.log.error('{request_type}: {exception!s}'.format(request_type=message['sylkrtc'], exception=e))
            if 'transaction' in message:
                self._send_response(sylkrtc.ErrorResponse(transaction=message['transaction'], error=str(e)))
        else:
            operation = Operation(type='request', name=request.sylkrtc, data=request)
            self.operations_queue.send(operation)

    def handle_conference_invite(self, originator, room, invited_uris):
        for account_id in set(self.accounts_map).intersection(invited_uris):
            data = dict(sylkrtc='account_event', event='conference_invite', account=account_id, data=dict(originator=dict(uri=originator.id, display_name=originator.display_name), room=room.uri))
            room.log.info('invitation from %s for %s', originator.id, account_id)
            self.log.info('received an invitation from %s for %s to join video room %s', originator.id, account_id, room.uri)
            self._send_data(json.dumps(data))

    def notify(self, event):
        event.validate()
        self._send_data(json.dumps(event.to_struct()))

    # internal methods (not overriding / implementing the protocol API)

    def _send_response(self, response):
        response.validate()
        self._send_data(json.dumps(response.to_struct()))

    def _send_data(self, data):
        self.protocol.sendMessage(data)

    def _cleanup_session(self, session):
        # should only be called from a green thread.

        if self.janus_session_id is None:  # The connection was closed, there is noting to do
            return

        if session in self.sip_sessions:
            self.sip_sessions.remove(session)
            if session.direction == 'outgoing':
                # Destroy plugin handle for outgoing sessions. For incoming ones it's the same as the account handle, so don't
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)

    def _cleanup_videoroom_session(self, session):
        # should only be called from a green thread.

        if self.janus_session_id is None:  # The connection was closed, there is noting to do
            return

        if session in self.videoroom_sessions:
            self.videoroom_sessions.remove(session)
            if session.type == 'publisher':
                session.room.discard(session)
                session.feeds.clear()
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)
                self._maybe_destroy_videoroom(session.room)
            else:
                session.parent_session.feeds.discard(session.publisher_id)
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)

    def _maybe_destroy_videoroom(self, videoroom):
        # should only be called from a green thread.

        if self.protocol is None or self.janus_session_id is None:  # The connection was closed, there is nothing to do
            return

        if videoroom in self.protocol.factory.videorooms and not videoroom:
            self.protocol.factory.videorooms.remove(videoroom)

            # create a handle to do the cleanup
            handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
            self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)
            data = dict(request='destroy', room=videoroom.id)
            try:
                block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
            except JanusError:
                pass
            block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            self.protocol.backend.janus_set_event_handler(handle_id, None)

            videoroom.log.info('destroyed')

    def _lookup_sip_proxy(self, uri):
        # The proxy dance: Sofia-SIP seems to do a DNS lookup per SIP message when a domain is passed
        # as the proxy, so do the resolution ourselves and give it pre-resolver proxy URL. Since we use
        # caching to avoid long delays, we randomize the results matching the highest priority route's
        # transport.

        proxy = GeneralConfig.outbound_sip_proxy
        if proxy is not None:
            sip_uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
        else:
            sip_uri = SIPURI.parse('sip:%s' % uri)
        settings = SIPSimpleSettings()
        try:
            routes = self.resolver.lookup_sip_proxy(sip_uri, settings.sip.transport_list).wait()
        except DNSLookupError as e:
            raise DNSLookupError('DNS lookup error: {exception!s}'.format(exception=e))
        if not routes:
            raise DNSLookupError('DNS lookup error: no results found')

        route = random.choice([r for r in routes if r.transport == routes[0].transport])

        self.log.debug('DNS lookup for SIP proxy for {} yielded {}'.format(uri, route))

        # Build a proxy URI Sofia-SIP likes
        return 'sips:{route.address}:{route.port}'.format(route=route) if route.transport == 'tls' else str(route.uri)

    def _handle_janus_event_sip(self, handle_id, event_type, event):
        # TODO: use a model
        self.log.debug('janus SIP event: type={event_type} handle_id={handle_id} event={event}'.format(event_type=event_type, handle_id=handle_id, event=event))
        operation = Operation(type='event', name='janus-event-sip', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(operation)

    def _handle_janus_event_videoroom(self, handle_id, event_type, event):
        # TODO: use a model
        self.log.debug('janus video room event: type={event_type} handle_id={handle_id} event={event}'.format(event_type=event_type, handle_id=handle_id, event=event))
        operation = Operation(type='event', name='janus-event-videoroom', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(operation)

    def _operations_handler(self):
        self.ready_event.wait()
        while True:
            operation = self.operations_queue.wait()
            handler = getattr(self, '_OH_' + operation.type)
            handler(operation)
            del operation, handler

    def _OH_request(self, operation):
        handler = getattr(self, '_RH_' + operation.name.normalized)
        request = operation.data
        try:
            handler(request)
        except (APIError, DNSLookupError, JanusError) as e:
            self.log.error('{operation.name}: {exception!s}'.format(operation=operation, exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        except Exception as e:
            self.log.exception('{operation.type} {operation.name}: {exception!s}'.format(operation=operation, exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error='Internal error'))
        else:
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_event(self, operation):
        handler = getattr(self, '_EH_' + operation.name.normalized)
        try:
            handler(operation.data)
        except Exception as e:
            self.log.exception('{operation.type} {operation.name}: {exception!s}'.format(operation=operation, exception=e))

    # Request handlers

    def _RH_account_add(self, request):
        if request.account in self.accounts_map:
            raise APIError('Account {request.account} already added'.format(request=request))

        # check if domain is acceptable
        domain = request.account.partition('@')[2]
        if not {'*', domain}.intersection(GeneralConfig.sip_domains):
            raise APIError('SIP domain not allowed: %s' % domain)

        # Create and store our mapping
        account_info = AccountInfo(request.account, request.password, request.display_name, request.user_agent)
        self.accounts_map[account_info.id] = account_info
        self.log.info('added account {request.account} using {request.user_agent}'.format(request=request))

    def _RH_account_remove(self, request):
        try:
            account_info = self.accounts_map.pop(request.account)
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        # cleanup in case the client didn't unregister before removing the account
        handle_id = account_info.janus_handle_id
        if handle_id is not None:
            block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            self.protocol.backend.janus_set_event_handler(handle_id, None)
            self.account_handles_map.pop(handle_id)
        self.log.info('removed account {request.account}'.format(request=request))

    def _RH_account_register(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        proxy = self._lookup_sip_proxy(request.account)

        handle_id = account_info.janus_handle_id
        if handle_id is not None:
            # Destroy the existing plugin handle
            block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            self.protocol.backend.janus_set_event_handler(handle_id, None)
            self.account_handles_map.pop(handle_id)
            account_info.janus_handle_id = None

        # Create a plugin handle
        handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.sip'))
        self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_sip)
        account_info.janus_handle_id = handle_id
        self.account_handles_map[handle_id] = account_info

        data = dict(request='register', proxy=proxy, **account_info.user_data)
        block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))

    def _RH_account_unregister(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        handle_id = account_info.janus_handle_id
        if handle_id is not None:
            block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            self.protocol.backend.janus_set_event_handler(handle_id, None)
            account_info.janus_handle_id = None
            self.account_handles_map.pop(handle_id)
        self.log.info('unregistered {request.account} from receiving incoming calls'.format(request=request))

    def _RH_account_devicetoken(self, request):
        if request.account not in self.accounts_map:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))
        storage = TokenStorage()
        if request.old_token is not None:
            storage.remove(request.account, request.old_token)
            self.log.debug('removed token {request.old_token} for {request.account}'.format(request=request))
        if request.new_token is not None:
            storage.add(request.account, request.new_token)
            self.log.debug('added token {request.new_token} for {request.account}'.format(request=request))

    def _RH_session_create(self, request):
        if request.session in self.sip_sessions:
            raise APIError('Session ID {request.session} already in use'.format(request=request))

        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        proxy = self._lookup_sip_proxy(request.uri)

        # Create a new plugin handle and 'register' it, without actually doing so
        handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.sip'))
        self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_sip)

        try:
            data = dict(request='register', send_register=False, proxy=proxy, **account_info.user_data)
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))

            data = dict(request='call', uri='sip:%s' % SIP_PREFIX_RE.sub('', request.uri), srtp='sdes_optional')
            jsep = dict(type='offer', sdp=request.sdp)
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data, jsep))
        except:
            try:
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            except JanusError:
                pass
            self.protocol.backend.janus_set_event_handler(handle_id, None)
            raise

        session_info = SIPSessionInfo(request.session)
        session_info.janus_handle_id = handle_id
        session_info.init_outgoing(request.account, request.uri)
        self.sip_sessions.add(session_info)

        self.log.info('created outgoing session {request.session} from {request.account} to {request.uri}'.format(request=request))

    def _RH_session_answer(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session specified: {request.session}'.format(request=request))

        if session_info.direction != 'incoming':
            raise APIError('Cannot answer outgoing session {request.session}'.format(request=request))
        if session_info.state != 'connecting':
            raise APIError('Invalid state for answering session {session.id}: {session.state}'.format(session=session_info))

        data = dict(request='accept')
        jsep = dict(type='answer', sdp=request.sdp)
        block_on(self.protocol.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data, jsep))
        self.log.info('answered incoming session {session.id}'.format(session=session_info))

    def _RH_session_trickle(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session specified: {request.session}'.format(request=request))

        if session_info.state == 'terminated':
            raise APIError('Session {request.session} is terminated'.format(request=request))

        candidates = [c.to_struct() for c in request.candidates]

        block_on(self.protocol.backend.janus_trickle(self.janus_session_id, session_info.janus_handle_id, candidates))

        if not candidates:
            self.log.debug('session {session.id} negotiated ICE'.format(session=session_info))

    def _RH_session_terminate(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session specified: {request.session}'.format(request=request))

        if session_info.state not in ('connecting', 'progress', 'accepted', 'established'):
            raise APIError('Invalid state for terminating session {session.id}: {session.state}'.format(session=session_info))

        if session_info.direction == 'incoming' and session_info.state == 'connecting':
            data = dict(request='decline', code=486)
        else:
            data = dict(request='hangup')
        block_on(self.protocol.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data))
        self.log.info('requested termination for session {session.id}'.format(session=session_info))

    def _RH_videoroom_join(self, request):
        if request.session in self.videoroom_sessions:
            raise APIError('Session ID {request.session} already in use'.format(request=request))

        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        try:
            videoroom = self.protocol.factory.videorooms[request.uri]
        except KeyError:
            videoroom = VideoRoom(request.uri)
            self.protocol.factory.videorooms.add(videoroom)

        if not videoroom.allow_uri(request.account):
            self._maybe_destroy_videoroom(videoroom)
            raise APIError('{request.account} is not allowed to join room {request.uri}'.format(request=request))

        try:
            handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
            self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)

            try:
                # create the room if it doesn't exist
                config = videoroom.config
                data = dict(request='create', room=videoroom.id, publishers=10, bitrate=config.max_bitrate, videocodec=config.video_codec, record=config.record, rec_dir=config.recording_dir)
                try:
                    block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
                except JanusError as e:
                    if e.code != 427:  # 427 means room already exists
                        raise

                # join the room
                data = dict(request='joinandconfigure', room=videoroom.id, ptype='publisher', audio=True, video=True)
                if account_info.display_name:
                    data.update(display=account_info.display_name)
                jsep = dict(type='offer', sdp=request.sdp)
                block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data, jsep))
            except:
                try:
                    block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
                except JanusError:
                    pass
                self.protocol.backend.janus_set_event_handler(handle_id, None)
                raise
        except:
            self._maybe_destroy_videoroom(videoroom)
            raise

        videoroom_session = VideoRoomSessionInfo(request.session, owner=self)
        videoroom_session.janus_handle_id = handle_id
        videoroom_session.initialize(request.account, 'publisher', videoroom)
        self.videoroom_sessions.add(videoroom_session)

        data = dict(sylkrtc='videoroom_event', session=videoroom_session.id, event='state', data=dict(state='progress'))
        self._send_data(json.dumps(data))

        self.log.info('created session {request.session} from account {request.account} to video room {request.uri}'.format(request=request))

    def _RH_videoroom_ctl(self, request):
        option_name = request.option.replace('-', '_')
        if getattr(request, option_name) is None:
            raise APIError('missing {!r} field in request'.format(option_name))
        sub_handler = getattr(self, '_RH_videoroom_ctl_{}'.format(option_name))
        sub_handler(request)

    def _RH_videoroom_ctl_configure_room(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('configure-room: unknown video room session: {request.session}'.format(request=request))
        videoroom = videoroom_session.room
        # todo: should we send out events if the active participant list did not change?
        try:
            videoroom.active_participants = request.configure_room.active_participants
        except ValueError as e:
            raise APIError('configure-room: {exception!s}'.format(exception=e))
        for session in videoroom:
            session.owner.notify(sylkrtc.VideoRoomConfigurationEvent(session=session.id, active_participants=videoroom.active_participants, originator=request.session))

    def _RH_videoroom_ctl_feed_attach(self, request):
        if request.feed_attach.session in self.videoroom_sessions:
            raise APIError('feed-attach: video room session ID {request.feed_attach.session} already in use'.format(request=request))

        # get the 'base' session, the one used to join and publish
        try:
            base_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('feed-attach: unknown video room session: {request.session}'.format(request=request))

        # get the publisher's session
        try:
            publisher_session = base_session.room[request.feed_attach.publisher]
        except KeyError:
            raise APIError('feed-attach: unknown publisher video room session to attach to: {request.feed_attach.publisher}'.format(request=request))
        if publisher_session.publisher_id is None:
            raise APIError('feed-attach: video room session {session.id} does not have a publisher ID'.format(session=publisher_session))

        handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
        self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)

        # join the room as a listener
        try:
            data = dict(request='join', room=base_session.room.id, ptype='listener', feed=publisher_session.publisher_id)
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
        except:
            try:
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            except JanusError:
                pass
            self.protocol.backend.janus_set_event_handler(handle_id, None)
            raise

        videoroom_session = VideoRoomSessionInfo(request.feed_attach.session, owner=self)
        videoroom_session.janus_handle_id = handle_id
        videoroom_session.parent_session = base_session
        videoroom_session.publisher_id = publisher_session.id
        videoroom_session.initialize(base_session.account_id, 'subscriber', base_session.room)
        self.videoroom_sessions.add(videoroom_session)
        base_session.feeds.add(publisher_session)

    def _RH_videoroom_ctl_feed_answer(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.feed_answer.session]
        except KeyError:
            raise APIError('feed-answer: unknown video room session: {request.feed_answer.session}'.format(request=request))
        data = dict(request='start', room=videoroom_session.room.id)
        jsep = dict(type='answer', sdp=request.feed_answer.sdp)
        block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data, jsep))

    def _RH_videoroom_ctl_feed_detach(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.feed_detach.session]
        except KeyError:
            raise APIError('feed-detach: unknown video room session to detach: {request.feed_detach.session}'.format(request=request))
        if videoroom_session.parent_session.id != request.session:
            raise APIError('feed-detach: {request.feed_detach.session} is not an attached feed of {request.session}'.format(request=request))
        data = dict(request='leave')
        block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
        self._cleanup_videoroom_session(videoroom_session)

    def _RH_videoroom_ctl_invite_participants(self, request):
        try:
            base_session = self.videoroom_sessions[request.session]
            account_info = self.accounts_map[base_session.account_id]
        except KeyError:
            raise APIError('invite-participants: unknown video room session: {request.session}'.format(request=request))
        for protocol in self.protocol.factory.connections.difference([self.protocol]):
            protocol.connection_handler.handle_conference_invite(account_info, base_session.room, request.invite_participants.participants)

    def _RH_videoroom_ctl_trickle(self, request):
        session = request.trickle.session or request.session
        try:
            videoroom_session = self.videoroom_sessions[session]
        except KeyError:
            raise APIError('trickle: unknown video room session: {session}'.format(session=session))
        candidates = [c.to_struct() for c in request.trickle.candidates]
        block_on(self.protocol.backend.janus_trickle(self.janus_session_id, videoroom_session.janus_handle_id, candidates))
        if not candidates and videoroom_session.type == 'publisher':
            self.log.debug('video room session {session.id} negotiated ICE'.format(session=videoroom_session))

    def _RH_videoroom_ctl_update(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('update: unknown video room session: {request.session}'.format(request=request))
        update_data = request.update.to_struct()
        if update_data:
            data = dict(request='configure', room=videoroom_session.room.id, **update_data)
            block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
            modified = ', '.join('{}={}'.format(key, update_data[key]) for key in update_data)
            self.log.info('updated video room session {request.session} with {modified}'.format(request=request, modified=modified))

    def _RH_videoroom_terminate(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))

        data = dict(request='leave')
        block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))

        data = dict(sylkrtc='videoroom_event', session=videoroom_session.id, event='state', data=dict(state='terminated'))
        self._send_data(json.dumps(data))

        # safety net in case we do not get any answer for the leave request
        # todo: to be adjusted later after pseudo-synchronous communication with janus is implemented
        reactor.callLater(2, call_in_green_thread, self._cleanup_videoroom_session, videoroom_session)

        self.log.info('requested termination for video room session {request.session}'.format(request=request))

    # Event handlers

    def _EH_janus_event_sip(self, data):
        handle_id = data['handle_id']
        event_type = data['event_type']
        event = data['event']

        if event_type == 'event':
            self._janus_event_plugin_sip(data)
        elif event_type == 'webrtcup':
            try:
                session_info = self.sip_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find session for handle ID %s' % handle_id)
                return
            session_info.state = 'established'
            data = dict(sylkrtc='session_event', session=session_info.id, event='state', data=dict(state=session_info.state))
            self._send_data(json.dumps(data))  # TODO: SessionEvent model
            self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
            self.log.info('established WEBRTC connection for session {session.id}'.format(session=session_info))
        elif event_type == 'hangup':
            try:
                session_info = self.sip_sessions[handle_id]
            except KeyError:
                return
            if session_info.state != 'terminated':
                session_info.state = 'terminated'
                self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
                reason = event.get('reason', 'reason unspecified')
                data = dict(sylkrtc='session_event', session=session_info.id, event='state', data=dict(state=session_info.state, reason=reason))
                # TODO: SessionEvent model
                self._send_data(json.dumps(data))
                self.log.info('{session.direction} session {session.id} terminated ({reason})'.format(session=session_info, reason=reason))
                self._cleanup_session(session_info)
        elif event_type in ('media', 'detached'):
            # ignore
            pass
        elif event_type == 'slowlink':
            try:
                session_info = self.sip_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find session for handle ID %s' % handle_id)
                return
            try:
                uplink = data['event']['uplink']
            except KeyError:
                self.log.warning('could not find uplink in slowlink event data')
                return
            if uplink:  # uplink is from janus' point of view
                if not session_info.slow_download:
                    self.log.info('poor download connectivity for session {session.id}'.format(session=session_info))
                session_info.slow_download = True
            else:
                if not session_info.slow_upload:
                    self.log.info('poor upload connectivity for session {session.id}'.format(session=session_info))
                session_info.slow_upload = True
        else:
            self.log.warning('received unexpected event type: %s' % event_type)

    def _janus_event_plugin_sip(self, data):
        handle_id = data['handle_id']
        event = data['event']
        plugin_data = event['plugindata']
        assert plugin_data['plugin'] == 'janus.plugin.sip'
        event_data = plugin_data['data']
        assert event_data.get('sip') == 'event'

        if 'result' not in event_data:
            self.log.warning('unexpected event: %s' % event)
            return

        event_data = event_data['result']
        jsep = event.get('jsep', None)
        event_type = event_data['event']
        if event_type in ('registering', 'registered', 'registration_failed', 'incomingcall'):
            # skip 'registered' events from session handles
            if event_type == 'registered' and event_data['register_sent'] in (False, 'false'):
                return
            # account event
            try:
                account_info = self.account_handles_map[handle_id]
            except KeyError:
                self.log.warning('could not find account for handle ID %s' % handle_id)
                return
            if event_type == 'incomingcall':
                originator_uri = SIP_PREFIX_RE.sub('', event_data['username'])
                originator_display_name = event_data.get('displayname', '').replace('"', '')
                jsep = event.get('jsep', None)
                assert jsep is not None
                session_id = uuid.uuid4().hex
                session = SIPSessionInfo(session_id)
                session.janus_handle_id = handle_id
                session.init_incoming(account_info.id, originator_uri, originator_display_name)
                self.sip_sessions.add(session)
                data = dict(sylkrtc='account_event', account=account_info.id, session=session_id, event='incoming_session', data=dict(originator=session.remote_identity.__dict__, sdp=jsep['sdp']))
                self.log.info('received incoming session {session.id} from {session.remote_identity.uri!s} to {session.local_identity.uri!s}'.format(session=session))
            else:
                registration_state = event_type
                if registration_state == 'registration_failed':
                    registration_state = 'failed'
                if account_info.registration_state == registration_state:
                    return
                account_info.registration_state = registration_state
                registration_data = dict(state=registration_state)
                if registration_state == 'failed':
                    registration_data['reason'] = reason = '{0[code]} {0[reason]}'.format(event_data)
                    self.log.info('registration for {account.id} failed: {reason}'.format(account=account_info, reason=reason))
                elif registration_state == 'registered':
                    self.log.info('registered {account.id} to receive incoming calls'.format(account=account_info))
                data = dict(sylkrtc='account_event', account=account_info.id, event='registration_state', data=registration_data)
            # TODO: AccountEvent model
            self._send_data(json.dumps(data))
        elif event_type in ('calling', 'accepted'):
            # session event
            try:
                session_info = self.sip_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find session for handle ID %s' % handle_id)
                return
            state_map = dict(calling='progress', accepted='accepted')
            session_info.state = state_map[event_type]
            self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
            data = dict(sylkrtc='session_event', session=session_info.id, event='state', data=dict(state=session_info.state))
            if session_info.state == 'accepted' and session_info.direction == 'outgoing':
                assert jsep is not None
                data['data']['sdp'] = jsep['sdp']
            self._send_data(json.dumps(data))  # TODO: SessionEvent model
        elif event_type == 'hangup':  # session hangup event
            try:
                session_info = self.sip_sessions[handle_id]
            except KeyError:
                return
            if session_info.state != 'terminated':
                session_info.state = 'terminated'
                self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
                code = event_data.get('code', 0)
                reason = '%d %s' % (code, event_data.get('reason', 'Unknown'))
                data = dict(sylkrtc='session_event', session=session_info.id, event='state', data=dict(state=session_info.state, reason=reason))
                self._send_data(json.dumps(data))  # TODO: SessionEvent model
                if session_info.direction == 'incoming' and code == 487:  # check if missed incoming call
                    data = dict(sylkrtc='account_event', account=session_info.account_id, event='missed_session', data=dict(originator=session_info.remote_identity.__dict__))
                    self._send_data(json.dumps(data))  # TODO: AccountEvent model
                if code >= 300:
                    self.log.info('{session.direction} session {session.id} terminated ({reason})'.format(session=session_info, reason=reason))
                else:
                    self.log.info('{session.direction} session {session.id} terminated'.format(session=session_info))
                self._cleanup_session(session_info)
        elif event_type == 'missed_call':
            try:
                account_info = self.account_handles_map[handle_id]
            except KeyError:
                self.log.warning('could not find account for handle ID %s' % handle_id)
                return
            originator_uri = SIP_PREFIX_RE.sub('', event_data['caller'])
            originator_display_name = event_data.get('displayname', '').replace('"', '')
            # We have no session, so create an identity object by hand
            originator = SessionPartyIdentity(originator_uri, originator_display_name)
            data = dict(sylkrtc='account_event', account=account_info.id, event='missed_session', data=dict(originator=originator.__dict__))
            self.log.info('missed incoming call from {originator.uri}'.format(originator=originator))
            # TODO: AccountEvent model
            self._send_data(json.dumps(data))
        elif event_type in ('ack', 'declining', 'hangingup', 'proceeding'):
            pass  # ignore
        else:
            self.log.warning('unexpected SIP plugin event type: %s' % event_type)

    def _EH_janus_event_videoroom(self, data):
        handle_id = data['handle_id']
        event_type = data['event_type']

        if event_type == 'event':
            self._janus_event_plugin_videoroom(data)
        elif event_type == 'webrtcup':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find video room session for handle ID %s during webrtcup event' % handle_id)
                return
            if videoroom_session.type == 'publisher':
                self.log.info('established WEBRTC connection for session {session.id}'.format(session=videoroom_session))
                data = dict(sylkrtc='videoroom_event', session=videoroom_session.id, event='state', data=dict(state='established'))
            else:
                data = dict(sylkrtc='videoroom_event', session=videoroom_session.parent_session.id, event='feed_established', data=dict(state='established', subscription=videoroom_session.id))
            self._send_data(json.dumps(data))
        elif event_type == 'hangup':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                return
            self._cleanup_videoroom_session(videoroom_session)
        elif event_type in ('media', 'detached'):
            pass
        elif event_type == 'slowlink':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find video room session for handle ID %s during slowlink event' % handle_id)
                return
            try:
                uplink = data['event']['uplink']
            except KeyError:
                self.log.error('could not find uplink in slowlink event data')
                return
            if uplink:  # uplink is from janus' point of view
                if not videoroom_session.slow_download:
                    self.log.info('poor download connectivity to video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
                videoroom_session.slow_download = True
            else:
                if not videoroom_session.slow_upload:
                    self.log.info('poor upload connectivity to video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
                videoroom_session.slow_upload = True
        else:
            self.log.warning('received unexpected event type %s: data=%s' % (event_type, data))

    def _janus_event_plugin_videoroom(self, data):
        handle_id = data['handle_id']
        event = data['event']
        plugin_data = event['plugindata']
        assert(plugin_data['plugin'] == 'janus.plugin.videoroom')
        event_data = event['plugindata']['data']
        assert 'videoroom' in event_data

        event_type = event_data['videoroom']
        if event_type == 'joined':
            # a join request succeeded, this is a publisher
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find video room session for handle ID %s during joined event' % handle_id)
                return
            self.log.info('joined video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
            videoroom_session.publisher_id = event_data['id']
            room = videoroom_session.room
            jsep = event.get('jsep', None)
            assert jsep is not None
            data = dict(sylkrtc='videoroom_event', session=videoroom_session.id, event='state', data=dict(state='accepted', sdp=jsep['sdp']))
            self._send_data(json.dumps(data))
            # send information about existing publishers
            publishers = []
            for publisher in event_data['publishers']:
                publisher_id = publisher['id']
                try:
                    publisher_session = room[publisher_id]
                except KeyError:
                    self.log.warning('could not find matching session for publisher %s during joined event' % publisher_id)
                    continue
                publishers.append(dict(id=publisher_session.id, uri=publisher_session.account_id, display_name=publisher.get('display', '')))
            data = dict(sylkrtc='videoroom_event', session=videoroom_session.id, event='initial_publishers', data=dict(publishers=publishers))
            self._send_data(json.dumps(data))
            room.add(videoroom_session)  # adding the session to the room might also trigger sending an event with the active participants which must be sent last
        elif event_type == 'event':
            if 'publishers' in event_data:
                try:
                    videoroom_session = self.videoroom_sessions[handle_id]
                except KeyError:
                    self.log.warning('could not find video room session for handle ID %s during publishers event' % handle_id)
                    return
                room = videoroom_session.room
                # send information about new publishers
                publishers = []
                for publisher in event_data['publishers']:
                    publisher_id = publisher['id']
                    try:
                        publisher_session = room[publisher_id]
                    except KeyError:
                        self.log.warning('could not find matching session for publisher %s during publishers event' % publisher_id)
                        continue
                    publishers.append(dict(id=publisher_session.id, uri=publisher_session.account_id, display_name=publisher.get('display', '')))
                data = dict(sylkrtc='videoroom_event', session=videoroom_session.id, event='publishers_joined', data=dict(publishers=publishers))
                self._send_data(json.dumps(data))
            elif 'leaving' in event_data:
                janus_publisher_id = event_data['leaving']  # janus_publisher_id == 'ok' when the event is about ourselves leaving the room
                try:
                    base_session = self.videoroom_sessions[handle_id]
                except KeyError:
                    if janus_publisher_id != 'ok':
                        self.log.warning('could not find video room session for handle ID %s during leaving event' % handle_id)
                    return
                if janus_publisher_id == 'ok':
                    self.log.info('left video room {session.room.uri} with session {session.id}'.format(session=base_session))
                    self._cleanup_videoroom_session(base_session)
                    return
                try:
                    publisher_session = base_session.feeds.pop(janus_publisher_id)
                except KeyError:
                    return
                data = dict(sylkrtc='videoroom_event', session=base_session.id, event='publishers_left', data=dict(publishers=[publisher_session.id]))
                self._send_data(json.dumps(data))
            elif {'started', 'unpublished', 'left', 'configured'}.intersection(event_data):
                pass
            else:
                self.log.warning('received unexpected video room plugin event: type={} data={}'.format(event_type, event_data))
        elif event_type == 'attached':
            # sent when a feed is subscribed for a given publisher
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find video room session for handle ID %s during attached event' % handle_id)
                return
            # get the session which originated the subscription
            base_session = videoroom_session.parent_session
            jsep = event.get('jsep', None)
            assert base_session is not None
            assert jsep is not None
            assert jsep['type'] == 'offer'
            data = dict(sylkrtc='videoroom_event', session=base_session.id, event='feed_attached', data=dict(sdp=jsep['sdp'], subscription=videoroom_session.id))
            self._send_data(json.dumps(data))
        elif event_type == 'slow_link':
            pass
        else:
            self.log.warning('received unexpected video room plugin event: type={} data={}'.format(event_type, event_data))
