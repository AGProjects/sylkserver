
import hashlib
import json
import random
import os
import time
import uuid

from application.python import limit
from application.python.weakref import defaultweakobjectmap
from application.system import makedirs, unlink
from eventlib import coros, proc
from itertools import count
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import call_in_green_thread, run_in_green_thread
from shutil import copyfileobj, rmtree
from string import maketrans
from twisted.internet import reactor
from typing import Generic, Container, Iterable, Sized, TypeVar, Dict, Set, Optional, Union
from werkzeug.exceptions import InternalServerError

from . import push
from .configuration import GeneralConfig, get_room_config
from .janus import JanusBackend, JanusError, JanusSession, SIPPluginHandle, VideoroomPluginHandle
from .logger import ConnectionLogger, VideoroomLogger
from .models import sylkrtc, janus
from .storage import TokenStorage


class AccountInfo(object):
    # noinspection PyShadowingBuiltins
    def __init__(self, id, password, display_name=None, user_agent=None):
        self.id = id
        self.password = password
        self.display_name = display_name
        self.user_agent = user_agent
        self.registration_state = None
        self.janus_handle = None  # type: Optional[SIPPluginHandle]

    @property
    def uri(self):
        return 'sip:' + self.id

    @property
    def user_data(self):
        return dict(username=self.uri, display_name=self.display_name, user_agent=self.user_agent, ha1_secret=self.password)


class SessionPartyIdentity(object):
    def __init__(self, uri, display_name=None):
        self.uri = uri
        self.display_name = display_name


# todo: might need to replace this auto-resetting descriptor with a timer in case we need to know when the slow link state expired

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

    # noinspection PyShadowingBuiltins
    def __init__(self, id):
        self.id = id
        self.direction = None
        self.state = None
        self.account = None            # type: AccountInfo
        self.local_identity = None     # type: SessionPartyIdentity
        self.remote_identity = None    # type: SessionPartyIdentity
        self.janus_handle = None       # type: SIPPluginHandle
        self.slow_download = False
        self.slow_upload = False

    def init_outgoing(self, account, destination):
        self.account = account
        self.direction = 'outgoing'
        self.state = 'connecting'
        self.local_identity = SessionPartyIdentity(account.id)
        self.remote_identity = SessionPartyIdentity(destination)

    def init_incoming(self, account, originator, originator_display_name=''):
        self.account = account
        self.direction = 'incoming'
        self.state = 'connecting'
        self.local_identity = SessionPartyIdentity(account.id)
        self.remote_identity = SessionPartyIdentity(originator, originator_display_name)


class VideoroomSessionInfo(object):
    slow_download = SlowLinkDescriptor()
    slow_upload = SlowLinkDescriptor()

    # noinspection PyShadowingBuiltins
    def __init__(self, id, owner, janus_handle):
        self.type = None                  # publisher / subscriber
        self.id = id
        self.owner = owner                # type: ConnectionHandler
        self.janus_handle = janus_handle  # type: VideoroomPluginHandle
        self.account = None               # type: AccountInfo
        self.room = None                  # type: Videoroom
        self.bitrate = None
        self.parent_session = None        # type: VideoroomSessionInfo  # for subscribers this is their main session (the one used to join), for publishers is None
        self.publisher_id = None          # janus publisher ID for publishers / publisher session ID for subscribers
        self.slow_download = False
        self.slow_upload = False
        self.feeds = PublisherFeedContainer()  # keeps references to all the other participant's publisher feeds that we subscribed to

    def init_publisher(self, account, room):
        self.type = 'publisher'
        self.account = account
        self.room = room
        self.bitrate = room.config.max_bitrate

    def init_subscriber(self, publisher_session, parent_session):
        assert publisher_session.type == parent_session.type == 'publisher'
        self.type = 'subscriber'
        self.publisher_id = publisher_session.id
        self.parent_session = parent_session
        self.account = parent_session.account
        self.room = parent_session.room
        self.bitrate = self.room.config.max_bitrate

    def __repr__(self):
        return '<{0.__class__.__name__}: type={0.type!r} id={0.id!r} janus_handle={0.janus_handle!r}>'.format(self)


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


class Videoroom(object):
    def __init__(self, uri):
        self.id = random.getrandbits(32)    # janus needs numeric room names
        self.uri = uri
        self.config = get_room_config(uri)
        self.log = VideoroomLogger(self)
        self._active_participants = []
        self._sessions = set()  # type: Set[VideoroomSessionInfo]
        self._id_map = {}       # type: Dict[Union[str, int], VideoroomSessionInfo]  # map session.id -> session and session.publisher_id -> session
        self._shared_files = []
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
        self.log.info('{session.account.id} has joined the room'.format(session=session))
        self._update_bitrate()
        if self._active_participants:
            session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
        if self._shared_files:
            session.owner.send(sylkrtc.VideoroomFileSharingEvent(session=session.id, files=self._shared_files))

    def discard(self, session):
        if session in self._sessions:
            self._sessions.discard(session)
            self._id_map.pop(session.id, None)
            self._id_map.pop(session.publisher_id, None)
            self.log.info('{session.account.id} has left the room'.format(session=session))
            if session.id in self._active_participants:
                self._active_participants.remove(session.id)
                self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
                for session in self._sessions:
                    session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
            self._update_bitrate()

    def remove(self, session):
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.publisher_id)
        self.log.info('{session.account.id} has left the room'.format(session=session))
        if session.id in self._active_participants:
            self._active_participants.remove(session.id)
            self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
            for session in self._sessions:
                session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
        self._update_bitrate()

    def clear(self):
        for session in self._sessions:
            self.log.info('{session.account.id} has left the room'.format(session=session))
        self._active_participants = []
        self._shared_files = []
        self._sessions.clear()
        self._id_map.clear()

    def allow_uri(self, uri):
        config = self.config
        if config.access_policy == 'allow,deny':
            return config.allow.match(uri) and not config.deny.match(uri)
        else:
            return not config.deny.match(uri) or config.allow.match(uri)

    def add_file(self, upload_request):
        self._write_file(upload_request)

    def get_file(self, filename):
        path = os.path.join(self.config.filesharing_dir, filename)
        if os.path.exists(path):
            return path
        else:
            raise LookupError('file does not exist')

    def _fix_path(self, path):
        name, extension = os.path.splitext(path)
        for x in count(0, step=-1):
            path = '{}{}{}'.format(name, x or '', extension)
            if not os.path.exists(path) and not os.path.islink(path):
                return path

    @run_in_thread('file-io')
    def _write_file(self, upload_request):
        makedirs(self.config.filesharing_dir)
        path = self._fix_path(os.path.join(self.config.filesharing_dir, upload_request.shared_file.filename))
        upload_request.shared_file.filename = os.path.basename(path)
        try:
            with open(path, 'wb') as output_file:
                copyfileobj(upload_request.content, output_file)
        except (OSError, IOError):
            upload_request.had_error = True
            unlink(path)
        self._write_file_done(upload_request)

    @run_in_twisted_thread
    def _write_file_done(self, upload_request):
        if upload_request.had_error:
            upload_request.deferred.errback(InternalServerError('could not save file'))
        else:
            self._shared_files.append(upload_request.shared_file)
            for session in self._sessions:
                session.owner.send(sylkrtc.VideoroomFileSharingEvent(session=session.id, files=[upload_request.shared_file]))
            upload_request.deferred.callback('OK')

    def cleanup(self):
        self._remove_files()

    @run_in_thread('file-io')
    def _remove_files(self):
        rmtree(self.config.filesharing_dir, ignore_errors=True)

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
                        session.janus_handle.message(janus.VideoroomUpdatePublisher(bitrate=bitrate), async=True)
            else:
                bitrate = self.config.max_bitrate // limit(len(self._sessions) - 1, min=1)
                self.log.info('participant bitrate is {}'.format(bitrate))
                for session in self._sessions:
                    if session.bitrate != bitrate:
                        session.bitrate = bitrate
                        session.janus_handle.message(janus.VideoroomUpdatePublisher(bitrate=bitrate), async=True)

    # todo: make Videoroom be a context manager that is retained/released on enter/exit and implement __nonzero__ to be different from __len__
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


SessionT = TypeVar('SessionT', SIPSessionInfo, VideoroomSessionInfo)


class SessionContainer(Sized, Iterable[SessionT], Container[SessionT], Generic[SessionT]):
    def __init__(self):
        self._sessions = set()
        self._id_map = {}  # map session.id -> session and session.janus_handle.id -> session

    def add(self, session):
        assert session not in self._sessions
        assert session.id not in self._id_map and session.janus_handle.id not in self._id_map
        self._sessions.add(session)
        self._id_map[session.id] = self._id_map[session.janus_handle.id] = session

    def discard(self, item):  # item can be any of session, session.id or session.janus_handle.id
        session = self._id_map[item] if item in self._id_map else item if item in self._sessions else None
        if session is not None:
            self._sessions.discard(session)
            self._id_map.pop(session.id, None)
            self._id_map.pop(session.janus_handle.id, None)

    def remove(self, item):  # item can be any of session, session.id or session.janus_handle.id
        session = self._id_map[item] if item in self._id_map else item
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.janus_handle.id)

    def pop(self, item):  # item can be any of session, session.id or session.janus_handle.id
        session = self._id_map[item] if item in self._id_map else item
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.janus_handle.id)
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


class GreenEvent(object):
    def __init__(self):
        self._event = coros.event()

    def set(self):
        if self._event.ready():
            return
        self._event.send(True)

    def is_set(self):
        return self._event.ready()

    def clear(self):
        if self._event.ready():
            self._event.reset()

    def wait(self):
        return self._event.wait()


# noinspection PyPep8Naming
class ConnectionHandler(object):
    janus = JanusBackend()

    def __init__(self, protocol):
        self.protocol = protocol
        self.device_id = hashlib.md5(protocol.peer).digest().encode('base64').rstrip('=\n')
        self.janus_session = None  # type: JanusSession
        self.accounts_map = {}  # account ID -> account
        self.account_handles_map = {}  # Janus handle ID -> account
        self.sip_sessions = SessionContainer()        # type: SessionContainer[SIPSessionInfo]        # incoming and outgoing SIP sessions
        self.videoroom_sessions = SessionContainer()  # type: SessionContainer[VideoroomSessionInfo]  # publisher and subscriber sessions in video rooms
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
            self.janus_session = JanusSession()
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
                self.send(sylkrtc.ReadyEvent())

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
                if account_info.janus_handle is not None:
                    self.janus.set_event_handler(account_info.janus_handle.id, None)
            for session in self.sip_sessions:
                if session.janus_handle is not None:
                    self.janus.set_event_handler(session.janus_handle.id, None)
            for session in self.videoroom_sessions:
                if session.janus_handle is not None:
                    self.janus.set_event_handler(session.janus_handle.id, None)
                session.room.discard(session)
                session.feeds.clear()
            self.janus_session.destroy()  # this automatically detaches all plugin handles associated with it, no need to manually do it
        # cleanup
        self.ready_event.clear()
        self.accounts_map.clear()
        self.account_handles_map.clear()
        self.sip_sessions.clear()
        self.videoroom_sessions.clear()
        self.janus_session = None
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
                self.send(sylkrtc.ErrorResponse(transaction=message['transaction'], error=str(e)))
        else:
            operation = Operation(type='request', name=request.sylkrtc, data=request)
            self.operations_queue.send(operation)

    def send(self, message):
        if self.protocol is not None:
            self.protocol.sendMessage(json.dumps(message.__data__))

    # internal methods (not overriding / implementing the protocol API)

    def _cleanup_session(self, session):
        # should only be called from a green thread.

        if self.janus_session is None:  # The connection was closed, there is noting to do
            return

        if session in self.sip_sessions:
            self.sip_sessions.remove(session)
            if session.direction == 'outgoing':
                # Destroy plugin handle for outgoing sessions. For incoming ones it's the same as the account handle, so don't
                session.janus_handle.detach()

    def _cleanup_videoroom_session(self, session):
        # should only be called from a green thread.

        if self.janus_session is None:  # The connection was closed, there is noting to do
            return

        if session in self.videoroom_sessions:
            self.videoroom_sessions.remove(session)
            if session.type == 'publisher':
                session.room.discard(session)
                session.feeds.clear()
                session.janus_handle.detach()
                self._maybe_destroy_videoroom(session.room)
            else:
                session.parent_session.feeds.discard(session.publisher_id)
                session.janus_handle.detach()

    def _maybe_destroy_videoroom(self, videoroom):
        # should only be called from a green thread.

        if self.protocol is None or self.janus_session is None:  # The connection was closed, there is nothing to do
            return

        if videoroom in self.protocol.factory.videorooms and not videoroom:
            self.protocol.factory.videorooms.remove(videoroom)
            videoroom.cleanup()

            with VideoroomPluginHandle(self.janus_session, event_handler=self._handle_janus_videoroom_event) as videoroom_handle:
                videoroom_handle.destroy(room=videoroom.id)

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

    def _handle_janus_sip_event(self, event):
        operation = Operation(type='event', name='janus-sip', data=event)
        self.operations_queue.send(operation)

    def _handle_janus_videoroom_event(self, event):
        operation = Operation(type='event', name='janus-videoroom', data=event)
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
            self.send(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        except Exception as e:
            self.log.exception('{operation.type} {operation.name}: {exception!s}'.format(operation=operation, exception=e))
            self.send(sylkrtc.ErrorResponse(transaction=request.transaction, error='Internal error'))
        else:
            self.send(sylkrtc.AckResponse(transaction=request.transaction))

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
        if account_info.janus_handle is not None:
            account_info.janus_handle.detach()
            self.account_handles_map.pop(account_info.janus_handle.id)
        self.log.info('removed account {request.account}'.format(request=request))

    def _RH_account_register(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        proxy = self._lookup_sip_proxy(request.account)

        if account_info.janus_handle is not None:
            # Destroy the existing plugin handle
            account_info.janus_handle.detach()
            self.account_handles_map.pop(account_info.janus_handle.id)
            account_info.janus_handle = None

        # Create a plugin handle
        account_info.janus_handle = SIPPluginHandle(self.janus_session, event_handler=self._handle_janus_sip_event)
        self.account_handles_map[account_info.janus_handle.id] = account_info

        account_info.janus_handle.register(account_info, proxy=proxy)

    def _RH_account_unregister(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if account_info.janus_handle is not None:
            account_info.janus_handle.detach()
            self.account_handles_map.pop(account_info.janus_handle.id)
            account_info.janus_handle = None
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
        janus_handle = SIPPluginHandle(self.janus_session, event_handler=self._handle_janus_sip_event)

        try:
            janus_handle.call(account_info, uri=request.uri, sdp=request.sdp, proxy=proxy)
        except Exception:
            janus_handle.detach()
            raise

        session_info = SIPSessionInfo(request.session)
        session_info.janus_handle = janus_handle
        session_info.init_outgoing(account_info, request.uri)
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

        session_info.janus_handle.accept(sdp=request.sdp)
        self.log.info('answered incoming session {session.id}'.format(session=session_info))

    def _RH_session_trickle(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session specified: {request.session}'.format(request=request))

        if session_info.state == 'terminated':
            raise APIError('Session {request.session} is terminated'.format(request=request))

        session_info.janus_handle.trickle(request.candidates)

        if not request.candidates:
            self.log.debug('session {session.id} negotiated ICE'.format(session=session_info))

    def _RH_session_terminate(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session specified: {request.session}'.format(request=request))

        if session_info.state not in ('connecting', 'progress', 'accepted', 'established'):
            raise APIError('Invalid state for terminating session {session.id}: {session.state}'.format(session=session_info))

        if session_info.direction == 'incoming' and session_info.state == 'connecting':
            session_info.janus_handle.decline()
        else:
            session_info.janus_handle.hangup()
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
            videoroom = Videoroom(request.uri)
            self.protocol.factory.videorooms.add(videoroom)

        if not videoroom.allow_uri(request.account):
            self._maybe_destroy_videoroom(videoroom)
            raise APIError('{request.account} is not allowed to join room {request.uri}'.format(request=request))

        try:
            videoroom_handle = VideoroomPluginHandle(self.janus_session, event_handler=self._handle_janus_videoroom_event)

            try:
                try:
                    videoroom_handle.create(room=videoroom.id, config=videoroom.config, publishers=10)
                except JanusError as e:
                    if e.code != 427:  # 427 means room already exists
                        raise
                videoroom_handle.join(room=videoroom.id, sdp=request.sdp, display_name=account_info.display_name)
            except Exception:
                videoroom_handle.detach()
                raise
        except Exception:
            self._maybe_destroy_videoroom(videoroom)
            raise

        videoroom_session = VideoroomSessionInfo(request.session, owner=self, janus_handle=videoroom_handle)
        videoroom_session.init_publisher(account=account_info, room=videoroom)
        self.videoroom_sessions.add(videoroom_session)

        self.send(sylkrtc.VideoroomSessionProgressEvent(session=videoroom_session.id))

        self.log.info('created session {request.session} from account {request.account} to video room {request.uri}'.format(request=request))

    def _RH_videoroom_leave(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))

        videoroom_session.janus_handle.leave()

        self.send(sylkrtc.VideoroomSessionTerminatedEvent(session=videoroom_session.id))

        # safety net in case we do not get any answer for the leave request
        # todo: to be adjusted later after pseudo-synchronous communication with janus is implemented
        reactor.callLater(2, call_in_green_thread, self._cleanup_videoroom_session, videoroom_session)

        self.log.info('leaving video room session {request.session}'.format(request=request))

    def _RH_videoroom_configure(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))
        videoroom = videoroom_session.room
        # todo: should we send out events if the active participant list did not change?
        try:
            videoroom.active_participants = request.active_participants
        except ValueError as e:
            raise APIError(str(e))
        for session in videoroom:
            session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=videoroom.active_participants, originator=request.session))

    def _RH_videoroom_feed_attach(self, request):
        if request.feed in self.videoroom_sessions:
            raise APIError('Video room session ID {request.feed} already in use'.format(request=request))

        try:
            base_session = self.videoroom_sessions[request.session]  # our 'base' session (the one used to join and publish)
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))

        try:
            publisher_session = base_session.room[request.publisher]  # the publisher's session (the one we want to subscribe to)
        except KeyError:
            raise APIError('Unknown publisher video room session to attach to: {request.publisher}'.format(request=request))
        if publisher_session.publisher_id is None:
            raise APIError('Video room session {session.id} does not have a publisher ID'.format(session=publisher_session))

        videoroom_handle = VideoroomPluginHandle(self.janus_session, event_handler=self._handle_janus_videoroom_event)

        try:
            videoroom_handle.feed_attach(room=base_session.room.id, feed=publisher_session.publisher_id)
        except Exception:
            videoroom_handle.detach()
            raise

        videoroom_session = VideoroomSessionInfo(request.feed, owner=self, janus_handle=videoroom_handle)
        videoroom_session.init_subscriber(publisher_session, parent_session=base_session)
        self.videoroom_sessions.add(videoroom_session)
        base_session.feeds.add(publisher_session)

    def _RH_videoroom_feed_answer(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.feed]
        except KeyError:
            raise APIError('Unknown video room session: {request.feed}'.format(request=request))
        if videoroom_session.parent_session.id != request.session:
            raise APIError('{request.feed} is not an attached feed of {request.session}'.format(request=request))
        videoroom_session.janus_handle.feed_start(sdp=request.sdp)

    def _RH_videoroom_feed_detach(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.feed]
        except KeyError:
            raise APIError('Unknown video room session to detach: {request.feed}'.format(request=request))
        if videoroom_session.parent_session.id != request.session:
            raise APIError('{request.feed} is not an attached feed of {request.session}'.format(request=request))
        videoroom_session.janus_handle.feed_detach()
        # safety net in case we do not get any answer for the feed_detach request
        # todo: to be adjusted later after pseudo-synchronous communication with janus is implemented
        reactor.callLater(2, call_in_green_thread, self._cleanup_videoroom_session, videoroom_session)

    def _RH_videoroom_invite(self, request):
        try:
            base_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))
        room = base_session.room
        participants = set(request.participants)
        originator = sylkrtc.SIPIdentity(uri=base_session.account.id, display_name=base_session.account.display_name)
        event = sylkrtc.AccountConferenceInviteEvent(account='placeholder', room=room.uri, originator=originator)
        for protocol in self.protocol.factory.connections.difference([self.protocol]):
            connection_handler = protocol.connection_handler
            for account in participants.intersection(connection_handler.accounts_map):
                event.account = account
                connection_handler.send(event)
                room.log.info('invitation from %s for %s', originator.uri, account)
                connection_handler.log.info('received an invitation from %s for %s to join video room %s', originator.uri, account, room.uri)
        for participant in participants:
            push.conference_invite(originator=originator.uri, destination=participant, room=room.uri)

    def _RH_videoroom_session_trickle(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))
        videoroom_session.janus_handle.trickle(request.candidates)
        if not request.candidates and videoroom_session.type == 'publisher':
            self.log.debug('video room session {session.id} negotiated ICE'.format(session=videoroom_session))

    def _RH_videoroom_session_update(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown video room session: {request.session}'.format(request=request))
        options = request.options.__data__
        if options:
            videoroom_session.janus_handle.update_publisher(options)
            modified = ', '.join('{}={}'.format(key, options[key]) for key in options)
            self.log.info('updated video room session {request.session} with {modified}'.format(request=request, modified=modified))

    # Event handlers

    def _EH_janus_sip(self, event):
        if isinstance(event, janus.PluginEvent):
            event_id = event.plugindata.data.__id__
            try:
                handler = getattr(self, '_EH_janus_' + '_'.join(event_id))
            except AttributeError:
                self.log.warning('unhandled Janus SIP event: {event_name}'.format(event_name=event_id[-1]))
            else:
                self.log.debug('janus SIP event: {event_name} (handle_id={event.sender})'.format(event=event, event_name=event_id[-1]))
                handler(event)
        else:  # janus.CoreEvent
            try:
                handler = getattr(self, '_EH_janus_sip_' + event.janus)
            except AttributeError:
                self.log.warning('unhandled Janus SIP event: {event.janus}'.format(event=event))
            else:
                self.log.debug('janus SIP event: {event.janus} (handle_id={event.sender})'.format(event=event))
                handler(event)

    def _EH_janus_sip_error(self, event):
        # fixme: implement error handling
        self.log.error('got SIP error event: {}'.format(event.__data__))
        handle_id = event.sender
        if handle_id in self.sip_sessions:
            pass  # this is a session related event
        elif handle_id in self.account_handles_map:
            pass  # this is an account related event

    def _EH_janus_sip_webrtcup(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for webrtcup event'.format(event=event))
            return
        session_info.state = 'established'
        self.send(sylkrtc.SessionEstablishedEvent(session=session_info.id))
        self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
        self.log.info('established WEBRTC connection for session {session.id}'.format(session=session_info))

    def _EH_janus_sip_hangup(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            return
        if session_info.state != 'terminated':
            session_info.state = 'terminated'
            reason = event.reason or 'unspecified reason'
            self.send(sylkrtc.SessionTerminatedEvent(session=session_info.id, reason=reason))
            self.log.info('{session.direction} session {session.id} terminated ({reason})'.format(session=session_info, reason=reason))
            self._cleanup_session(session_info)

    def _EH_janus_sip_slowlink(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for slowlink event'.format(event=event))
            return
        if event.uplink:  # uplink is from janus' point of view
            if not session_info.slow_download:
                self.log.info('poor download connectivity for session {session.id}'.format(session=session_info))
            session_info.slow_download = True
        else:
            if not session_info.slow_upload:
                self.log.info('poor upload connectivity for session {session.id}'.format(session=session_info))
            session_info.slow_upload = True

    def _EH_janus_sip_media(self, event):
        pass

    def _EH_janus_sip_detached(self, event):
        pass

    def _EH_janus_sip_event_registering(self, event):
        try:
            account_info = self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for registering event'.format(event=event))
            return
        if account_info.registration_state != 'registering':
            account_info.registration_state = 'registering'
            self.send(sylkrtc.AccountRegisteringEvent(account=account_info.id))

    def _EH_janus_sip_event_registered(self, event):
        if event.sender in self.sip_sessions:  # skip 'registered' events from outgoing session handles
            return
        try:
            account_info = self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for registered event'.format(event=event))
            return
        if account_info.registration_state != 'registered':
            account_info.registration_state = 'registered'
            self.send(sylkrtc.AccountRegisteredEvent(account=account_info.id))
            self.log.info('registered {account.id} to receive incoming calls'.format(account=account_info))

    def _EH_janus_sip_event_registration_failed(self, event):
        try:
            account_info = self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for registration failed event'.format(event=event))
            return
        if account_info.registration_state != 'failed':
            account_info.registration_state = 'failed'
            reason = '{result.code} {result.reason}'.format(result=event.plugindata.data.result)
            self.send(sylkrtc.AccountRegistrationFailedEvent(account=account_info.id, reason=reason))
            self.log.info('registration for {account.id} failed: {reason}'.format(account=account_info, reason=reason))

    def _EH_janus_sip_event_incomingcall(self, event):
        try:
            account_info = self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for incoming call event'.format(event=event))
            return
        assert event.jsep is not None
        data = event.plugindata.data.result  # type: janus.SIPResultIncomingCall
        originator = sylkrtc.SIPIdentity(uri=data.username, display_name=data.displayname)
        session = SIPSessionInfo(uuid.uuid4().hex)
        session.janus_handle = account_info.janus_handle
        session.init_incoming(account_info, originator.uri, originator.display_name)
        self.sip_sessions.add(session)
        self.send(sylkrtc.AccountIncomingSessionEvent(account=account_info.id, session=session.id, originator=originator, sdp=event.jsep.sdp))
        self.log.info('received incoming session {session.id} from {session.remote_identity.uri!s} to {session.local_identity.uri!s}'.format(session=session))

    def _EH_janus_sip_event_missed_call(self, event):
        try:
            account_info = self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for missed call event'.format(event=event))
            return
        data = event.plugindata.data.result  # type: janus.SIPResultMissedCall
        originator = sylkrtc.SIPIdentity(uri=data.caller, display_name=data.displayname)
        self.send(sylkrtc.AccountMissedSessionEvent(account=account_info.id, originator=originator))
        self.log.info('missed incoming call from {originator.uri}'.format(originator=originator))

    def _EH_janus_sip_event_calling(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for calling event'.format(event=event))
            return
        session_info.state = 'progress'
        self.send(sylkrtc.SessionProgressEvent(session=session_info.id))
        self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))

    def _EH_janus_sip_event_accepted(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for accepted event'.format(event=event))
            return
        session_info.state = 'accepted'
        if session_info.direction == 'outgoing':
            assert event.jsep is not None
            self.send(sylkrtc.SessionAcceptedEvent(session=session_info.id, sdp=event.jsep.sdp))
        else:
            self.send(sylkrtc.SessionAcceptedEvent(session=session_info.id))
        self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))

    def _EH_janus_sip_event_hangup(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for hangup event'.format(event=event))
            return
        if session_info.state != 'terminated':
            session_info.state = 'terminated'
            data = event.plugindata.data.result  # type: janus.SIPResultHangup
            reason = '{0.code} {0.reason}'.format(data)
            self.send(sylkrtc.SessionTerminatedEvent(session=session_info.id, reason=reason))
            if session_info.direction == 'incoming' and data.code == 487:  # incoming call was cancelled -> missed
                self.send(sylkrtc.AccountMissedSessionEvent(account=session_info.account.id, originator=session_info.remote_identity.__dict__))
            if data.code >= 300:
                self.log.info('{session.direction} session {session.id} terminated ({reason})'.format(session=session_info, reason=reason))
            else:
                self.log.info('{session.direction} session {session.id} terminated'.format(session=session_info))
            self._cleanup_session(session_info)

    def _EH_janus_sip_event_declining(self, event):
        pass

    def _EH_janus_sip_event_hangingup(self, event):
        pass

    def _EH_janus_sip_event_proceeding(self, event):
        pass

    def _EH_janus_sip_event_progress(self, event):  # TODO: implement this to handle 183 w/ SDP -Dan
        pass

    def _EH_janus_sip_event_ringing(self, event):  # TODO: check if we want to use this -Dan
        pass

    def _EH_janus_videoroom(self, event):
        if isinstance(event, janus.PluginEvent):
            event_id = event.plugindata.data.__id__
            try:
                handler = getattr(self, '_EH_janus_' + '_'.join(event_id))
            except AttributeError:
                self.log.warning('unhandled Janus videoroom event: {event_name}'.format(event_name=event_id[-1]))
            else:
                self.log.debug('janus videoroom event: {event_name} (handle_id={event.sender})'.format(event=event, event_name=event_id[-1]))
                handler(event)
        else:  # janus.CoreEvent
            try:
                handler = getattr(self, '_EH_janus_videoroom_' + event.janus)
            except AttributeError:
                self.log.warning('unhandled Janus videoroom event: {event.janus}'.format(event=event))
            else:
                self.log.debug('janus videoroom event: {event.janus} (handle_id={event.sender})'.format(event=event))
                handler(event)

    def _EH_janus_videoroom_error(self, event):
        # fixme: implement error handling
        self.log.error('got videoroom error event: {}'.format(event.__data__))
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find video room session with handle ID {event.sender} for error event'.format(event=event))
            return
        if videoroom_session.type == 'publisher':
            pass
        else:
            pass

    def _EH_janus_videoroom_webrtcup(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find video room session with handle ID {event.sender} for webrtcup event'.format(event=event))
            return
        if videoroom_session.type == 'publisher':
            self.log.info('established WEBRTC connection for session {session.id}'.format(session=videoroom_session))
            self.send(sylkrtc.VideoroomSessionEstablishedEvent(session=videoroom_session.id))
        else:
            self.send(sylkrtc.VideoroomFeedEstablishedEvent(session=videoroom_session.parent_session.id, feed=videoroom_session.id))

    def _EH_janus_videoroom_hangup(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            return
        self._cleanup_videoroom_session(videoroom_session)

    def _EH_janus_videoroom_slowlink(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find video room session with handle ID {event.sender} for slowlink event'.format(event=event))
            return
        if event.uplink:  # uplink is from janus' point of view
            if not videoroom_session.slow_download:
                self.log.info('poor download connectivity to video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
            videoroom_session.slow_download = True
        else:
            if not videoroom_session.slow_upload:
                self.log.info('poor upload connectivity to video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
            videoroom_session.slow_upload = True

    def _EH_janus_videoroom_media(self, event):
        pass

    def _EH_janus_videoroom_detached(self, event):
        pass

    def _EH_janus_videoroom_joined(self, event):
        # send when a publisher successfully joined a room
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find video room session with handle ID {event.sender} for joined event'.format(event=event))
            return
        self.log.info('joined video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
        data = event.plugindata.data  # type: janus.VideoroomJoined
        videoroom_session.publisher_id = data.id
        room = videoroom_session.room
        assert event.jsep is not None
        self.send(sylkrtc.VideoroomSessionAcceptedEvent(session=videoroom_session.id, sdp=event.jsep.sdp))
        # send information about existing publishers
        publishers = []
        for publisher in data.publishers:  # type: janus.VideoroomPublisher
            try:
                publisher_session = room[publisher.id]
            except KeyError:
                self.log.warning('could not find matching session for publisher {publisher.id} during joined event'.format(publisher=publisher))
            else:
                publishers.append(dict(id=publisher_session.id, uri=publisher_session.account.id, display_name=publisher.display or ''))
        self.send(sylkrtc.VideoroomInitialPublishersEvent(session=videoroom_session.id, publishers=publishers))
        room.add(videoroom_session)  # adding the session to the room might also trigger sending an event with the active participants which must be sent last

    def _EH_janus_videoroom_attached(self, event):
        # sent when a feed is subscribed for a given publisher
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find video room session with handle ID {event.sender} for attached event'.format(event=event))
            return
        # get the session which originated the subscription
        base_session = videoroom_session.parent_session
        assert base_session is not None
        assert event.jsep is not None and event.jsep.type == 'offer'
        self.send(sylkrtc.VideoroomFeedAttachedEvent(session=base_session.id, feed=videoroom_session.id, sdp=event.jsep.sdp))

    def _EH_janus_videoroom_slow_link(self, event):
        pass

    def _EH_janus_videoroom_event_publishers(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find video room session with handle ID {event.sender} for publishers event'.format(event=event))
            return
        room = videoroom_session.room
        # send information about new publishers
        publishers = []
        for publisher in event.plugindata.data.publishers:  # type: janus.VideoroomPublisher
            try:
                publisher_session = room[publisher.id]
            except KeyError:
                self.log.warning('could not find matching session for publisher {publisher.id} during publishers event'.format(publisher=publisher))
                continue
            publishers.append(dict(id=publisher_session.id, uri=publisher_session.account.id, display_name=publisher.display or ''))
        self.send(sylkrtc.VideoroomPublishersJoinedEvent(session=videoroom_session.id, publishers=publishers))

    def _EH_janus_videoroom_event_leaving(self, event):
        # this is a publisher
        publisher_id = event.plugindata.data.leaving  # publisher_id == 'ok' when the event is about ourselves leaving the room, else the publisher's janus ID
        try:
            base_session = self.videoroom_sessions[event.sender]
        except KeyError:
            if publisher_id != 'ok':
                self.log.warning('could not find video room session with handle ID {event.sender} for leaving event'.format(event=event))
            return
        if publisher_id == 'ok':
            self.log.info('left video room {session.room.uri} with session {session.id}'.format(session=base_session))
            self._cleanup_videoroom_session(base_session)
            return
        try:
            publisher_session = base_session.feeds.pop(publisher_id)
        except KeyError:
            return
        self.send(sylkrtc.VideoroomPublishersLeftEvent(session=base_session.id, publishers=[publisher_session.id]))

    def _EH_janus_videoroom_event_left(self, event):
        # this is a subscriber
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            pass
        else:
            self._cleanup_videoroom_session(videoroom_session)

    def _EH_janus_videoroom_event_configured(self, event):
        pass

    def _EH_janus_videoroom_event_started(self, event):
        pass

    def _EH_janus_videoroom_event_unpublished(self, event):
        pass
