
"""
SIP backend using SylkServer's own SIP stack (SIP SIMPLE SDK).

SylkServer does the SIP signalling (REGISTER, INVITE through
sylk.session.Session) and Janus is used only for the media, through the
NoSIP plugin, which converts between the WebRTC PeerConnection and plain
RTP/SRTP.

Registration: every account of a WebRTC client registers a contact of its
own, pointing to this SylkServer:

    sip:<user>@<sylkserver>:<port>;transport=<t>;x-sylk-app=webrtcgateway;x-sylk-gw=<token>[;pn_...]

The x-sylk-app parameter makes SylkServer hand the requests sent to the
contact to the webrtcgateway application, and the x-sylk-gw token tells
which client connection and account they are for (see find_contact). The
push notification parameters given by the client (account-devicetoken) are
added to the contact URI, as the Janus SIP plugin does.

Calls: a sylk.session.Session does the SIP side, with one RelayedRTPStream
per media line. The streams do not handle any media themselves: their SDP
comes from the NoSIP plugin (generate() turns the browser's offer into a
plain SDP pointing to Janus, process() turns the remote answer into the
JSEP answer for the browser) and Janus relays the RTP between the browser
and the remote party.

Implemented: outgoing calls (with early media), hangup from either side,
in-dialog MESSAGE. Not implemented yet: incoming calls (rejected with 480),
re-INVITEs (session-update), DTMF with SIP INFO (RFC 4733 DTMF is relayed
in the RTP).
"""

import secrets

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from sipsimple.core import (ContactHeader, Credentials, FromHeader, Header, Registration, RouteHeader, SDPConnection, SDPMediaStream,
                            SDPSession, SIPURI, SIPCoreError, ToHeader)
from sipsimple.streams import IMediaStream, UnknownStreamError
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import call_in_green_thread
from zope.interface import implementer

from sylk.accounts import DefaultAccount
from sylk.session import Session

from ..janus import JanusBackend, JanusError, NoSIPPluginHandle, _is_pstn_uri
from ..models import janus
from .base import SIPAccountBackend, SIPBackend, SIPBackendError, SIPCallBackend


__all__ = 'SIPSimpleBackend', 'CONTACT_TOKEN_PARAMETER', 'contact_token', 'find_contact'


CONTACT_APP_PARAMETER = 'x-sylk-app'
CONTACT_TOKEN_PARAMETER = 'x-sylk-gw'

REGISTRATION_DURATION = 600  # seconds, same as the Janus SIP plugin's register_ttl

_contacts = {}  # contact token -> SIPSimpleAccount


def contact_token(uri):
    """The x-sylk-gw token of a SIP URI (a request URI), or None"""
    parameters = getattr(uri, 'parameters', None) or {}
    for key in (CONTACT_TOKEN_PARAMETER, CONTACT_TOKEN_PARAMETER.encode()):
        try:
            value = parameters[key]
        except (KeyError, TypeError):
            continue
        return value.decode() if isinstance(value, bytes) else value
    return None


def find_contact(uri):
    """The SIPSimpleAccount a request URI was sent to, or None if it is unknown (stale)"""
    token = contact_token(uri)
    return _contacts.get(token) if token is not None else None


def user_agent(account_info):
    """The client's User-Agent, made safe for use as a SIP header value, or None"""
    return ' '.join(str(account_info.user_agent or '').split())[:200] or None


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


@implementer(IObserver)
class SIPSimpleAccount(SIPAccountBackend):
    def __init__(self, backend, account_info):
        super(SIPSimpleAccount, self).__init__(backend, account_info)
        self.token = secrets.token_hex(8)
        self.contact_header = None
        self.route = None
        self._registration = None
        self._registered = False

    @property
    def from_header(self):
        return FromHeader(SIPURI.parse(self.account_info.uri), self.account_info.display_name)

    @property
    def credentials(self):
        username = _text(SIPURI.parse(self.account_info.uri).user)
        return Credentials(username=username, password=self.account_info.password.encode('utf-8'), digest=True)  # the password is the HA1 hash

    @property
    def registration(self):
        if self._registration is None:
            self._registration = Registration(self.from_header, credentials=self.credentials, duration=REGISTRATION_DURATION, extra_headers=self.extra_headers)
            NotificationCenter().add_observer(self, sender=self._registration)
        return self._registration

    @property
    def extra_headers(self):
        # The requests sent on behalf of the client carry the client's
        # User-Agent (the one it gave in account-add), as with the Janus SIP
        # plugin, instead of SylkServer's. The SIP stack only adds its own
        # User-Agent header when the request has none.
        return [Header('User-Agent', user_agent(self.account_info))] if user_agent(self.account_info) else []

    def lookup_proxy(self):
        # REGISTER goes where the account's other SIP requests go (the Thor
        # node responsible for the account, the outbound proxy or the
        # domain's DNS records), never to the local server.
        return self.handler._lookup_sip_target_route(self.account_info.id, sender=self.account_info.id, local=False)

    def reset(self):
        # The same Registration (and contact) is reused for every attempt, so
        # a new attempt refreshes the existing binding instead of adding one.
        pass

    def register(self, proxy):
        self.route = proxy
        self.contact_header = self._build_contact_header(proxy)
        _contacts[self.token] = self
        self.handler.post_backend_event(self.handler.sip_account_registering, self.account_info)
        self.log.debug('sipsimple: REGISTER {} with contact {} via {}'.format(self.account_info.id, self.contact_header.uri, proxy.uri))
        self.registration.register(self.contact_header, RouteHeader(proxy.uri), timeout=10)

    def release(self):
        self._end()

    def connection_lost(self):
        self._end()

    def _end(self):
        _contacts.pop(self.token, None)
        registration, self._registered = self._registration, False
        if registration is None:
            return
        if registration.is_registered:
            try:
                registration.end(timeout=2)
            except SIPCoreError:
                pass
            else:
                return  # keep observing it until the unREGISTER is answered, see _NH_SIPRegistrationDidEnd
        NotificationCenter().discard_observer(self, sender=registration)
        self._registration = None

    def _build_contact_header(self, route):
        base = DefaultAccount().contact[route]  # the SIP address of this server for the route's transport
        user = SIPURI.parse(self.account_info.uri).user
        parameters = dict(base.parameters)
        parameters[CONTACT_APP_PARAMETER] = 'webrtcgateway'
        parameters[CONTACT_TOKEN_PARAMETER] = self.token
        for name, value in self.account_info.contact_params.items():
            if value is not None:
                parameters[name] = str(value)
        uri = SIPURI(user=user, host=base.host, port=base.port, parameters=parameters)
        # No Contact header parameters (e.g. +sip.instance): with str keys the
        # SDK writes garbage after the URI, and the Janus SIP plugin did not
        # send any either.
        return ContactHeader(uri)

    # Registration notifications (from the SIP stack's thread)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPRegistrationDidSucceed(self, notification):
        if self.token not in _contacts:  # released meanwhile, the unREGISTER is on its way
            return
        self._registered = True
        self.log.debug('sipsimple: registered {} for {}s, {} contact(s) bound to the account'.format(
            self.account_info.id, notification.data.expires_in, len(notification.data.contact_header_list)))
        self.handler.post_backend_event(self.handler.sip_account_registered, self.account_info)

    def _NH_SIPRegistrationDidFail(self, notification):
        if self.token not in _contacts:
            return
        self._registered = False
        reason = '{} {}'.format(notification.data.code, _text(notification.data.reason)) if notification.data.code else _text(notification.data.reason)
        self.handler.post_backend_event(self.handler.sip_account_registration_failed, self.account_info, reason)

    def _NH_SIPRegistrationWillExpire(self, notification):
        if self.token not in _contacts or self.contact_header is None:
            return
        self.registration.register(self.contact_header, RouteHeader(self.route.uri), timeout=10)

    def _NH_SIPRegistrationDidEnd(self, notification):
        if self.token in _contacts and notification.data.expired:
            # the binding expired without being refreshed (e.g. the refresh could not be sent)
            self._registered = False
            self.handler.post_backend_event(self.handler.sip_account_registration_failed, self.account_info, 'registration expired')
        elif self.token not in _contacts:
            NotificationCenter().discard_observer(self, sender=notification.sender)
            if notification.sender is self._registration:
                self._registration = None

    def _NH_SIPRegistrationDidNotEnd(self, notification):
        if self.token not in _contacts:
            NotificationCenter().discard_observer(self, sender=notification.sender)
            if notification.sender is self._registration:
                self._registration = None


class _NoEncryption(object):
    # The SDK's own encryption (ZRTP, SDES) is not involved: SDES-SRTP is
    # negotiated by Janus (NoSIP) and passes through in the SDP.
    type = None
    active = False


@implementer(IMediaStream)
class RelayedRTPStream(object):
    """
    One media line (audio or video) of a call whose media Janus relays. It
    only provides the SDP: the media line comes from the NoSIP plugin, and
    starting the stream means giving the remote answer to NoSIP. It is not a
    registered stream type, so it is never created for other applications'
    sessions.
    """

    priority = 0
    hold_supported = False
    on_hold = on_hold_by_local = on_hold_by_remote = False
    preserve_connection = True  # keep the Janus address in c=, see sylk.session
    ice_active = False

    def __init__(self, call, media_type, sdp_index):
        self.call = call
        self.type = media_type
        self.sdp_index = sdp_index  # index of the media line in the NoSIP SDP
        self.index = None           # index in the session's SDP, set by Session
        self.session = None
        self.direction = None
        self.encryption = _NoEncryption()

    def __repr__(self):
        return '<{0.__class__.__name__} {0.type} index={0.index}>'.format(self)

    @classmethod
    def new_from_sdp(cls, session, remote_sdp, stream_index):
        raise UnknownStreamError

    def initialize(self, session, direction):
        self.session = session
        self.direction = direction
        NotificationCenter().post_notification('MediaStreamDidInitialize', sender=self)

    def get_local_media(self, remote_sdp=None, index=0, *args, **kw):
        return self.call.local_media(self)

    def start(self, local_sdp, remote_sdp, stream_index):
        self.call.stream_started(self, remote_sdp)

    def validate_update(self, remote_sdp, stream_index):
        return True

    def update(self, local_sdp, remote_sdp, stream_index):
        pass

    def hold(self):
        pass

    def unhold(self):
        pass

    def reset(self, stream_index):
        pass

    def deactivate(self):
        pass

    def end(self):
        pass


@implementer(IObserver)
class SIPSimpleCall(SIPCallBackend):
    def __init__(self, backend, session_info, account):
        super(SIPSimpleCall, self).__init__(backend, session_info)
        self.account = account          # SIPSimpleAccount
        self.janus_handle = None        # NoSIPPluginHandle
        self.session = None             # sylk.session.Session
        self.streams = []
        self.local_sdp = None           # the current plain SDP from NoSIP (SDPSession)
        self._awaiting_answer = False   # we sent an offer, the next remote SDP is the answer for NoSIP
        self._answer_waiters = []       # streams started while the answer is being processed
        self._processing_answer = False
        self._answer_jsep = None        # the JSEP answer for the browser, until the call is accepted
        self._early_media = False
        self._ended = False

    # Outgoing call

    def call(self, uri, sdp, headers):
        # should only be called from a green thread
        session_info = self.session_info
        self.janus_handle = NoSIPPluginHandle(self.handler.janus_session, event_handler=self._janus_event_handler)
        session_info.janus_handle = self.janus_handle
        try:
            srtp = None if _is_pstn_uri(uri) else 'sdes_optional'
            result = self.janus_handle.generate(sdp, 'offer', srtp=srtp)
            self._set_local_sdp(result.sdp)
            self.streams = [RelayedRTPStream(self, media.media.decode() if isinstance(media.media, bytes) else media.media, index)
                            for index, media in enumerate(self.local_sdp.media) if media.port != 0]
            if not self.streams:
                raise SIPBackendError('no audio or video in the offer')

            target = uri if uri.startswith(('sip:', 'sips:')) else 'sip:' + uri
            route = self.handler._lookup_sip_target_route(uri, sender=self.account.account_info.id, local=False)
            contact_header = self.account._build_contact_header(route)
        except Exception:
            self.janus_handle.detach()
            raise

        self.session = Session(DefaultAccount())
        NotificationCenter().add_observer(self, sender=self.session)
        self._awaiting_answer = True
        self.session.connect(self.account.from_header, ToHeader(SIPURI.parse(target)), route, self.streams,
                             contact_header=contact_header, credentials=self.account.credentials,
                             extra_headers=self._extra_headers(headers))
        self.log.info('sipsimple: calling {} via {} with {}'.format(target, route.uri, ', '.join(stream.type for stream in self.streams)))
        self.handler.post_backend_event(self.handler.sip_session_calling, session_info)

    def _extra_headers(self, headers):
        forbidden = {'to', 'from', 'via', 'contact', 'route', 'record-route', 'user-agent'}
        extra_headers = list(self.account.extra_headers)
        for header in headers or []:
            name, value = header.get('name'), header.get('value')
            if name and value is not None and name.lower() not in forbidden:
                extra_headers.append(Header(name, str(value)))
        return extra_headers

    # SDP

    def _set_local_sdp(self, sdp):
        self.local_sdp = SDPSession.parse(sdp.encode() if isinstance(sdp, str) else sdp)

    def local_media(self, stream):
        media = SDPMediaStream.new(self.local_sdp.media[stream.sdp_index])
        if media.connection is None and self.local_sdp.connection is not None:
            # NoSIP puts the Janus address in the session level c= line, but
            # only the media lines are copied to the session's SDP
            media.connection = SDPConnection.new(self.local_sdp.connection)
        return media

    def stream_started(self, stream, remote_sdp):
        # called by Session (in its green thread) for every stream the remote party accepted
        if not self._awaiting_answer:
            NotificationCenter().post_notification('MediaStreamDidStart', sender=stream)
            return
        self._answer_waiters.append(stream)
        if not self._processing_answer:
            self._processing_answer = True
            call_in_green_thread(self._process_answer, str(remote_sdp))

    def _process_answer(self, sdp):
        notification_center = NotificationCenter()
        waiters, self._answer_waiters = self._answer_waiters, []
        try:
            result, jsep = self.janus_handle.process(sdp, 'answer')
        except JanusError as e:
            self.log.warning('sipsimple: Janus could not use the answer of {}: {}'.format(self.session_info.remote_identity.uri, e))
            for stream in waiters:
                notification_center.post_notification('MediaStreamDidFail', sender=stream, data=NotificationData(context='starting', reason='Janus: {}'.format(e)))
        else:
            self._awaiting_answer = False
            early = getattr(self.session._invitation, 'state', None) == 'early'
            self.handler.post_backend_event(self._remote_answer, jsep.sdp if jsep is not None else None, early)
            for stream in waiters:
                notification_center.post_notification('MediaStreamDidStart', sender=stream)
        finally:
            self._processing_answer = False

    def _remote_answer(self, sdp, early):
        if self._ended or sdp is None:
            return
        self.session_info.call_id = self.session.call_id
        if early:
            self._early_media = True
            self.handler.sip_session_early_media(self.session_info, sdp, call_id=self.session_info.call_id)
        else:
            self._answer_jsep = sdp

    # Requests from the client

    def answer(self, sdp, headers=None):
        raise SIPBackendError('incoming calls are not implemented yet in the sipsimple SIP backend')

    def decline(self, code):
        raise SIPBackendError('incoming calls are not implemented yet in the sipsimple SIP backend')

    def hangup(self):
        if self.session is not None:
            call_in_green_thread(self.session.end)

    def update(self, sdp, jsep_type, headers=None):
        raise SIPBackendError('session updates (re-INVITE) are not implemented yet in the sipsimple SIP backend')

    def trickle(self, candidates):
        if self.janus_handle is None:
            raise SIPBackendError('the session ended')
        self.janus_handle.trickle(candidates)

    def send_message(self, content_type, content):
        try:
            self.session.send_message(content_type, content)
        except (SIPCoreError, AttributeError) as e:
            raise SIPBackendError('could not send in-dialog message: {}'.format(e))
        # The SIP stack does not report the final response of an in-dialog
        # MESSAGE, so the message is reported as sent (202) once it is out.
        self.handler.post_backend_event(self.handler.sip_session_message_delivery, self.session_info, 202, 'Sent')

    def send_dtmf_info(self, digit, duration=None):
        raise SIPBackendError('DTMF with SIP INFO is not available in the sipsimple SIP backend (RFC 4733 DTMF is relayed in the RTP)')

    def cleanup(self):
        self._end(detach=True)

    def connection_lost(self):
        # The Janus session is destroyed right after, which detaches the handle
        self._end(detach=False)

    def _end(self, detach):
        # may be called from the reactor thread (connection_lost), so the blocking calls run in green threads
        if self._ended:
            return
        self._ended = True
        if self.session is not None and self.session.state not in ('terminating', 'terminated'):
            call_in_green_thread(self.session.end)
        handle, self.janus_handle = self.janus_handle, None
        if handle is not None:
            if detach:
                call_in_green_thread(handle.detach)
            else:
                JanusBackend().set_event_handler(handle.id, None)

    # Janus events (non-transaction events of the NoSIP handle)

    def _janus_event_handler(self, event):
        self.handler.post_backend_event(self._handle_janus_event, event)

    def _handle_janus_event(self, event):
        if self._ended or not isinstance(event, janus.CoreEvent):
            return
        session_info = self.session_info
        if event.janus == 'webrtcup':
            self.handler.sip_session_established(session_info)
        elif event.janus == 'hangup':
            # the PeerConnection with the browser is gone
            self.log.info('sipsimple: WebRTC media of session {} ended ({}), ending the call'.format(session_info.id, getattr(event, 'reason', None) or 'no reason'))
            if self.session is not None and self.session.state not in ('terminating', 'terminated'):
                call_in_green_thread(self.session.end)
        elif event.janus == 'slowlink':
            self.handler.sip_session_slow_link(session_info, uplink=event.uplink)
        elif event.janus == 'media':
            self.handler.sip_session_media(session_info, getattr(event, 'type', None) or 'audio', bool(getattr(event, 'receiving', False)))

    # Session notifications

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        self.session_info.call_id = self.session.call_id
        self.handler.post_backend_event(self.handler.sip_session_proceeding, self.session_info, notification.data.code)

    def _NH_SIPSessionGotRingIndication(self, notification):
        self.handler.post_backend_event(self.handler.sip_session_ringing, self.session_info)

    def _NH_SIPSessionDidStart(self, notification):
        self.handler.post_backend_event(self._accepted)

    def _accepted(self):
        if self._ended:
            return
        self.session_info.call_id = self.session.call_id
        self.handler.sip_session_accepted(self.session_info, sdp=self._answer_jsep, call_id=self.session_info.call_id)
        self._answer_jsep = None

    def _NH_SIPSessionDidFail(self, notification):
        NotificationCenter().discard_observer(self, sender=notification.sender)
        data = notification.data
        if data.code:
            code, reason = data.code, '{} {}'.format(data.code, _text(data.reason))
        else:
            code, reason = (487, '487 Session Cancelled') if data.originator == 'local' else (500, _text(data.failure_reason) or 'Session failed')
        self.handler.post_backend_event(self._terminated, reason, code)

    def _NH_SIPSessionDidEnd(self, notification):
        NotificationCenter().discard_observer(self, sender=notification.sender)
        reason = 'Session ended by the {} party'.format('local' if notification.data.originator == 'local' else 'remote')
        self.handler.post_backend_event(self._terminated, reason, 200)

    def _terminated(self, reason, code):
        if self.session is not None and self.session.call_id:
            self.session_info.call_id = self.session.call_id
        self.handler.sip_session_terminated(self.session_info, reason, code=code)  # calls cleanup()

    def _NH_SIPSessionGotMessage(self, notification):
        data = notification.data
        if not data.body:
            return
        headers = data.headers
        from_header = headers.get('From', None)
        content_type = headers.get('Content-Type', None)
        if from_header is None or content_type is None:
            return
        body = data.body
        if isinstance(body, bytes) and content_type.content_type != 'message/cpim':
            body = body.decode('utf-8', 'replace')
        sender = str(from_header.uri)
        self.handler.post_backend_event(self.handler.sip_session_message, self.session_info, body, content_type.content_type, sender, from_header.display_name)


class SIPSimpleBackend(SIPBackend):
    name = 'sipsimple'

    def create_account(self, account_info):
        return SIPSimpleAccount(self, account_info)

    def create_outgoing_session(self, account_info, session_info, uri, sdp, headers):
        if not isinstance(account_info.sip_backend, SIPSimpleAccount):
            raise SIPBackendError('the account does not use the sipsimple SIP backend')
        call = SIPSimpleCall(self, session_info, account_info.sip_backend)
        call.call(uri, sdp, headers)
        return call

    @staticmethod
    def incoming_session(session):
        """An INVITE sent to a contact registered by this backend (see the webrtcgateway application)"""
        account = find_contact(session.request_uri)
        if account is None:
            session.reject(480)  # stale contact, the client is gone
        else:
            account.log.info('sipsimple: rejecting incoming call from {}, calls are not implemented yet'.format(session.remote_identity.uri))
            session.reject(480)
