
import hashlib
import json
import os
import random
import re
import time
import uuid
import weakref

from application.python import Null
from application.python.weakref import defaultweakobjectmap
from application.system import makedirs
from eventlib import coros, proc
from eventlib.twistedutil import block_on
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor

from sylk.applications.webrtcgateway.configuration import GeneralConfig, get_room_config
from sylk.applications.webrtcgateway.logger import ConnectionLogger, VideoroomLogger
from sylk.applications.webrtcgateway.models import sylkrtc
from sylk.applications.webrtcgateway.storage import TokenStorage
from sylk.applications.webrtcgateway.util import GreenEvent


SIP_PREFIX_RE = re.compile('^sips?:')

sylkrtc_models = {model.sylkrtc.default_value: model for model in vars(sylkrtc).values() if hasattr(model, 'sylkrtc') and issubclass(model, sylkrtc.SylkRTCRequestBase)}


class ACLValidationError(Exception): pass


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
        self.config = get_room_config(uri)
        self.uri = uri
        self.record = self.config.record
        self.rec_dir = os.path.join(GeneralConfig.recording_dir, '%s/' % uri)
        self.id = random.getrandbits(32)    # janus needs numeric room names
        self.destroyed = False
        self.log = VideoroomLogger(self)
        self._session_id_map = weakref.WeakValueDictionary()
        self._publisher_id_map = weakref.WeakValueDictionary()

        if self.record:
            makedirs(self.rec_dir, 0o755)
            self.log.info('created (recording on)')
        else:
            self.log.info('created')

    def add(self, session):
        assert session.publisher_id is not None
        assert session.id not in self._session_id_map
        assert session.publisher_id not in self._publisher_id_map
        self._session_id_map[session.id] = session
        self._publisher_id_map[session.publisher_id] = session

    def __getitem__(self, key):
        try:
            return self._session_id_map[key]
        except KeyError:
            return self._publisher_id_map[key]

    def __len__(self):
        return len(self._session_id_map)


class VideoRoomSessionInfo(object):
    slow_download = SlowLinkDescriptor()
    slow_upload = SlowLinkDescriptor()

    def __init__(self, id):
        self.id = id
        self.account_id = None
        self.type = None  # publisher / subscriber
        self.publisher_id = None    # janus publisher ID for publishers / publisher session ID for subscribers
        self.janus_handle_id = None
        self.room = None
        self.parent_session = None
        self.slow_download = False
        self.slow_upload = False
        self.feeds = {}    # janus publisher ID -> our publisher ID

    def initialize(self, account_id, type, room):
        assert type in ('publisher', 'subscriber')
        self.account_id = account_id
        self.type = type
        self.room = room

    def __repr__(self):
        return '<%s: id=%s janus_handle_id=%s type=%s>' % (self.__class__.__name__, self.id, self.janus_handle_id, self.type)


class VideoRoomSessionContainer(object):
    def __init__(self):
        self._sessions = set()
        self._janus_handle_map = weakref.WeakValueDictionary()
        self._id_map = weakref.WeakValueDictionary()

    def add(self, session):
        assert session not in self._sessions
        assert session.janus_handle_id not in self._janus_handle_map
        assert session.id not in self._id_map
        self._sessions.add(session)
        self._janus_handle_map[session.janus_handle_id] = session
        self._id_map[session.id] = session

    def remove(self, session):
        self._sessions.discard(session)

    def clear(self):
        self._sessions.clear()

    def __len__(self):
        return len(self._sessions)

    def __iter__(self):
        return iter(self._sessions)

    def __getitem__(self, key):
        try:
            return self._id_map[key]
        except KeyError:
            return self._janus_handle_map[key]

    def __contains__(self, item):
        return item in self._id_map or item in self._janus_handle_map or item in self._sessions


class Operation(object):
    __slots__ = ('name', 'data')

    def __init__(self, name, data):
        self.name = name
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
        self.sessions_map = {}  # session ID -> session
        self.session_handles_map = {}  # Janus handle ID -> session
        self.videoroom_sessions = VideoRoomSessionContainer()    # session ID / janus handle ID -> session
        self.ready_event = GreenEvent()
        self.resolver = DNSLookup()
        self.proc = proc.spawn(self._operations_handler)
        self.operations_queue = coros.queue()
        self.log = ConnectionLogger(self)

    def start(self):
        self._create_janus_session()

    def stop(self):
        if self.ready_event.is_set():
            assert self.janus_session_id is not None
            for account_info in self.accounts_map.values():
                handle_id = account_info.janus_handle_id
                if handle_id is not None:
                    self.protocol.backend.janus_detach(self.janus_session_id, handle_id)
                    self.protocol.backend.janus_set_event_handler(handle_id, None)
            self.protocol.backend.janus_stop_keepalive(self.janus_session_id)
            self.protocol.backend.janus_destroy_session(self.janus_session_id)
        if self.proc is not None:
            self.proc.kill()
            self.proc = None
        # cleanup
        self.ready_event.clear()
        self.accounts_map.clear()
        self.account_handles_map.clear()
        self.sessions_map.clear()
        self.session_handles_map.clear()
        self.videoroom_sessions.clear()
        self.janus_session_id = None
        self.protocol = None

    def handle_message(self, data):
        try:
            request_type = data.pop('sylkrtc')
        except KeyError:
            self.log.error('could not get WebSocket message type')
            return
        self.ready_event.wait()
        try:
            model = sylkrtc_models[request_type]
        except KeyError:
            self.log.error('unknown WebSocket request type: %s' % request_type)
            return
        try:
            request = model(**data)
            request.validate()
        except Exception as e:
            self.log.error('%s: %s' % (request_type, e))
            transaction = data.get('transaction')  # we cannot rely on request.transaction as request may not have been created
            if transaction:
                self._send_response(sylkrtc.ErrorResponse(transaction=transaction, error=str(e)))
            return
        op = Operation(request_type, request)
        self.operations_queue.send(op)

    def validate_acl(self, room_uri, from_uri):
        cfg = get_room_config(room_uri)
        if cfg.access_policy == 'allow,deny':
            if cfg.allow.match(from_uri) and not cfg.deny.match(from_uri):
                return
            raise ACLValidationError
        else:
            if cfg.deny.match(from_uri) and not cfg.allow.match(from_uri):
                raise ACLValidationError

    # internal methods (not overriding / implementing the protocol API)

    def _send_response(self, response):
        response.validate()
        self._send_data(json.dumps(response.to_struct()))

    def _send_data(self, data):
        self.protocol.sendMessage(data)

    def _cleanup_session(self, session):
        @run_in_green_thread
        def do_cleanup():
            if self.janus_session_id is None:
                # The connection was closed, there is noting to do here
                return
            self.sessions_map.pop(session.id)
            if session.direction == 'outgoing':
                # Destroy plugin handle for outgoing sessions. For incoming ones it's the
                # same as the account handle, so don't
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)
            self.session_handles_map.pop(session.janus_handle_id)

        # give it some time to receive other hangup events
        reactor.callLater(2, do_cleanup)

    def _cleanup_videoroom_session(self, session):
        @run_in_green_thread
        def do_cleanup():
            if self.janus_session_id is None:
                # The connection was closed, there is noting to do here
                return
            if session in self.videoroom_sessions:
                self.videoroom_sessions.remove(session)
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(session.janus_handle_id, None)

        # give it some time to receive other hangup events
        reactor.callLater(2, do_cleanup)

    def _maybe_destroy_videoroom(self, videoroom):
        if videoroom is None:
            return

        @run_in_green_thread
        def f():
            if self.protocol is None:
                # The connection was closed
                return
            # destroy the room if empty
            if not videoroom and not videoroom.destroyed:
                videoroom.destroyed = True
                self.protocol.factory.videorooms.remove(videoroom)

                # create a handle to do the cleanup
                handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
                self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)

                data = {'request': 'destroy',
                        'room': videoroom.id}
                try:
                    block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
                except Exception:
                    pass
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
                self.protocol.backend.janus_set_event_handler(handle_id, None)

                videoroom.log.info('destroyed')

        # don't destroy it immediately
        reactor.callLater(5, f)

    @run_in_green_thread
    def _create_janus_session(self):
        if self.ready_event.is_set():
            self._send_response(sylkrtc.ReadyEvent())
            return
        try:
            self.janus_session_id = block_on(self.protocol.backend.janus_create_session())
            self.protocol.backend.janus_start_keepalive(self.janus_session_id)
        except Exception as e:
            self.log.warning('could not create session, disconnecting: %s' % e)
            self.protocol.disconnect(3000, unicode(e))
            return
        self._send_response(sylkrtc.ReadyEvent())
        self.ready_event.set()

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
        routes = self.resolver.lookup_sip_proxy(sip_uri, settings.sip.transport_list).wait()
        if not routes:
            raise DNSLookupError('no results found')

        route = random.choice([r for r in routes if r.transport == routes[0].transport])

        self.log.debug('DNS lookup for SIP proxy for {} yielded {}'.format(uri, route))

        # Build a proxy URI Sofia-SIP likes
        return 'sips:{route.address}:{route.port}'.format(route=route) if route.transport == 'tls' else str(route.uri)

    def _handle_conference_invite(self, originator, room, participants):
        for p in participants:
            try:
                account_info = self.accounts_map[p]
            except KeyError:
                continue
            data = dict(sylkrtc='account_event',
                        account=account_info.id,
                        event='conference_invite',
                        data=dict(originator=dict(uri=originator.id, display_name=originator.display_name), room=room.uri))
            room.log.info('invitation from %s for %s', originator.id, account_info.id)
            self.log.info('received an invitation from %s for %s to join video room %s', originator.id, account_info.id, room.uri)
            self._send_data(json.dumps(data))

    def _handle_janus_event_sip(self, handle_id, event_type, event):
        # TODO: use a model
        self.log.debug('janus SIP event: type={event_type} handle_id={handle_id} event={event}'.format(event_type=event_type, handle_id=handle_id, event=event))
        op = Operation('janus-event-sip', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(op)

    def _handle_janus_event_videoroom(self, handle_id, event_type, event):
        # TODO: use a model
        self.log.debug('janus video room event: type={event_type} handle_id={handle_id} event={event}'.format(event_type=event_type, handle_id=handle_id, event=event))
        op = Operation('janus-event-videoroom', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(op)

    def _operations_handler(self):
        while True:
            op = self.operations_queue.wait()
            handler = getattr(self, '_OH_%s' % op.name.replace('-', '_'), Null)
            try:
                handler(op.data)
            except Exception:
                self.log.exception('unhandled exception in operation %r' % op.name)
            del op, handler

    def _OH_account_add(self, request):
        try:
            if request.account in self.accounts_map:
                raise APIError('Account {request.account} already added'.format(request=request))

            # check if domain is acceptable
            domain = request.account.partition('@')[2]
            if not {'*', domain}.intersection(GeneralConfig.sip_domains):
                raise APIError('SIP domain not allowed: %s' % domain)

            # Create and store our mapping
            account_info = AccountInfo(request.account, request.password, request.display_name, request.user_agent)
            self.accounts_map[account_info.id] = account_info
        except APIError as e:
            self.log.error('account-add: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('added account {request.account} using {request.user_agent}'.format(request=request))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_remove(self, request):
        try:
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
        except APIError as e:
            self.log.error('account-remove: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('removed account {request.account}'.format(request=request))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_register(self, request):
        try:
            try:
                account_info = self.accounts_map[request.account]
            except KeyError:
                raise APIError('Unknown account specified: {request.account}'.format(request=request))

            try:
                proxy = self._lookup_sip_proxy(request.account)
            except DNSLookupError as e:
                raise APIError('DNS lookup error: {exception!s}'.format(exception=e))

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

            data = {'request': 'register',
                    'username': account_info.uri,
                    'display_name': account_info.display_name,
                    'user_agent': account_info.user_agent,
                    'ha1_secret': account_info.password,
                    'proxy': proxy}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
        except APIError as e:
            self.log.error('account-register: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_unregister(self, request):
        try:
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
        except APIError as e:
            self.log.error('account-unregister: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('unregistered {request.account} from receiving incoming calls'.format(request=request))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_devicetoken(self, request):
        try:
            if request.account not in self.accounts_map:
                raise APIError('Unknown account specified: {request.account}'.format(request=request))
            storage = TokenStorage()
            if request.old_token is not None:
                storage.remove(request.account, request.old_token)
                self.log.debug('removed token {request.old_token} for {request.account}'.format(request=request))
            if request.new_token is not None:
                storage.add(request.account, request.new_token)
                self.log.debug('added token {request.new_token} for {request.account}'.format(request=request))
        except APIError as e:
            self.log.error('account-devicetoken: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_create(self, request):
        try:
            if request.session in self.sessions_map:
                raise APIError('Session ID {request.session} already in use'.format(request=request))

            try:
                account_info = self.accounts_map[request.account]
            except KeyError:
                raise APIError('Unknown account specified: {request.account}'.format(request=request))

            try:
                proxy = self._lookup_sip_proxy(request.uri)
            except DNSLookupError:
                raise APIError('DNS lookup error')

            # Create a new plugin handle and 'register' it, without actually doing so
            handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.sip'))
            self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_sip)
            data = {'request': 'register',
                    'username': account_info.uri,
                    'display_name': account_info.display_name,
                    'user_agent': account_info.user_agent,
                    'ha1_secret': account_info.password,
                    'proxy': proxy,
                    'send_register': False}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))

            session_info = SIPSessionInfo(request.session)
            session_info.janus_handle_id = handle_id
            session_info.init_outgoing(request.account, request.uri)
            # TODO: create a "SessionContainer" object combining the 2
            self.sessions_map[session_info.id] = session_info
            self.session_handles_map[handle_id] = session_info

            data = {'request': 'call', 'uri': 'sip:%s' % SIP_PREFIX_RE.sub('', request.uri), 'srtp': 'sdes_optional'}
            jsep = {'type': 'offer', 'sdp': request.sdp}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data, jsep))
        except APIError as e:
            self.log.error('session-create: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('created outgoing session {request.session} from {request.account} to {request.uri}'.format(request=request))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_answer(self, request):
        try:
            try:
                session_info = self.sessions_map[request.session]
            except KeyError:
                raise APIError('Unknown session specified: {request.session}'.format(request=request))

            if session_info.direction != 'incoming':
                raise APIError('Cannot answer outgoing session {request.session}'.format(request=request))
            if session_info.state != 'connecting':
                raise APIError('Invalid state for answering session {session.id}: {session.state}'.format(session=session_info))

            data = {'request': 'accept'}
            jsep = {'type': 'answer', 'sdp': request.sdp}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data, jsep))
        except APIError as e:
            self.log.error('session-answer: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('answered incoming session {session.id}'.format(session=session_info))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_trickle(self, request):
        try:
            try:
                session_info = self.sessions_map[request.session]
            except KeyError:
                raise APIError('Unknown session specified: {request.session}'.format(request=request))

            if session_info.state == 'terminated':
                raise APIError('Session {request.session} is terminated'.format(request=request))

            candidates = [c.to_struct() for c in request.candidates]

            block_on(self.protocol.backend.janus_trickle(self.janus_session_id, session_info.janus_handle_id, candidates))
        except APIError as e:
            self.log.error('session-trickle: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            if not candidates:
                self.log.debug('session {session.id} negotiated ICE'.format(session=session_info))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_terminate(self, request):
        try:
            try:
                session_info = self.sessions_map[request.session]
            except KeyError:
                raise APIError('Unknown session specified: {request.session}'.format(request=request))

            if session_info.state not in ('connecting', 'progress', 'accepted', 'established'):
                raise APIError('Invalid state for terminating session {session.id}: {session.state}'.format(session=session_info))

            if session_info.direction == 'incoming' and session_info.state == 'connecting':
                data = {'request': 'decline', 'code': 486}
            else:
                data = {'request': 'hangup'}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data))
        except APIError as e:
            self.log.error('session-terminate: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('requested termination for session {session.id}'.format(session=session_info))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_videoroom_join(self, request):
        videoroom = None

        try:
            if request.session in self.videoroom_sessions:
                raise APIError('Session ID {request.session} already in use'.format(request=request))

            try:
                self.validate_acl(request.uri, request.account)
            except ACLValidationError:
                raise APIError('{request.account} is not allowed to join room {request.uri}'.format(request=request))

            try:
                account_info = self.accounts_map[request.account]
            except KeyError:
                raise APIError('Unknown account specified: {request.account}'.format(request=request))

            handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
            self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)

            # create the room if it doesn't exist
            #
            try:
                videoroom = self.protocol.factory.videorooms[request.uri]
            except KeyError:
                videoroom = VideoRoom(request.uri)
                self.protocol.factory.videorooms.add(videoroom)

            data = {'request': 'create',
                    'room': videoroom.id,
                    'publishers': 10,
                    'bitrate': 4*1024*1024,  # max bitrate = 4 Mb/s (if we do not specify this it defaults to 256Kb/s in janus)
                    'record': videoroom.record,
                    'rec_dir': videoroom.rec_dir}
            try:
                block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
            except Exception as e:
                code = getattr(e, 'code', -1)
                if code != 427:  # 427 means room already exists
                    block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
                    self.protocol.backend.janus_set_event_handler(handle_id, None)
                    raise APIError(str(e))

            # join the room
            data = {'request': 'joinandconfigure',
                    'room': videoroom.id,
                    'ptype': 'publisher',
                    'audio': True,
                    'video': True}
            if account_info.display_name:
                data['display'] = account_info.display_name
            jsep = {'type': 'offer', 'sdp': request.sdp}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data, jsep))

            videoroom_session = VideoRoomSessionInfo(request.session)
            videoroom_session.janus_handle_id = handle_id
            videoroom_session.initialize(request.account, 'publisher', videoroom)
            self.videoroom_sessions.add(videoroom_session)
        except APIError as e:
            self.log.error('videoroom-join: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            self._maybe_destroy_videoroom(videoroom)
        else:
            self.log.info('created session {request.session} from account {request.account} to video room {request.uri}'.format(request=request))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
            data = dict(sylkrtc='videoroom_event',
                        session=videoroom_session.id,
                        event='state',
                        data=dict(state='progress'))
            self._send_data(json.dumps(data))

    def _OH_videoroom_ctl(self, request):
        if request.option == 'trickle':
            trickle = request.trickle
            if not trickle:
                self.log.error("videoroom-ctl: missing 'trickle' field in request")
                return
            candidates = [c.to_struct() for c in trickle.candidates]
            session = trickle.session or request.session
            try:
                try:
                    videoroom_session = self.videoroom_sessions[session]
                except KeyError:
                    raise APIError('trickle: unknown video room session: {session}'.format(session=session))
                block_on(self.protocol.backend.janus_trickle(self.janus_session_id, videoroom_session.janus_handle_id, candidates))
            except APIError as e:
                self.log.error('videoroom-ctl: {exception!s}'.format(exception=e))
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                if not candidates and videoroom_session.parent_session is None:  # parent_session is None for publishers
                    self.log.debug('video room session {session.id} negotiated ICE'.format(session=videoroom_session))
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
        elif request.option == 'update':
            if not request.update:
                self.log.error("videoroom-ctl: missing 'update' field in request")
                return
            try:
                try:
                    videoroom_session = self.videoroom_sessions[request.session]
                except KeyError:
                    raise APIError('update: unknown video room session: {request.session}'.format(request=request))
                update_data = request.update.to_struct()
                if update_data:
                    data = dict(request='configure', room=videoroom_session.room.id, **update_data)
                    block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
            except APIError as e:
                self.log.error('videoroom-ctl: {exception!s}'.format(exception=e))
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                if update_data:
                    modified = ', '.join('{}={}'.format(key, update_data[key]) for key in update_data)
                    self.log.info('updated video room session {request.session} with {modified}'.format(request=request, modified=modified))
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
        elif request.option == 'feed-attach':
            feed_attach = request.feed_attach
            if not feed_attach:
                self.log.error("videoroom-ctl: missing 'feed_attach' field in request")
                return
            try:
                if feed_attach.session in self.videoroom_sessions:
                    raise APIError('feed-attach: video room session ID {feed.session} already in use'.format(feed=feed_attach))

                # get the 'base' session, the one used to join and publish
                try:
                    base_session = self.videoroom_sessions[request.session]
                except KeyError:
                    raise APIError('feed-attach: unknown video room session: {request.session}'.format(request=request))

                # get the publisher's session
                try:
                    publisher_session = base_session.room[feed_attach.publisher]
                except KeyError:
                    raise APIError('feed-attach: unknown publisher video room session to attach to: {feed.publisher}'.format(feed=feed_attach))
                if publisher_session.publisher_id is None:
                    raise APIError('feed-attach: video room session {session.id} does not have a publisher ID'.format(session=publisher_session))

                handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
                self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)

                # join the room as a listener
                data = {'request': 'join',
                        'room': base_session.room.id,
                        'ptype': 'listener',
                        'feed': publisher_session.publisher_id}
                block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))

                videoroom_session = VideoRoomSessionInfo(feed_attach.session)
                videoroom_session.janus_handle_id = handle_id
                videoroom_session.parent_session = base_session
                videoroom_session.publisher_id = publisher_session.id
                videoroom_session.initialize(base_session.account_id, 'subscriber', base_session.room)
                self.videoroom_sessions.add(videoroom_session)
                base_session.feeds[publisher_session.publisher_id] = publisher_session.id
            except APIError as e:
                self.log.error('videoroom-ctl: {exception!s}'.format(exception=e))
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
        elif request.option == 'feed-answer':
            feed_answer = request.feed_answer
            if not feed_answer:
                self.log.error("videoroom-ctl: missing 'feed_answer' field in request")
                return
            try:
                try:
                    videoroom_session = self.videoroom_sessions[request.feed_answer.session]
                except KeyError:
                    raise APIError('feed-answer: unknown video room session: {feed.session}'.format(feed=feed_answer))
                data = {'request': 'start',
                        'room': videoroom_session.room.id}
                jsep = {'type': 'answer', 'sdp': feed_answer.sdp}
                block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data, jsep))
            except APIError as e:
                self.log.error('videoroom-ctl: {exception!s}'.format(exception=e))
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
        elif request.option == 'feed-detach':
            feed_detach = request.feed_detach
            if not feed_detach:
                self.log.error("videoroom-ctl: missing 'feed_detach' field in request")
                return
            try:
                try:
                    base_session = self.videoroom_sessions[request.session]
                except KeyError:
                    raise APIError('feed-detach: unknown video room session: {request.session}'.format(request=request))
                try:
                    videoroom_session = self.videoroom_sessions[feed_detach.session]
                except KeyError:
                    raise APIError('feed-detach: unknown video room session to detach from: {feed.session}'.format(feed=feed_detach))
                data = {'request': 'leave'}
                block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
            except APIError as e:
                self.log.error('videoroom-ctl: {exception!s}'.format(exception=e))
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, videoroom_session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(videoroom_session.janus_handle_id, None)
                self.videoroom_sessions.remove(videoroom_session)
                try:
                    janus_publisher_id = next(k for k, v in base_session.feeds.iteritems() if v == videoroom_session.publisher_id)
                except StopIteration:
                    pass
                else:
                    base_session.feeds.pop(janus_publisher_id)
        elif request.option == 'invite-participants':
            invite_participants = request.invite_participants
            if not invite_participants:
                self.log.error("videoroom-ctl: missing 'invite_participants' field in request")
                return
            try:
                try:
                    base_session = self.videoroom_sessions[request.session]
                    account_info = self.accounts_map[base_session.account_id]
                except KeyError:
                    raise APIError('invite-participants: unknown video room session: {request.session}'.format(request=request))
            except APIError as e:
                self.log.error('videoroom-ctl: {exception!s}'.format(exception=e))
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
                for conn in self.protocol.factory.connections.difference([self]):
                    if conn.connection_handler:
                        conn.connection_handler._handle_conference_invite(account_info, base_session.room, invite_participants.participants)
        else:
            self.log.error('videoroom-ctl: unsupported option: {request.option!r}'.format(request=request))

    def _OH_videoroom_terminate(self, request):
        try:
            try:
                videoroom_session = self.videoroom_sessions[request.session]
            except KeyError:
                raise APIError('Unknown video room session: {request.session}'.format(request=request))
            data = {'request': 'leave'}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
        except APIError as e:
            self.log.error('videoroom-terminate: {exception!s}'.format(exception=e))
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.log.info('requesting termination for video room session {request.session}'.format(request=request))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
            data = dict(sylkrtc='videoroom_event',
                        session=videoroom_session.id,
                        event='state',
                        data=dict(state='terminated'))
            self._send_data(json.dumps(data))
            self._cleanup_videoroom_session(videoroom_session)
            self._maybe_destroy_videoroom(videoroom_session.room)

    # Event handlers

    def _OH_janus_event_sip(self, data):
        handle_id = data['handle_id']
        event_type = data['event_type']
        event = data['event']

        if event_type == 'event':
            self._janus_event_plugin_sip(data)
        elif event_type == 'webrtcup':
            try:
                session_info = self.session_handles_map[handle_id]
            except KeyError:
                self.log.warning('could not find session for handle ID %s' % handle_id)
                return
            session_info.state = 'established'
            data = dict(sylkrtc='session_event',
                        session=session_info.id,
                        event='state',
                        data=dict(state=session_info.state))
            self._send_data(json.dumps(data))  # TODO: SessionEvent model
            self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
            self.log.info('established WEBRTC connection for session {session.id}'.format(session=session_info))
        elif event_type == 'hangup':
            try:
                session_info = self.session_handles_map[handle_id]
            except KeyError:
                self.log.warning('could not find session for handle ID %s' % handle_id)
                return
            if session_info.state != 'terminated':
                session_info.state = 'terminated'
                code = event.get('code', 0)
                reason = event.get('reason', 'Unknown')
                reason = '%d %s' % (code, reason)
                data = dict(sylkrtc='session_event',
                            session=session_info.id,
                            event='state',
                            data=dict(state=session_info.state, reason=reason))
                # TODO: SessionEvent model
                self._send_data(json.dumps(data))
                self._cleanup_session(session_info)
                if code >= 300:
                    self.log.info('{session.direction} session {session.id} terminated ({reason})'.format(session=session_info, reason=reason))
                else:
                    self.log.info('{session.direction} session {session.id} terminated'.format(session=session_info))
        elif event_type in ('media', 'detached'):
            # ignore
            pass
        elif event_type == 'slowlink':
            try:
                session_info = self.session_handles_map[handle_id]
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
        assert(plugin_data['plugin'] == 'janus.plugin.sip')
        event_data = plugin_data['data']
        assert(event_data.get('sip') == 'event')

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
                self.sessions_map[session_id] = session
                self.session_handles_map[handle_id] = session
                data = dict(sylkrtc='account_event',
                            account=account_info.id,
                            session=session_id,
                            event='incoming_session',
                            data=dict(originator=session.remote_identity.__dict__, sdp=jsep['sdp']))
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
                data = dict(sylkrtc='account_event',
                            account=account_info.id,
                            event='registration_state',
                            data=registration_data)
            # TODO: AccountEvent model
            self._send_data(json.dumps(data))
        elif event_type in ('calling', 'accepted', 'hangup'):
            # session event
            try:
                session_info = self.session_handles_map[handle_id]
            except KeyError:
                self.log.warning('could not find session for handle ID %s' % handle_id)
                return
            if event_type == 'hangup' and session_info.state == 'terminated':
                return
            if event_type == 'calling':
                session_info.state = 'progress'
            elif event_type == 'accepted':
                session_info.state = 'accepted'
            elif event_type == 'hangup':
                session_info.state = 'terminated'
            self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
            data = dict(sylkrtc='session_event',
                        session=session_info.id,
                        event='state',
                        data=dict(state=session_info.state))
            if session_info.state == 'accepted' and session_info.direction == 'outgoing':
                assert jsep is not None
                data['data']['sdp'] = jsep['sdp']
            elif session_info.state == 'terminated':
                code = event_data.get('code', 0)
                reason = event_data.get('reason', 'Unknown')
                reason = '%d %s' % (code, reason)
                data['data']['reason'] = reason
            self._send_data(json.dumps(data))  # TODO: SessionEvent model
            if session_info.state == 'terminated':
                self._cleanup_session(session_info)
                if code >= 300:
                    self.log.info('{session.direction} session {session.id} terminated ({reason})'.format(session=session_info, reason=reason))
                else:
                    self.log.info('{session.direction} session {session.id} terminated'.format(session=session_info))
                # check if missed incoming call
                if session_info.direction == 'incoming' and code == 487:
                    data = dict(sylkrtc='account_event',
                                account=session_info.account_id,
                                event='missed_session',
                                data=dict(originator=session_info.remote_identity.__dict__))
                    # TODO: AccountEvent model
                    self._send_data(json.dumps(data))
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
            data = dict(sylkrtc='account_event',
                        account=account_info.id,
                        event='missed_session',
                        data=dict(originator=originator.__dict__))
            self.log.info('missed incoming call from {originator.uri}'.format(originator=originator))
            # TODO: AccountEvent model
            self._send_data(json.dumps(data))
        elif event_type in ('ack', 'declining', 'hangingup', 'proceeding'):
            pass  # ignore
        else:
            self.log.warning('unexpected SIP plugin event type: %s' % event_type)

    def _OH_janus_event_videoroom(self, data):
        handle_id = data['handle_id']
        event_type = data['event_type']

        if event_type == 'event':
            self._janus_event_plugin_videoroom(data)
        elif event_type == 'webrtcup':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find video room session for handle ID %s' % handle_id)
                return
            if videoroom_session.parent_session is None:
                self.log.info('established WEBRTC connection for session {session.id}'.format(session=videoroom_session))
                data = dict(sylkrtc='videoroom_event',
                            session=videoroom_session.id,
                            event='state',
                            data=dict(state='established'))
            else:
                # this is a subscriber session
                data = dict(sylkrtc='videoroom_event',
                            session=videoroom_session.parent_session.id,
                            event='feed_established',
                            data=dict(state='established', subscription=videoroom_session.id))
            self._send_data(json.dumps(data))
        elif event_type == 'hangup':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                self.log.warning('could not find video room session for handle ID %s' % handle_id)
                return
            self._cleanup_videoroom_session(videoroom_session)
            self._maybe_destroy_videoroom(videoroom_session.room)
        elif event_type in ('media', 'detached'):
            pass
        elif event_type == 'slowlink':
            try:
                videoroom_session = (session_info for session_info in self.videoroom_sessions if session_info.janus_handle_id == handle_id).next()
            except StopIteration:
                self.log.warning('could not find video room session for Janus handle ID %s' % handle_id)
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
                self.log.warning('could not find video room session for handle ID %s' % handle_id)
                return
            room = videoroom_session.room
            videoroom_session.publisher_id = event_data['id']
            room.add(videoroom_session)
            jsep = event.get('jsep', None)
            assert jsep is not None
            data = dict(sylkrtc='videoroom_event',
                        session=videoroom_session.id,
                        event='state',
                        data=dict(state='accepted', sdp=jsep['sdp']))
            self._send_data(json.dumps(data))
            self.log.info('joined video room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
            room.log.info('{session.account_id} has joined the room'.format(session=videoroom_session))
            # send information about existing publishers
            publishers = []
            for p in event_data['publishers']:
                publisher_id = p['id']
                publisher_display = p.get('display', '')
                try:
                    publisher_session = room[publisher_id]
                except KeyError:
                    self.log.warning('could not find matching session for publisher %s' % publisher_id)
                    continue
                item = {'id': publisher_session.id,
                        'uri': publisher_session.account_id,
                        'display_name': publisher_display}
                publishers.append(item)
            data = dict(sylkrtc='videoroom_event',
                        session=videoroom_session.id,
                        event='initial_publishers',
                        data=dict(publishers=publishers))
            self._send_data(json.dumps(data))
        elif event_type == 'event':
            if 'publishers' in event_data:
                try:
                    videoroom_session = self.videoroom_sessions[handle_id]
                except KeyError:
                    self.log.warning('could not find video room session for handle ID %s' % handle_id)
                    return
                room = videoroom_session.room
                # send information about new publishers
                publishers = []
                for p in event_data['publishers']:
                    publisher_id = p['id']
                    publisher_display = p.get('display', '')
                    try:
                        publisher_session = room[publisher_id]
                    except KeyError:
                        self.log.warning('could not find matching session for publisher %s' % publisher_id)
                        continue
                    item = {'id': publisher_session.id,
                            'uri': publisher_session.account_id,
                            'display_name': publisher_display}
                    publishers.append(item)
                data = dict(sylkrtc='videoroom_event',
                            session=videoroom_session.id,
                            event='publishers_joined',
                            data=dict(publishers=publishers))
                self._send_data(json.dumps(data))
            elif 'leaving' in event_data:
                try:
                    base_session = self.videoroom_sessions[handle_id]
                except KeyError:
                    self.log.warning('could not find video room session for handle ID %s' % handle_id)
                    return
                janus_publisher_id = event_data['leaving']
                if janus_publisher_id == 'ok':  # the id is 'ok' when the notification is about ourselves leaving the room
                    self.log.info('left video room {session.room.uri} with session {session.id}'.format(session=base_session))  # todo: include session.id in log?
                    base_session.room.log.info('{session.account_id} has left the room'.format(session=base_session))
                try:
                    publisher_id = base_session.feeds.pop(janus_publisher_id)
                except KeyError:
                    return
                data = dict(sylkrtc='videoroom_event',
                            session=base_session.id,
                            event='publishers_left',
                            data=dict(publishers=[publisher_id]))
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
                self.log.warning('could not find video room session for handle ID %s' % handle_id)
                return
            # get the session which originated the subscription
            base_session = videoroom_session.parent_session
            assert base_session is not None
            jsep = event.get('jsep', None)
            assert jsep is not None
            assert jsep['type'] == 'offer'
            data = dict(sylkrtc='videoroom_event',
                        session=base_session.id,
                        event='feed_attached',
                        data=dict(sdp=jsep['sdp'], subscription=videoroom_session.id))
            self._send_data(json.dumps(data))
        elif event_type == 'slow_link':
            pass
        else:
            self.log.warning('received unexpected video room plugin event: type={} data={}'.format(event_type, event_data))
