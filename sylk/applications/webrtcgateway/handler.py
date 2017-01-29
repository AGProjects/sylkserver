import json
import os
import random
import re
import uuid
import weakref

from application.python import Null
from application.system import makedirs
from datetime import datetime
from eventlib import coros, proc
from eventlib.twistedutil import block_on
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor

from sylk.applications.webrtcgateway.configuration import GeneralConfig, get_room_config
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.models import sylkrtc
from sylk.applications.webrtcgateway.storage import TokenStorage
from sylk.applications.webrtcgateway.util import GreenEvent

SIP_PREFIX_RE = re.compile('^sips?:')

class ACLValidationError(Exception): pass

sylkrtc_models = {
    # account management
    'account-add'         : sylkrtc.AccountAddRequest,
    'account-remove'      : sylkrtc.AccountRemoveRequest,
    'account-register'    : sylkrtc.AccountRegisterRequest,
    'account-unregister'  : sylkrtc.AccountUnregisterRequest,
    'account-devicetoken' : sylkrtc.AccountDeviceTokenRequest,
    # session management
    'session-create'      : sylkrtc.SessionCreateRequest,
    'session-answer'      : sylkrtc.SessionAnswerRequest,
    'session-trickle'     : sylkrtc.SessionTrickleRequest,
    'session-terminate'   : sylkrtc.SessionTerminateRequest,
    # video conference management
    'videoroom-join'      : sylkrtc.VideoRoomJoinRequest,
    'videoroom-ctl'       : sylkrtc.VideoRoomControlRequest,
    'videoroom-terminate' : sylkrtc.VideoRoomTerminateRequest,
}


class AccountInfo(object):
    def __init__(self, id, password, display_name=None, user_agent=None):
        self.id = id
        self.password = password
        self.display_name = display_name
        self.user_agent = user_agent
        self.registration_state = None
        self.janus_handle_id = None
        self.fcm_token = None

    @property
    def uri(self):
        return 'sip:%s' % self.id


class SessionPartyIdentity(object):
    def __init__(self, uri, display_name=''):
        self.uri = uri
        self.display_name = display_name


class SIPSessionInfo(object):
    def __init__(self, id):
        self.id = id
        self.direction = None
        self.state = None
        self.account_id = None
        self.local_identity = None     # instance of SessionPartyIdentity
        self.remote_identity = None    # instance of SessionPartyIdentity
        self.janus_handle_id = None
        self.trickle_ice_active = False

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
        self._session_id_map = weakref.WeakValueDictionary()
        self._publisher_id_map = weakref.WeakValueDictionary()

        self.start_time = datetime.now()
        self.participants = {}

        if self.record:
            makedirs(self.rec_dir, 0755)
            log.msg('Video room %s created with recording in %s' % (self.uri, self.rec_dir))
        else:
            log.msg('Video room %s created without recording' % self.uri)

    def add(self, session):
        assert session.publisher_id is not None
        assert session.id not in self._session_id_map
        assert session.publisher_id not in self._publisher_id_map
        self._session_id_map[session.id] = session
        self._publisher_id_map[session.publisher_id] = session
        log.msg('Video room %s: added session %s for %s' % (self.uri, session.id, session.account_id))

    def __getitem__(self, key):
        try:
            return self._session_id_map[key]
        except KeyError:
            return self._publisher_id_map[key]

    def __len__(self):
        return len(self._session_id_map)


class VideoRoomSessionInfo(object):
    def __init__(self, id):
        self.id = id
        self.account_id = None
        self.type = None  # publisher / subscriber
        self.publisher_id = None    # janus publisher ID for publishers / publisher session ID for subscribers
        self.janus_handle_id = None
        self.room = None
        self.parent_session = None
        self.feeds = {}    # janus publisher ID -> our publisher ID
        self.trickle_ice_active = False

    def initialize(self, account_id, type, room):
        assert type in ('publisher', 'subscriber')
        self.account_id = account_id
        self.type = type
        self.room = room
        log.msg('Video room %s: new session %s initialized by %s' % (self.room.uri, self.id, self.account_id))


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

    def count(self):
        return len(self._sessions)

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

    @property
    def end_point_address(self):
        return self.protocol.peer

    def start(self):
        self._create_janus_session()

    def stop(self):
        if self.ready_event.is_set():
            assert self.janus_session_id is not None
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
            log.warn('Error getting WebSocket message type')
            return
        self.ready_event.wait()
        try:
            model = sylkrtc_models[request_type]
        except KeyError:
            log.warn('Unknown request type: %s' % request_type)
            return
        try:
            request = model(**data)
        except Exception, e:
            log.error('%s: %s' % (request_type, e))
            return

        try:
            request.validate()
        except Exception, e:
            log.error('%s: %s' % (request_type, e))
            if request.transaction:
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
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
        if GeneralConfig.trace_websocket:
            self.protocol.factory.ws_logger.msg("OUT", ISOTimestamp.now(), data)
        self.protocol.sendMessage(data, False)

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

                log.msg('Video room %s destroyed' % videoroom.uri)

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
        except Exception, e:
            log.warn('Error creating session, disconnecting: %s' % e)
            self.protocol.disconnect(3000, unicode(e))
            return
        self._send_response(sylkrtc.ReadyEvent())
        self.ready_event.set()

    def _lookup_sip_proxy(self, account):
        sip_uri = SIPURI.parse('sip:%s' % account)

        # The proxy dance: Sofia-SIP seems to do a DNS lookup per SIP message when a domain is passed
        # as the proxy, so do the resolution ourselves and give it pre-resolver proxy URL. Since we use
        # caching to avoid long delays, we randomize the results matching the highest priority route's
        # transport.
        proxy = GeneralConfig.outbound_sip_proxy
        if proxy is not None:
            proxy_uri = SIPURI(host=proxy.host,
                               port=proxy.port,
                               parameters={'transport': proxy.transport})
        else:
            proxy_uri = SIPURI(host=sip_uri.host)
        settings = SIPSimpleSettings()
        routes = self.resolver.lookup_sip_proxy(proxy_uri, settings.sip.transport_list).wait()
        if not routes:
            raise DNSLookupError('no results found')

        # Get all routes with the highest priority transport and randomly pick one
        route = random.choice([r for r in routes if r.transport == routes[0].transport])

        # Build a proxy URI Sofia-SIP likes
        return '%s:%s:%d%s' % ('sips' if route.transport == 'tls' else 'sip',
                               route.address,
                               route.port,
                               ';transport=%s' % route.transport if route.transport != 'tls' else '')

    def _handle_conference_invite(self, originator, room_uri, participants):
        for p in participants:
            try:
                account_info = self.accounts_map[p]
            except KeyError:
                continue
            data = dict(sylkrtc='account_event',
                        account=account_info.id,
                        event='conference_invite',
                        data=dict(originator=dict(uri=originator.id, display_name=originator.display_name),
                                  room=room_uri))
            log.msg('Video room %s: invitation from %s to %s' % (room_uri, originator.id, account_info.id))
            self._send_data(json.dumps(data))

    def _handle_janus_event_sip(self, handle_id, event_type, event):
        # TODO: use a model
        op = Operation('janus-event-sip', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(op)

    def _handle_janus_event_videoroom(self, handle_id, event_type, event):
        # TODO: use a model
        op = Operation('janus-event-videoroom', data=dict(handle_id=handle_id, event_type=event_type, event=event))
        self.operations_queue.send(op)

    def _operations_handler(self):
        while True:
            op = self.operations_queue.wait()
            handler = getattr(self, '_OH_%s' % op.name.replace('-', '_'), Null)
            try:
                handler(op.data)
            except Exception:
                log.exception('Unhandled exception in operation "%s"' % op.name)
            del op, handler

    def _OH_account_add(self, request):
        # extract the fields to avoid going through the descriptor several times
        account = request.account
        password = request.password
        display_name = request.display_name
        user_agent = request.user_agent

        try:
            if account in self.accounts_map:
                raise APIError('Account %s already added' % account)

            # check if domain is acceptable
            domain = account.partition('@')[2]
            if not {'*', domain}.intersection(GeneralConfig.sip_domains):
                raise APIError('SIP domain not allowed: %s' % domain)

            # Create and store our mapping
            account_info = AccountInfo(account, password, display_name, user_agent)
            self.accounts_map[account_info.id] = account_info
        except APIError, e:
            log.error('account_add: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.protocol.usage['accounts'].add(account_info.id)
            log.msg('Account %s added using %s at %s' % (account, user_agent, self.end_point_address))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_remove(self, request):
        # extract the fields to avoid going through the descriptor several times
        account = request.account

        try:
            self._remove_account(account)
        except APIError, e:
            log.error('account_remove: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _remove_account(self, account):
        log.msg('Account %s will be removed...' % account)

        try:
            account_info = self.accounts_map.pop(account)
        except KeyError:
            raise APIError('Unknown account specified: %s' % account)

        handle_id = account_info.janus_handle_id
        if handle_id is not None:
            # Destroy the existing plugin handle
            block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
            self.protocol.backend.janus_set_event_handler(handle_id, None)
            self.account_handles_map.pop(handle_id)
            self.protocol.usage['devices'].discard(handle_id)

        self.protocol.usage['accounts'].discard(account)
        log.msg('Account %s removed' % account)

    def _OH_account_register(self, request):
        # extract the fields to avoid going through the descriptor several times
        account = request.account

        try:
            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            try:
                proxy = self._lookup_sip_proxy(account)
            except DNSLookupError:
                raise APIError('DNS lookup error')

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
        except APIError, e:
            log.error('account-register: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            log.msg('Account %s will register using %s' % (account, account_info.user_agent))
            self.protocol.usage['devices'].add(handle_id)
            self.protocol.usage['user_agents'].add(account_info.user_agent)
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_unregister(self, request):
        # extract the fields to avoid going through the descriptor several times
        account = request.account

        try:
            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            handle_id = account_info.janus_handle_id
            if handle_id is not None:
                log.msg('Account %s will unregister' % account)
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
                self.protocol.backend.janus_set_event_handler(handle_id, None)
                account_info.janus_handle_id = None
                self.account_handles_map.pop(handle_id)
        except APIError, e:
            log.error('account-unregister: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self.protocol.usage['devices'].discard(handle_id)
            self.protocol.usage['accounts'].discard(account_info.id)
            log.msg('Account %s removed' % account)
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_account_devicetoken(self, request):
        # extract the fields to avoid going through the descriptor several times
        account = request.account
        old_token = request.old_token
        new_token = request.new_token

        try:
            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            storage = TokenStorage()
            if old_token is not None:
                storage.remove(account, old_token)
                log.msg('Removed device token %s for account %s using %s' % (old_token, account, account_info.user_agent))
                account_info.fcm_token = None
            if new_token is not None:
                storage.add(account, new_token)
                account_info.fcm_token = new_token
                log.msg('Added device token %s for account %s using %s' % (new_token, account, account_info.user_agent))
        except APIError, e:
            log.error('account-devicetoken: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_create(self, request):
        # extract the fields to avoid going through the descriptor several times
        account = request.account
        session = request.session
        uri = request.uri
        sdp = request.sdp

        try:
            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            if session in self.sessions_map:
                raise APIError('Session ID (%s) already in use' % session)

            # Create a new plugin handle and 'register' it, without actually doing so
            handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.sip'))
            self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_sip)
            try:
                proxy = self._lookup_sip_proxy(account_info.id)
            except DNSLookupError:
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
                self.protocol.backend.janus_set_event_handler(handle_id, None)
                raise APIError('DNS lookup error')
            data = {'request': 'register',
                    'username': account_info.uri,
                    'display_name': account_info.display_name,
                    'user_agent': account_info.user_agent,
                    'ha1_secret': account_info.password,
                    'proxy': proxy,
                    'send_register': False}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))

            session_info = SIPSessionInfo(session)
            session_info.janus_handle_id = handle_id
            session_info.init_outgoing(account, uri)
            # TODO: create a "SessionContainer" object combining the 2
            self.sessions_map[session_info.id] = session_info
            self.session_handles_map[handle_id] = session_info

            data = {'request': 'call', 'uri': 'sip:%s' % SIP_PREFIX_RE.sub('', uri), 'srtp': 'sdes_optional'}
            jsep = {'type': 'offer', 'sdp': sdp}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data, jsep))
        except APIError, e:
            log.error('session-create: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            log.msg('Outgoing session %s from %s to %s created using %s from %s' % (session, account, uri, account_info.user_agent, self.end_point_address))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_answer(self, request):
        # extract the fields to avoid going through the descriptor several times
        session = request.session
        sdp = request.sdp

        try:
            try:
                session_info = self.sessions_map[session]
            except KeyError:
                raise APIError('Unknown session specified: %s' % session)

            if session_info.direction != 'incoming':
                raise APIError('Cannot answer outgoing session')
            if session_info.state != 'connecting':
                raise APIError('Invalid state for session answer')

            data = {'request': 'accept'}
            jsep = {'type': 'answer', 'sdp': sdp}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data, jsep))
        except APIError, e:
            log.error('session-answer: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            log.msg('%s answered session %s' % (session_info.account_id, session))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_trickle(self, request):
        # extract the fields to avoid going through the descriptor several times
        session = request.session
        candidates = [c.to_struct() for c in request.candidates]

        try:
            try:
                session_info = self.sessions_map[session]
            except KeyError:
                raise APIError('Unknown session specified: %s' % session)
            if session_info.state == 'terminated':
                raise APIError('Session is terminated')

            try:
                account_info = self.accounts_map[session_info.account_id]
            except KeyError:
                raise APIError('Unknown account specified: %s' % session_info.account_id)

            block_on(self.protocol.backend.janus_trickle(self.janus_session_id, session_info.janus_handle_id, candidates))
        except APIError, e:
            log.error('session-trickle: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:

            if candidates:
                if not session_info.trickle_ice_active:
                    log.msg('Session %s: ICE negotiation started by %s using %s' % (session_info.id, session_info.account_id, account_info.user_agent))
                    session_info.trickle_ice_active = True
            else:
                log.msg('Session %s: ICE negotiation ended by %s using %s' % (session_info.id, session_info.account_id, account_info.user_agent))
                session_info.trickle_ice_active = False
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

    def _OH_session_terminate(self, request):
        # extract the fields to avoid going through the descriptor several times
        session = request.session

        try:
            try:
                session_info = self.sessions_map[session]
            except KeyError:
                raise APIError('Unknown session specified: %s' % session)

            if session_info.state not in ('connecting', 'progress', 'accepted', 'established'):
                raise APIError('Invalid state for session terminate: \"%s\"' % session_info.state)

            if session_info.direction == 'incoming' and session_info.state == 'connecting':
                data = {'request': 'decline', 'code': 486}
            else:
                data = {'request': 'hangup'}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, session_info.janus_handle_id, data))
        except APIError, e:
            log.error('session-terminate: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            log.msg('%s terminated session %s' % (session_info.account_id, session))
            self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
            if '@guest' in session_info.account_id:
                self._remove_account(session_info.account_id)

    def _OH_videoroom_join(self, request):
        account = request.account
        session = request.session
        uri = request.uri
        sdp = request.sdp
        videoroom = None

        try:
            try:
                self.validate_acl(uri, account)
            except ACLValidationError:
                raise APIError('%s is not allowed to join room %s' % (account, uri))

            try:
                account_info = self.accounts_map[account]
            except KeyError:
                raise APIError('Unknown account specified: %s' % account)

            if session in self.videoroom_sessions:
                raise APIError('Video room session ID (%s) already in use' % session)

            handle_id = block_on(self.protocol.backend.janus_attach(self.janus_session_id, 'janus.plugin.videoroom'))
            self.protocol.backend.janus_set_event_handler(handle_id, self._handle_janus_event_videoroom)

            # create the room if it doesn't exist

            try:
                videoroom = self.protocol.factory.videorooms[uri]
            except KeyError:
                videoroom = VideoRoom(uri)
                self.protocol.factory.videorooms.add(videoroom)

            data = {'request': 'create',
                    'room': videoroom.id,
                    'publishers': 10,
                    'record': videoroom.record,
                    'rec_dir': videoroom.rec_dir
                    }
            try:
                block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data))
            except Exception, e:
                code = getattr(e, 'code', -1)
                if code != 427:    # 417 == room exists
                    block_on(self.protocol.backend.janus_detach(self.janus_session_id, handle_id))
                    self.protocol.backend.janus_set_event_handler(handle_id, None)
                    raise APIError(str(e))

            # join the room
            data = {'request': 'joinandconfigure',
                    'room': videoroom.id,
                    'ptype': 'publisher',
                    'audio': True,
                    'video': True
                    }
            if account_info.display_name:
                data['display'] = account_info.display_name
            jsep = {'type': 'offer', 'sdp': sdp}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, handle_id, data, jsep))

            videoroom_session = VideoRoomSessionInfo(session)
            videoroom_session.janus_handle_id = handle_id
            videoroom_session.initialize(account, 'publisher', videoroom)
            self.videoroom_sessions.add(videoroom_session)
        except APIError, e:
            log.error('videoroom-join: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            self._maybe_destroy_videoroom(videoroom)
        else:
            log.msg('Video room %s: joined by %s using %s (%d participants present) from %s' % (videoroom.uri, account, account_info.user_agent, self.videoroom_sessions.count(), self.end_point_address))

            # track room participants and their sessions
            videoroom.participants[videoroom_session.id] = { 'account': account_info,
                                                             'join_time': datetime.now(),
                                                             'slow_download': None,
                                                             'slow_upload': None,
                                                             'subscriber_feeds': set(),
                                                             'publisher_feeds' : set([videoroom_session.id])
                                                             }
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
                log.error('videoroom-ctl: missing field')
                return
            candidates = [c.to_struct() for c in trickle.candidates]
            session = trickle.session or request.session
            try:
                try:
                    videoroom_session = self.videoroom_sessions[session]
                except KeyError:
                    raise APIError('trickle: unknown video room session ID specified: %s' % session)

                try:
                    account_info = self.accounts_map[videoroom_session.account_id]
                except KeyError:
                    raise APIError('Unknown account specified: %s' % videoroom_session.account_id)

                block_on(self.protocol.backend.janus_trickle(self.janus_session_id, videoroom_session.janus_handle_id, candidates))
            except APIError, e:
                log.error('videoroom-ctl: %s' % e)
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                if candidates:
                    if not videoroom_session.trickle_ice_active:
                        log.msg('Video room %s: ICE negotiation started by %s using %s' % (videoroom_session.room.uri, videoroom_session.account_id, account_info.user_agent))
                        videoroom_session.trickle_ice_active = True
                else:
                    log.msg('Video room %s: ICE negotiation ended by %s using %s' % (videoroom_session.room.uri, videoroom_session.account_id, account_info.user_agent))
                    videoroom_session.trickle_ice_active = False

                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
        elif request.option == 'feed-attach':
            feed_attach = request.feed_attach
            if not feed_attach:
                log.error('videoroom-ctl: missing field')
                return
            try:
                if feed_attach.session in self.videoroom_sessions:
                    raise APIError('feed-attach: video room session ID (%s) already in use' % feed_attach.session)

                # get the 'base' session, the one used to join and publish
                try:
                    base_session = self.videoroom_sessions[request.session]
                except KeyError:
                    raise APIError('feed-attach: unknown video room session ID specified: %s' % request.session)

                # get the publisher's session
                try:
                    publisher_session = base_session.room[feed_attach.publisher]
                except KeyError:
                    raise APIError('feed-attach: unknown publisher video room session ID specified: %s' % feed_attach.publisher)
                if publisher_session.publisher_id is None:
                    raise APIError('feed-attach: video room session ID does not have a publisher ID' % feed_attach.publisher)

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
            except APIError, e:
                log.error('videoroom-ctl: %s' % e)
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                log.msg('Video room %s: %s attached to %s' % (base_session.room.uri, base_session.account_id, feed_attach.publisher))
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))

                # track room participants and their sessions
                videoroom = self.protocol.factory.videorooms[publisher_session.room.uri]
                participant = videoroom.participants[publisher_session.id]
                participant['subscriber_feeds'].add(videoroom_session.id)

        elif request.option == 'feed-answer':
            feed_answer = request.feed_answer
            if not feed_answer:
                log.error('videoroom-ctl: missing field')
                return
            try:
                try:
                    videoroom_session = self.videoroom_sessions[request.feed_answer.session]
                except KeyError:
                    raise APIError('feed-answer: unknown video room session ID specified: %s' % feed_answer.session)
                data = {'request': 'start',
                        'room': videoroom_session.room.id}
                jsep = {'type': 'answer', 'sdp': feed_answer.sdp}
                block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data, jsep))
            except APIError, e:
                log.error('videoroom-ctl: %s' % e)
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
        elif request.option == 'feed-detach':
            feed_detach = request.feed_detach
            if not feed_detach:
                log.error('videoroom-ctl: missing field')
                return
            try:
                try:
                    base_session = self.videoroom_sessions[request.session]
                except KeyError:
                    raise APIError('feed-detach: unknown video room session ID specified: %s' % request.session)
                try:
                    videoroom_session = self.videoroom_sessions[feed_detach.session]
                except KeyError:
                    raise APIError('feed-detach: unknown video room session ID specified: %s' % feed_detach.session)
                data = {'request': 'leave'}
                block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
            except APIError, e:
                log.error('videoroom-ctl: %s' % e)
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
                block_on(self.protocol.backend.janus_detach(self.janus_session_id, videoroom_session.janus_handle_id))
                self.protocol.backend.janus_set_event_handler(videoroom_session.janus_handle_id, None)
                self.videoroom_sessions.remove(videoroom_session)

                # track room participants and their sessions
                videoroom = self.protocol.factory.videorooms[base_session.room.uri]
                participant = videoroom.participants[base_session.id]
                participant['subscriber_feeds'].discard(videoroom_session.id)

                try:
                    janus_publisher_id = next(k for k, v in base_session.feeds.iteritems() if v == videoroom_session.publisher_id)
                except StopIteration:
                    pass
                else:
                    base_session.feeds.pop(janus_publisher_id)
        elif request.option == 'invite-participants':
            invite_participants = request.invite_participants
            if not invite_participants:
                log.error('videoroom-ctl: missing field')
                return
            try:
                try:
                    base_session = self.videoroom_sessions[request.session]
                    account_info = self.accounts_map[base_session.account_id]
                except KeyError:
                    raise APIError('invite-participants: unknown video room session ID specified: %s' % request.session)
            except APIError, e:
                log.error('videoroom-ctl: %s' % e)
                self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
            else:
                self._send_response(sylkrtc.AckResponse(transaction=request.transaction))
                for conn in self.protocol.factory.connections.difference([self]):
                    if conn.connection_handler:
                        conn.connection_handler._handle_conference_invite(account_info, base_session.room.uri, invite_participants.participants)
        else:
            log.error('videoroom-ctl: unsupported option: %s' % request.option)

    def _OH_videoroom_terminate(self, request):
        session = request.session

        try:
            try:
                videoroom_session = self.videoroom_sessions[session]
            except KeyError:
                raise APIError('Unknown video room session ID specified: %s' % session)
            data = {'request': 'leave'}
            block_on(self.protocol.backend.janus_message(self.janus_session_id, videoroom_session.janus_handle_id, data))
        except APIError, e:
            log.error('videoroom-terminate: %s' % e)
            self._send_response(sylkrtc.ErrorResponse(transaction=request.transaction, error=str(e)))
        else:
            log.msg('Video room %s: %s left the room (%d participants present)' % (videoroom_session.room.uri, videoroom_session.account_id, self.videoroom_sessions.count()))
            if '@guest' in videoroom_session.account_id:
                self._remove_account(videoroom_session.account_id)

            videoroom = self.protocol.factory.videorooms[videoroom_session.room.uri]
            del(videoroom.participants[videoroom_session.id])

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
                log.msg('Could not find session for handle ID %s' % handle_id)
                return
            session_info.state = 'established'
            data = dict(sylkrtc='session_event',
                        session=session_info.id,
                        event='state',
                        data=dict(state=session_info.state))
            direction = session_info.direction.title()
            log.msg('%s session %s from %s to %s state: %s' % (direction,
                                                     session_info.id,
                                                     session_info.local_identity.uri if direction == 'Outgoing' else session_info.remote_identity.uri,
                                                     session_info.remote_identity.uri if direction == 'Outgoing' else session_info.local_identity.uri,
                                                     session_info.state))

            # TODO: SessionEvent model
            self._send_data(json.dumps(data))
        elif event_type == 'hangup':
            try:
                session_info = self.session_handles_map[handle_id]
            except KeyError:
                log.msg('Could not find session for handle ID %s' % handle_id)
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
                direction = session_info.direction.title()
                log.msg('%s session %s from %s to %s terminated (%s)' % (direction,
                                                                     session_info.id,
                                                                     session_info.local_identity.uri if direction == 'Outgoing' else session_info.remote_identity.uri,
                                                                     session_info.remote_identity.uri if direction == 'Outgoing' else session_info.local_identity.uri,
                                                                     reason))
                if '@guest' in session_info.local_identity.uri:
                    self._remove_account(session_info.local_identity.uri)

        elif event_type in ('media', 'detached'):
            # ignore
            pass
        elif event_type == 'slowlink':
            #  TODO something
            pass
        else:
            log.warn('Received unexpected event type: %s' % event_type)

    def _janus_event_plugin_sip(self, data):
        handle_id = data['handle_id']
        event = data['event']
        plugin_data = event['plugindata']
        assert(plugin_data['plugin'] == 'janus.plugin.sip')
        event_data = event['plugindata']['data']
        assert(event_data.get('sip', '') == 'event')

        if 'result' not in event_data:
            log.warn('Unexpected event: %s' % event)
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
                log.warn('Could not find account for handle ID %s' % handle_id)
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
                log.msg('Incoming session %s from %s to %s created' % (session.id,
                                                                   session.remote_identity.uri,
                                                                   session.local_identity.uri))
            else:
                registration_state = event_type
                if registration_state == 'registration_failed':
                    registration_state = 'failed'
                if account_info.registration_state == registration_state:
                    return
                account_info.registration_state = registration_state
                registration_data = dict(state=registration_state)
                if registration_state == 'failed':
                    code = event_data['code']
                    reason = event_data['reason']
                    registration_data['reason'] = '%d %s' % (code, reason)
                    log.msg('Account %s registration failed: %s (%s)' % (account_info.id, code, reason))
                elif registration_state == 'registered':
                    log.msg('Account %s registered using %s from %s' % (account_info.id, account_info.user_agent, self.end_point_address))
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
                log.warn('Could not find session for handle ID %s' % handle_id)
                return
            if event_type == 'hangup' and session_info.state == 'terminated':
                return
            if event_type == 'calling':
                session_info.state = 'progress'
            elif event_type == 'accepted':
                session_info.state = 'accepted'
            elif event_type == 'hangup':
                session_info.state = 'terminated'
            data = dict(sylkrtc='session_event',
                        session=session_info.id,
                        event='state',
                        data=dict(state=session_info.state))
            direction = session_info.direction.title()
            log.msg('%s session %s from %s to %s state: %s' % (direction,
                                                     session_info.id,
                                                     session_info.local_identity.uri if direction == 'Outgoing' else session_info.remote_identity.uri,
                                                     session_info.remote_identity.uri if direction == 'Outgoing' else session_info.local_identity.uri,
                                                     session_info.state))
            if session_info.state == 'accepted' and session_info.direction == 'outgoing':
                assert jsep is not None
                data['data']['sdp'] = jsep['sdp']
            elif session_info.state == 'terminated':
                code = event_data.get('code', 0)
                reason = event_data.get('reason', 'Unknown')
                reason = '%d %s' % (code, reason)
                data['data']['reason'] = reason
            # TODO: SessionEvent model
            self._send_data(json.dumps(data))
            if session_info.state == 'terminated':
                self._cleanup_session(session_info)
                direction = session_info.direction.title()
                log.msg('%s session %s from %s to %s terminated (%s)' % (direction,
                                                         session_info.id,
                                                         session_info.local_identity.uri if direction == 'Outgoing' else session_info.remote_identity.uri,
                                                         session_info.remote_identity.uri if direction == 'Outgoing' else session_info.local_identity.uri,
                                                         reason))

                if '@guest' in session_info.local_identity.uri:
                    self._remove_account(session_info.local_identity.uri)

                # check if missed incoming call
                if session_info.direction == 'incoming' and code == 487:
                    data = dict(sylkrtc='account_event',
                                account=session_info.account_id,
                                event='missed_session',
                                data=dict(originator=session_info.remote_identity.__dict__))
                    log.msg('Incoming session from %s to %s was not answered ' % (session_info.remote_identity.uri, session_info.local_identity.uri))
                    # TODO: AccountEvent model
                    self._send_data(json.dumps(data))
        elif event_type == 'missed_call':
            try:
                account_info = self.account_handles_map[handle_id]
            except KeyError:
                log.warn('Could not find account for handle ID %s' % handle_id)
                return
            originator_uri = SIP_PREFIX_RE.sub('', event_data['caller'])
            originator_display_name = event_data.get('displayname', '').replace('"', '')
            # We have no session, so create an identity object by hand
            originator = SessionPartyIdentity(originator_uri, originator_display_name)
            data = dict(sylkrtc='account_event',
                        account=account_info.id,
                        event='missed_session',
                        data=dict(originator=originator.__dict__))
            log.msg('Incoming session from %s missed' % originator.uri)
            # TODO: AccountEvent model
            self._send_data(json.dumps(data))
        elif event_type in ('ack', 'declining', 'hangingup', 'proceeding'):
            # ignore
            pass
        else:
            log.warn('Unexpected SIP plugin event type: %s' % event_type)

    def _OH_janus_event_videoroom(self, data):
        handle_id = data['handle_id']
        event_type = data['event_type']

        if event_type == 'event':
            self._janus_event_plugin_videoroom(data)
        elif event_type == 'webrtcup':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                log.warn('Could not find videoroom session for handle ID %s' % handle_id)
                return
            base_session = videoroom_session.parent_session
            if base_session is None:
                data = dict(sylkrtc='videoroom_event',
                            session=videoroom_session.id,
                            event='state',
                            data=dict(state='established'))
            else:
                # this is a subscriber session
                data = dict(sylkrtc='videoroom_event',
                            session=base_session.id,
                            event='feed_established',
                            data=dict(state='established', subscription=videoroom_session.id))
            log.msg('Video room %s: session established to %s' % (videoroom_session.room.uri, videoroom_session.account_id))
            self._send_data(json.dumps(data))
        elif event_type == 'hangup':
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                log.warn('Could not find video room session for handle ID %s' % handle_id)
                return
            log.msg('Video room %s: session terminated to %s' % (videoroom_session.room.uri, videoroom_session.account_id))
            self._cleanup_videoroom_session(videoroom_session)
            self._maybe_destroy_videoroom(videoroom_session.room)
        elif event_type in ('media', 'detached'):
            # ignore
            pass
        elif event_type == 'slowlink':
            log.info('Slow link message received for Janus handle ID %s' % handle_id)
            try:
                videoroom_session = (session_info for session_info in self.videoroom_sessions if session_info.janus_handle_id == handle_id).next()
            except StopIteration:
                log.warn('Could not find video room session for Janus handle ID %s' % handle_id)
            else:

                try:
                    account_info = self.accounts_map[videoroom_session.account_id]
                except KeyError:
                    raise APIError('Unknown account specified: %s' % videoroom_session.account_id)

                try:
                    uplink = data['event']['uplink']
                except KeyError:
                    log.warn('Could not find uplink in slowlink event data')
                else:
                    # track room participants and their sessions
                    publisher_session = videoroom_session if videoroom_session.type == 'publisher' else videoroom_session.parent_session
                    try:
                        videoroom = self.protocol.factory.videorooms[publisher_session.room.uri]
                        participant = videoroom.participants[publisher_session.id]
                    except KeyError:
                        raise APIError('Cannot find publisher session for account %s' % publisher_session.account_id)
                    else:
                        if uplink:
                            log.msg('Video room %s: %s has poor upload on %s connection on %s' % (videoroom_session.room.uri, videoroom_session.account_id, videoroom_session.type, account_info.user_agent))
                            participant['slow_upload'] = datetime.now()
                        else:
                            log.msg('Video room %s: %s has poor download on %s connection on %s' % (videoroom_session.room.uri, videoroom_session.account_id, videoroom_session.type, account_info.user_agent))
                            participant['slow_download'] = datetime.now()

        else:
            log.warn('Received unexpected event type %s: data=%s' % (event_type, data))

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
                log.warn('Could not find video room session for handle ID %s' % handle_id)
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
            # send information about existing publishers
            publishers = []
            for p in event_data['publishers']:
                publisher_id = p['id']
                publisher_display = p.get('display', '')
                try:
                    publisher_session = room[publisher_id]
                except KeyError:
                    log.warn('Could not find matching session for publisher %s' % publisher_id)
                    continue
                item = {
                    'id': publisher_session.id,
                    'uri': publisher_session.account_id,
                    'display_name': publisher_display,
                }
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
                    log.warn('Could not find videoroom session for handle ID %s' % handle_id)
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
                        log.warn('Could not find matching session for publisher %s' % publisher_id)
                        continue
                    item = {
                        'id': publisher_session.id,
                        'uri': publisher_session.account_id,
                        'display_name': publisher_display,
                    }
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
                    log.warn('Could not find video room session for handle ID %s' % handle_id)
                    return
                janus_publisher_id = event_data['leaving']
                try:
                    publisher_id = base_session.feeds.pop(janus_publisher_id)
                except KeyError:
                    return
                data = dict(sylkrtc='videoroom_event',
                            session=base_session.id,
                            event='publishers_left',
                            data=dict(publishers=[publisher_id]))
                self._send_data(json.dumps(data))
            elif {'started', 'unpublished', 'left'}.intersection(event_data):
                # ignore
                pass
            else:
                log.warn('Received unexpected plugin "event" event')
        elif event_type == 'attached':
            # sent when a feed is subscribed for a given publisher
            try:
                videoroom_session = self.videoroom_sessions[handle_id]
            except KeyError:
                log.warn('Could not find videoroom session for handle ID %s' % handle_id)
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
            log.warn('Received unexpected plugin event type %s: plugin_data=%s, event_data=%s' % (event_type, plugin_data, event_data))

