
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

Implemented: outgoing calls (with early media), incoming calls (answer,
decline, cancel/missed), hangup from either side, in-dialog MESSAGE, DTMF
with SIP INFO (needs an SDK with Invitation.send_info), re-INVITEs in both
directions (session-update), including a rejected local re-INVITE, after
which the call goes on with the previous media, and MSRP chat.

MSRP chat: a chat stream offered by the remote party next to audio/video,
in the INVITE or added later with a re-INVITE, is accepted, SylkServer
being the MSRP endpoint (sylk.streams.ChatStream), for text/* and IMDN
only (file transfers over MSRP are standalone sessions, not part of a
call). We never offer one. The
messages received on it go to the client as session messages and are
journaled (see ConnectionHandler.sip_session_chat_message); the client's
session messages go out on it (send_chat_message). A call made of chat
only is rejected.

Re-INVITEs:
  - local (session-update offer from the client): NoSIP converts the new
    offer, the session sends it (Session.update_streams, or add_streams for
    new media lines), and the remote answer goes back through NoSIP to the
    client. When the remote party rejects it, NoSIP gets an answer made of
    the previously negotiated media (new lines rejected), and the client gets
    SessionUpdateEvent(state=failed) with the JSEP answer for that, so its
    PeerConnection stays on the previous media.
  - remote: the offer goes through NoSIP to the client (state received);
    the session waits for the client's session-update answer (up to
    REMOTE_UPDATE_TIMEOUT) before answering the re-INVITE.
"""

import re
import secrets

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from sipsimple.core import (ContactHeader, Credentials, FromHeader, Header, Registration, RouteHeader, SDPConnection, SDPMediaStream,
                            SDPSession, SIPURI, SIPCoreError, ToHeader)
from sipsimple.streams import IMediaStream, UnknownStreamError
from sipsimple.util import ISOTimestamp
from eventlib import api, coros
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import call_in_green_thread
from twisted.internet import reactor
from zope.interface import implementer

from sylk.accounts import DefaultAccount
from sylk.session import Session

from ..janus import JanusBackend, JanusError, NoSIPPluginHandle, _is_pstn_uri
from ..models import janus, sylkrtc
from .base import SIPAccountBackend, SIPBackend, SIPBackendError, SIPCallBackend


__all__ = 'SIPSimpleBackend', 'CONTACT_TOKEN_PARAMETER', 'contact_token', 'find_contact'


CONTACT_APP_PARAMETER = 'x-sylk-app'
CONTACT_TOKEN_PARAMETER = 'x-sylk-gw'

REGISTRATION_DURATION = 600  # seconds, same as the Janus SIP plugin's register_ttl
REMOTE_UPDATE_TIMEOUT = 20   # seconds to wait for the client's answer to a remote re-INVITE
CHAT_WRAPPED_TYPES = ['text/*', 'message/imdn+xml']  # what the chat stream of a call accepts

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


def _aor(uri):
    """user@host of a SIP URI (the form the sylkrtc protocol uses)"""
    return '{}@{}'.format(_text(uri.user), _text(uri.host)) if uri.user else _text(uri.host)


def _has_crypto(media):
    return any(_text(attribute.name) == 'crypto' for attribute in media.attributes)


# The SIP SIMPLE core keeps opus as opus/48000/1 inside: its opus-fix module
# rewrites a received opus/48000/2 rtpmap to /1, and an outgoing /1 to /2. A
# relayed line must therefore be opus/48000/1 when given to the core (else
# pjmedia_sdp_neg matches the local opus/48000/2 against the received, rewritten
# /1, finds no common codec and disables the line), and opus/48000/2 when given
# to NoSIP (what WebRTC and the wire use).
_OPUS_RTPMAP = re.compile(r'^(\d+\s+opus/48000)(?:/\d+)?\s*$', re.IGNORECASE)


def _set_opus_channels(media, channels):
    """Rewrite the opus rtpmap of an audio line (SDPMediaStream, changed in place) to opus/48000/<channels>."""
    if _text(media.media) != 'audio':
        return media
    for attribute in media.attributes:
        if _text(attribute.name) == 'rtpmap':
            match = _OPUS_RTPMAP.match(_text(attribute.value))
            if match is not None:
                attribute.value = '{}/{}'.format(match.group(1), channels).encode()
    return media


def _nosip_sdp(sdp):
    """The text of an SDP (SDPSession) for NoSIP: a copy with opus/48000/2."""
    sdp = SDPSession.new(sdp)
    sdp.media = [_set_opus_channels(SDPMediaStream.new(media), 2) for media in sdp.media]
    return str(sdp)


def _plain_rtp_lines(sdp, indexes):
    """
    Of the given media lines of sdp (in use, port != 0), the ones without
    SDES (a=crypto), when others do use SDES; empty when they agree.

    The Janus NoSIP plugin (1.1.2, and current master) keeps one remote SRTP
    flag for the whole call: when the remote party uses SDES on one line and
    plain RTP on another (e.g. SRTP audio, RTP video), NoSIP decrypts every
    RTP packet with a per media context it never created and Janus crashes
    (SIGSEGV) on the first packet of the plain RTP line, taking down every
    WebRTC session on the server. Such SDPs must never reach NoSIP.
    """
    lines = [index for index in indexes if index < len(sdp.media) and sdp.media[index].port != 0]
    with_crypto = [index for index in lines if _has_crypto(sdp.media[index])]
    if not with_crypto or len(with_crypto) == len(lines):
        return []
    return [index for index in lines if index not in with_crypto]


# WebRTC attributes that mean nothing to a SIP endpoint. NoSIP's SDP is the
# browser offer with only the ICE/DTLS layer removed, so it still carries
# them all: a video line has 80+ attributes and the SDP is 3+ KB, which
# overflows UDP and pjmedia's attribute limit, and confuses simple phones.
#   extmap, extmap-allow-mixed     RTP header extensions (browser specific)
#   mid, msid, msid-semantic, group  BUNDLE / MediaStream ids, no BUNDLE on SIP
#   ssrc, ssrc-group, rtcp-rsize   WebRTC transport details
_WEBRTC_ONLY_ATTRIBUTES = {'extmap', 'extmap-allow-mixed', 'mid', 'msid', 'msid-semantic', 'group',
                           'ssrc', 'ssrc-group', 'rtcp-rsize'}
# RTCP feedback a SIP peer can take part in; goog-remb and transport-cc need
# browser congestion control feedback that SIP endpoints never send
_SIP_RTCP_FEEDBACK = {'nack', 'nack pli', 'ccm fir'}

# pjmedia keeps at most PJMEDIA_MAX_SDP_ATTR (2 * PJMEDIA_MAX_SDP_FMT + 4 = 68)
# attributes per media line when parsing an SDP and silently drops the rest,
# a=crypto included when it comes last. Trimmed SDPs stay far below that;
# _fit_sdp_attributes is only a safety net.
MAX_SDP_MEDIA_ATTRIBUTES = 68

# attributes that must survive the safety net, in this order; the rest keep their order
_ESSENTIAL_ATTRIBUTES = ('crypto', 'sendrecv', 'sendonly', 'recvonly', 'inactive', 'rtpmap', 'fmtp',
                         'rtcp', 'rtcp-mux', 'ptime', 'maxptime')
# what the safety net drops first when a media line has too many attributes
_EXPENDABLE_ATTRIBUTES = ('rtcp-fb',)


def _sdp_lines(sdp):
    if isinstance(sdp, bytes):
        sdp = sdp.decode()
    newline = '\r\n' if '\r\n' in sdp else '\n'
    lines = sdp.split(newline)
    trailing = bool(lines) and lines[-1] == ''
    if trailing:
        lines = lines[:-1]
    return lines, newline, trailing


def _sdp_text(lines, newline, trailing):
    return newline.join(lines) + (newline if trailing else '')


def _attribute_name(line):
    return line[2:].split(':', 1)[0].split(' ', 1)[0]


def _trim_sdp_for_sip(sdp):
    """
    The NoSIP SDP without the WebRTC only attributes (see
    _WEBRTC_ONLY_ATTRIBUTES), and with only the RTCP feedback types a SIP
    endpoint can use. Codecs, crypto, direction and the rest are kept.
    """
    lines, newline, trailing = _sdp_lines(sdp)
    output = []
    for line in lines:
        if line.startswith('a='):
            name = _attribute_name(line)
            if name in _WEBRTC_ONLY_ATTRIBUTES:
                continue
            if name == 'rtcp-fb':
                # a=rtcp-fb:<pt> <type> [<parameter>]
                feedback = line.split(':', 1)[1].split(' ', 1)[1] if ' ' in line else ''
                if feedback.strip() not in _SIP_RTCP_FEEDBACK:
                    continue
        output.append(line)
    return _sdp_text(output, newline, trailing)


def _fit_sdp_attributes(sdp):
    """
    The SDP text with every media line reduced to MAX_SDP_MEDIA_ATTRIBUTES
    attributes, so that pjmedia keeps all the important ones, and a list of
    (media type, number of dropped attributes) for the lines that were reduced.
    """
    lines, newline, trailing = _sdp_lines(sdp)
    sections = [[]]
    for line in lines:
        if line.startswith('m='):
            sections.append([])
        sections[-1].append(line)
    output = list(sections[0])
    reduced = []
    for section in sections[1:]:
        attributes = [line for line in section if line.startswith('a=')]
        if len(attributes) <= MAX_SDP_MEDIA_ATTRIBUTES:
            output.extend(section)
            continue
        keep_count = MAX_SDP_MEDIA_ATTRIBUTES
        essential = [line for line in attributes if _attribute_name(line) in _ESSENTIAL_ATTRIBUTES][:keep_count]
        others = [line for line in attributes if _attribute_name(line) not in _ESSENTIAL_ATTRIBUTES]
        # drop the most expendable kinds first, from the end of the line
        for kind in reversed(_EXPENDABLE_ATTRIBUTES):
            while len(essential) + len(others) > keep_count and any(_attribute_name(line) == kind for line in others):
                index = max(i for i, line in enumerate(others) if _attribute_name(line) == kind)
                del others[index]
        others = others[:max(keep_count - len(essential), 0)]
        kept = set(id(line) for line in essential + others)
        output.extend(line for line in section if not line.startswith('a=') or id(line) in kept)
        reduced.append((section[0][2:].split(' ', 1)[0], len(attributes) - len(essential) - len(others)))
    return _sdp_text(output, newline, trailing), reduced


def _forwarded_headers(headers, prefixes):
    # The INVITE headers whose name starts with one of the account's
    # incoming_header_prefixes (default X-), as {name: value}, like the
    # Janus SIP plugin gives them in the incoming call event.
    prefixes = tuple(prefix.lower() for prefix in prefixes or ())
    forwarded = {}
    for name, header in (headers or {}).items():
        if not prefixes or not name.lower().startswith(prefixes):
            continue
        header = header[0] if isinstance(header, (list, tuple)) and header else header
        value = getattr(header, 'body', header)
        if value is not None:
            forwarded[name] = _text(value) if isinstance(value, bytes) else str(value)
    return forwarded


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
        return self.call.local_media(self, remote_sdp)

    def start(self, local_sdp, remote_sdp, stream_index):
        self.call.stream_started(self, remote_sdp)

    def validate_update(self, remote_sdp, stream_index):
        return True

    def update(self, local_sdp, remote_sdp, stream_index):
        self.call.stream_updated(self, remote_sdp)

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
        self.direction = 'outgoing'
        self._announced = session_info is not None  # the handler knows about the session
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
        self._negotiation = 'initial'   # what the awaited answer is for: the 'initial' offer or an 'update'
        self._answered_offer = None     # the remote offer (as text) the current local SDP answers
        self._remote_update = None      # event set when the client answered a remote re-INVITE
        self._pending_proposal = None   # streams of a remote re-INVITE adding media, until the client answers
        self._proposal_timer = None
        self._local_update_pending = False
        self._previous_local_sdp = None
        self._added_streams = []        # streams of a local update that adds media lines
        self._chat_offer = None         # the chat stream of an incoming INVITE, accepted with the call
        self.chat_stream = None         # the MSRP chat stream, once started
        self._chat_streams = set()      # chat streams observed
        self._chat_messages = {}        # MSRP message id -> client message id, of the messages sent

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

    # Incoming call

    def setup_incoming(self, session):
        # Called in the reactor thread, as soon as the INVITE arrived, so that
        # a CANCEL is not missed while the offer is converted.
        self.session = session
        self.direction = 'incoming'
        NotificationCenter().add_observer(self, sender=session)

    def start_incoming(self):
        # should only be called from a green thread (the operations queue)
        session = self.session
        if self._ended or session.state != 'incoming':
            return
        remote_sdp = session._invitation.sdp.proposed_remote
        relayed_media = []  # (index in the remote SDP, media type)
        for index, media in enumerate(remote_sdp.media):
            media_type = _text(media.media)
            if media.port != 0 and media_type in ('audio', 'video'):
                relayed_media.append((index, media_type))
        plain_lines = _plain_rtp_lines(remote_sdp, [index for index, media_type in relayed_media])
        if plain_lines:
            # mixed SRTP and RTP, which NoSIP cannot handle: only the SRTP lines are used
            self.log.warning('sipsimple: incoming call from {} offers {} without SRTP next to SRTP media, rejecting {}'.format(
                session.remote_identity.uri, ', '.join(_text(remote_sdp.media[index].media) for index in plain_lines), 'it' if len(plain_lines) == 1 else 'them'))
            relayed_media = [(index, media_type) for index, media_type in relayed_media if index not in plain_lines]
        if not relayed_media:
            self.log.info('sipsimple: rejecting incoming call from {}, it has no audio or video'.format(session.remote_identity.uri))
            call_in_green_thread(session.reject, 488)
            return

        # NoSIP only gets the audio and video lines; their order is kept, so
        # the n-th line of its answer belongs to the n-th relayed stream
        offer = SDPSession.new(remote_sdp)
        offer.media = [SDPMediaStream.new(remote_sdp.media[index]) for index, media_type in relayed_media]
        self.streams = []
        for position, (index, media_type) in enumerate(relayed_media):
            stream = RelayedRTPStream(self, media_type, position)
            stream.index = index
            self.streams.append(stream)

        try:
            self.janus_handle = NoSIPPluginHandle(self.handler.janus_session, event_handler=self._janus_event_handler)
            result, jsep = self.janus_handle.process(_nosip_sdp(offer), 'offer')
        except JanusError as e:
            self.log.warning('sipsimple: Janus could not use the offer of {}: {}'.format(session.remote_identity.uri, e))
            self._end(detach=True)
            call_in_green_thread(session.reject, 488)
            return
        if self._ended or session.state != 'incoming':  # cancelled meanwhile
            self._end(detach=True)
            return

        # only the relayed streams and one chat stream will be accepted (the rest get port 0)
        chat_streams = [stream for stream in session.proposed_streams if stream.type == 'chat'][:1]
        self._chat_offer = chat_streams[0] if chat_streams else None
        session.proposed_streams = list(self.streams) + chat_streams

        account_info = self.account.account_info
        call_id = session.call_id
        remote_uri = session.remote_identity.uri
        originator = sylkrtc.SIPIdentity(uri=_aor(remote_uri), display_name=_text(session.remote_identity.display_name) or None)

        from ..handler import SIPSessionInfo
        session_info = SIPSessionInfo(self.handler._callid_to_uuid(call_id))
        session_info.janus_handle = self.janus_handle
        session_info.backend = self
        session_info.init_incoming(account_info, originator.uri, originator.display_name)
        session_info.call_id = call_id
        session_info.update_media(jsep.sdp, remote=True)
        self.session_info = session_info
        self._announced = True

        try:
            session.send_ring_indication()  # the Janus SIP plugin answers 180 right away as well
        except Exception:
            pass
        headers = _forwarded_headers(getattr(session, 'request_headers', None), account_info.incoming_header_prefixes)
        self.log.info('sipsimple: incoming call from {} with {}'.format(remote_uri, ', '.join(stream.type for stream in self.streams + chat_streams)))
        self.handler.sip_account_incoming_session(account_info, session_info, originator, sdp=jsep.sdp, call_id=call_id, headers=headers or None)

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
        # the SDP sent on the SIP side: NoSIP's, without the WebRTC only parts
        sdp, reduced = _fit_sdp_attributes(_trim_sdp_for_sip(sdp))
        for media_type, count in reduced:
            self.log.info('sipsimple: dropped {} {} SDP attributes above the pjmedia limit of {}'.format(count, media_type, MAX_SDP_MEDIA_ATTRIBUTES))
        self.local_sdp = SDPSession.parse(sdp.encode())

    def _filtered_sdp(self, sdp, streams, disabled_lines=()):
        # The lines of sdp (a remote SDP) for the given streams, in the order of
        # the NoSIP SDP (sdp_index), which is the form NoSIP needs; a stream
        # without a line in sdp gets its local line with port 0, as do the
        # lines (indexes in sdp) in disabled_lines
        filtered = SDPSession.new(sdp)
        media = []
        for stream in sorted(streams, key=lambda item: item.sdp_index):
            if stream.index is not None and stream.index < len(sdp.media) and stream.index in disabled_lines:
                line = SDPMediaStream.new(sdp.media[stream.index])
                line.port = 0
                media.append(line)
            elif stream.index is not None and stream.index < len(sdp.media):
                media.append(SDPMediaStream.new(sdp.media[stream.index]))
            else:
                line = SDPMediaStream.new(self.local_sdp.media[stream.sdp_index])
                line.port = 0
                media.append(line)
        filtered.media = media
        return filtered

    def local_media(self, stream, remote_sdp=None):
        session = self.session
        if remote_sdp is not None and session is not None and session.state == 'received_proposal' and str(remote_sdp) != self._answered_offer:
            # a re-INVITE from the remote party: the client has to answer it first
            self._negotiate_remote_offer(remote_sdp)
        if stream.sdp_index >= len(self.local_sdp.media):
            # NoSIP's SDP has no line for this stream (should not happen): disable it
            if remote_sdp is None or stream.index is None or stream.index >= len(remote_sdp.media):
                raise SIPBackendError('no media line for the {} stream'.format(stream.type))
            media = SDPMediaStream.new(remote_sdp.media[stream.index])
            media.port = 0
            media.attributes = []
            return media
        media = SDPMediaStream.new(self.local_sdp.media[stream.sdp_index])
        if media.connection is None and self.local_sdp.connection is not None:
            # NoSIP puts the Janus address in the session level c= line, but
            # only the media lines are copied to the session's SDP
            media.connection = SDPConnection.new(self.local_sdp.connection)
        return _set_opus_channels(media, 1)     # the core's own form (see _set_opus_channels)

    def stream_started(self, stream, remote_sdp):
        # called by Session (in its green thread) for every stream the remote party accepted
        self._remote_sdp(stream, remote_sdp, started=True)

    def stream_updated(self, stream, remote_sdp):
        # called by Session for every stream once a re-INVITE completed
        self._remote_sdp(stream, remote_sdp, started=False)

    def _remote_sdp(self, stream, remote_sdp, started):
        if not self._awaiting_answer:
            if started:
                NotificationCenter().post_notification('MediaStreamDidStart', sender=stream)
            return
        self._answer_waiters.append((stream, started))
        if not self._processing_answer:
            self._processing_answer = True
            call_in_green_thread(self._process_answer, remote_sdp)

    def _process_answer(self, remote_sdp):
        notification_center = NotificationCenter()
        negotiation = self._negotiation
        plain_lines = []
        try:
            streams = self.streams + self._added_streams
            plain_lines = _plain_rtp_lines(remote_sdp, [stream.index for stream in streams if stream.index is not None])
            if plain_lines:
                # The remote party would send plain RTP on these lines, which crashes
                # NoSIP; the call cannot be relayed (see _plain_rtp_lines)
                raise JanusError(488, 'the answer mixes SRTP and plain RTP ({} without SRTP), which Janus NoSIP cannot relay'.format(
                    ', '.join(_text(remote_sdp.media[index].media) for index in plain_lines)))
            answer = self._filtered_sdp(remote_sdp, streams)
            result, jsep = self.janus_handle.process(_nosip_sdp(answer), 'answer')
        except JanusError as e:
            waiters, self._answer_waiters = self._answer_waiters, []
            self.log.warning('sipsimple: Janus could not use the answer of {}: {}'.format(self.session.remote_identity.uri, e))
            if negotiation == 'update':
                self._awaiting_answer = False
                if plain_lines:
                    # The remote party accepted the update on the SIP side and will
                    # send plain RTP that NoSIP cannot take: end the call right away.
                    self.log.warning('sipsimple: ending session {}, {}'.format(self.session_info.id, e.reason))
                    call_in_green_thread(self.session.end)
                else:
                    self.handler.post_backend_event(self._local_update_failed, 'Janus could not use the answer: {}'.format(e))
            for stream, started in waiters:
                if started:
                    notification_center.post_notification('MediaStreamDidFail', sender=stream, data=NotificationData(context='starting', reason='Janus: {}'.format(e)))
        else:
            waiters, self._answer_waiters = self._answer_waiters, []
            self._awaiting_answer = False
            sdp = jsep.sdp if jsep is not None else None
            if negotiation == 'update':
                self.handler.post_backend_event(self._local_update_answered, sdp)
            else:
                early = getattr(self.session._invitation, 'state', None) == 'early'
                self.handler.post_backend_event(self._remote_answer, sdp, early)
            for stream, started in waiters:
                if started:
                    notification_center.post_notification('MediaStreamDidStart', sender=stream)
        finally:
            self._processing_answer = False

    # Remote re-INVITE

    def _negotiate_remote_offer(self, remote_sdp):
        # Runs in the session's green thread while it builds the answer to a
        # re-INVITE: blocks until the client answered (or the timeout).
        self._answered_offer = str(remote_sdp)
        plain_lines = _plain_rtp_lines(remote_sdp, [stream.index for stream in self.streams if stream.index is not None])
        if plain_lines:
            self.log.warning('sipsimple: re-INVITE of {} mixes SRTP and plain RTP, rejecting the plain RTP lines'.format(self.session.remote_identity.uri))
        offer = self._filtered_sdp(remote_sdp, self.streams, disabled_lines=plain_lines)
        event = self._remote_update = coros.event()
        try:
            result, jsep = self.janus_handle.process(_nosip_sdp(offer), 'offer')
        except (JanusError, AttributeError) as e:
            self._remote_update = None
            self.log.warning('sipsimple: Janus could not use the re-INVITE offer of {}: {}, keeping the current media'.format(self.session.remote_identity.uri, e))
            return
        self.handler.post_backend_event(self._remote_offer_received, jsep.sdp)
        try:
            with api.timeout(REMOTE_UPDATE_TIMEOUT):
                event.wait()
        except api.TimeoutError:
            self.log.warning('sipsimple: the client did not answer the re-INVITE of {} in {}s, keeping the current media'.format(self.session.remote_identity.uri, REMOTE_UPDATE_TIMEOUT))
            self.handler.post_backend_event(self._remote_update_abandoned, 'no answer from the client')
        finally:
            self._remote_update = None

    def _remote_offer_received(self, sdp):
        if not self._ended:
            self.handler.sip_session_update_received(self.session_info, sdp)

    def _remote_proposal(self, proposed_streams):
        # A re-INVITE from the remote party that adds media lines
        session = self.session
        if self._ended or session.state != 'received_proposal':
            return
        new_streams = []
        for proposed in proposed_streams:
            if proposed.type in ('audio', 'video'):
                stream = RelayedRTPStream(self, proposed.type, len(self.streams) + len(new_streams))
                stream.index = proposed.index
                new_streams.append(stream)
        has_chat = self.chat_stream is not None and self.chat_stream in session.streams
        chat_streams = [] if has_chat else [stream for stream in proposed_streams if stream.type == 'chat'][:1]
        if not new_streams and not chat_streams:
            self.log.info('sipsimple: rejecting re-INVITE of {} adding {}, not supported'.format(session.remote_identity.uri, ', '.join(stream.type for stream in proposed_streams)))
            call_in_green_thread(session.reject_proposal, 488)
            return
        session.proposed_streams = new_streams + chat_streams  # only these are accepted
        if not new_streams:
            # only a chat stream is added: the media of the call does not change,
            # so there is nothing for the client to answer
            self.log.info('sipsimple: accepting chat stream added by {}'.format(session.remote_identity.uri))
            self._observe_chat(chat_streams[0])
            call_in_green_thread(session.accept_proposal, chat_streams)
            return
        remote_sdp = session._invitation.sdp.proposed_remote
        self._answered_offer = str(remote_sdp)
        try:
            plain_lines = _plain_rtp_lines(remote_sdp, [stream.index for stream in self.streams + new_streams])
            if plain_lines:
                self.log.warning('sipsimple: re-INVITE of {} mixes SRTP and plain RTP, rejecting the plain RTP lines'.format(session.remote_identity.uri))
            offer = self._filtered_sdp(remote_sdp, self.streams + new_streams, disabled_lines=plain_lines)
            result, jsep = self.janus_handle.process(_nosip_sdp(offer), 'offer')
        except JanusError as e:
            self.log.warning('sipsimple: Janus could not use the re-INVITE offer of {}: {}'.format(session.remote_identity.uri, e))
            call_in_green_thread(session.reject_proposal, 488)
            return
        self._pending_proposal = new_streams + chat_streams
        self._proposal_timer = reactor.callLater(REMOTE_UPDATE_TIMEOUT, self._remote_proposal_timeout)
        self.handler.sip_session_update_received(self.session_info, jsep.sdp)

    def _remote_proposal_timeout(self):
        self._proposal_timer = None
        if self._pending_proposal is not None and self.session.state == 'received_proposal':
            self._pending_proposal = None
            self.log.warning('sipsimple: the client did not answer the re-INVITE of {} in {}s, rejecting it'.format(self.session.remote_identity.uri, REMOTE_UPDATE_TIMEOUT))
            call_in_green_thread(self.session.reject_proposal, 488)
            self.handler.post_backend_event(self._remote_update_abandoned, 'no answer from the client')

    def _remote_update_abandoned(self, reason):
        # the client got a remote offer (update state received) that will not be used
        if not self._ended and self.session_info.state == 'remote-updating':
            self.handler.sip_session_update_failed(self.session_info, reason)

    # Local re-INVITE

    def _local_update_answered(self, sdp):
        if self._ended:
            return
        self._local_update_pending = False
        self._previous_local_sdp = None
        self.handler.sip_session_accepted(self.session_info, sdp=sdp)  # local-updating -> established, update accepted

    def _local_update_failed(self, reason):
        if self._ended or not self._local_update_pending:
            return
        self._local_update_pending = False
        self._awaiting_answer = False
        self._negotiation = 'initial'
        rejected_sdp = self.local_sdp
        added_streams, self._added_streams = self._added_streams, []
        if self._previous_local_sdp is not None:
            self.local_sdp, self._previous_local_sdp = self._previous_local_sdp, None
        # Close the client's pending negotiation with an answer that keeps the
        # previously negotiated media: the active remote SDP for the existing
        # lines, the new lines rejected (port 0).
        sdp = None
        try:
            active_remote = self.session._invitation.sdp.active_remote
            answer = SDPSession.new(active_remote)
            lines = []
            for index, line in enumerate(rejected_sdp.media):
                stream = next((item for item in self.streams if item.sdp_index == index), None)
                if stream is not None and stream.index is not None and stream.index < len(active_remote.media):
                    lines.append(SDPMediaStream.new(active_remote.media[stream.index]))
                else:
                    line = SDPMediaStream.new(line)
                    line.port = 0
                    lines.append(line)
            answer.media = lines
            result, jsep = self.janus_handle.process(_nosip_sdp(answer), 'answer')
            sdp = jsep.sdp if jsep is not None else None
        except Exception as e:
            self.log.warning('sipsimple: could not restore the previous media after the failed update: {}'.format(e))
        self.log.info('sipsimple: update of session {} failed ({}){}'.format(self.session_info.id, reason, ', removed {}'.format(', '.join(stream.type for stream in added_streams)) if added_streams else ''))
        self.handler.sip_session_update_failed(self.session_info, reason, sdp=sdp)

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
        if self.direction != 'incoming' or self.session is None or self.session.state != 'incoming':
            raise SIPBackendError('the session cannot be answered')
        try:
            result = self.janus_handle.generate(sdp, 'answer')
        except JanusError as e:
            raise SIPBackendError('Janus could not use the answer: {}'.format(e))
        self._set_local_sdp(result.sdp)
        self._answered_offer = str(self.session._invitation.sdp.proposed_remote)
        streams = list(self.streams)
        if self._chat_offer is not None:
            self._observe_chat(self._chat_offer)
            streams.append(self._chat_offer)
            self._chat_offer = None
        self.session.accept(streams, extra_headers=self._extra_headers(headers))

    def decline(self, code):
        if self.session is not None and self.session.state == 'incoming':
            self.session.reject(code)

    def hangup(self):
        if self.session is not None:
            call_in_green_thread(self.session.end)

    def update(self, sdp, jsep_type, headers=None):
        if self.janus_handle is None or self.session is None:
            raise SIPBackendError('the session ended')
        if jsep_type == 'answer':
            self._answer_remote_update(sdp)
        else:
            self._send_local_update(sdp, headers)

    def _answer_remote_update(self, sdp):
        # the client's answer to a re-INVITE from the remote party
        event, proposal = self._remote_update, self._pending_proposal
        try:
            result = self.janus_handle.generate(sdp, 'answer')
        except JanusError as e:
            if event is not None:
                event.send(False)
            if proposal is not None:
                self._pending_proposal = None
                call_in_green_thread(self.session.reject_proposal, 488)
            raise SIPBackendError('Janus could not use the answer: {}'.format(e))
        self._set_local_sdp(result.sdp)
        if event is not None:
            event.send(True)  # the session answers the re-INVITE with the new local SDP
        elif proposal is not None:
            self._pending_proposal = None
            if self._proposal_timer is not None and self._proposal_timer.active():
                self._proposal_timer.cancel()
            self._proposal_timer = None
            for stream in proposal:
                if stream.type == 'chat':
                    self._observe_chat(stream)
            self.session.accept_proposal(proposal)
        self.handler.post_backend_event(self.handler.sip_session_updated, self.session_info)

    def _uses_srtp(self):
        try:
            active_remote = self.session._invitation.sdp.active_remote
        except AttributeError:
            return False
        return any(_has_crypto(active_remote.media[stream.index]) for stream in self.streams
                   if stream.index is not None and stream.index < len(active_remote.media) and active_remote.media[stream.index].port != 0)

    def _send_local_update(self, sdp, headers):
        if self.session.state != 'connected':
            raise SIPBackendError('the session cannot be updated now ({})'.format(self.session.state))
        try:
            # Offer SRTP only the way the call already uses it: SDES required if
            # it uses SRTP, none if not, so that the remote party cannot answer
            # new media lines with a mix of SRTP and RTP (see _plain_rtp_lines)
            result = self.janus_handle.generate(sdp, 'offer', srtp='sdes_mandatory' if self._uses_srtp() else None)
        except JanusError as e:
            raise SIPBackendError('Janus could not use the offer: {}'.format(e))
        self._previous_local_sdp = self.local_sdp
        self._set_local_sdp(result.sdp)
        count = len(self.streams)
        self._added_streams = [RelayedRTPStream(self, _text(media.media), index) for index, media in enumerate(self.local_sdp.media)
                               if index >= count and media.port != 0 and _text(media.media) in ('audio', 'video')]
        self._negotiation = 'update'
        self._awaiting_answer = True
        self._local_update_pending = True
        try:
            if self._added_streams:
                self.session.add_streams(self._added_streams)
            else:
                self.session.update_streams(extra_headers=self._extra_headers(headers))
        except Exception as e:
            self._awaiting_answer = False
            self._local_update_pending = False
            self._added_streams = []
            self.local_sdp, self._previous_local_sdp = self._previous_local_sdp, None
            raise SIPBackendError('could not send the update: {}'.format(e))
        self.log.info('sipsimple: sending re-INVITE for session {}{}'.format(self.session_info.id, ', adding {}'.format(', '.join(stream.type for stream in self._added_streams)) if self._added_streams else ''))

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
        # the same application/dtmf-relay body the Janus SIP plugin sends
        body = 'Signal={}\r\nDuration={}\r\n'.format(digit, int(duration) if duration else 160)
        try:
            self.session.send_info('application/dtmf-relay', body)
        except (SIPCoreError, AttributeError) as e:
            raise SIPBackendError('could not send DTMF with SIP INFO: {}'.format(e))

    # MSRP chat

    @property
    def chat_active(self):
        return self.chat_stream is not None and not self._ended

    def send_chat_message(self, content, content_type, message_id, timestamp=None, additional_headers=None):
        stream = self.chat_stream
        if stream is None or self._ended:
            raise SIPBackendError('the session has no chat stream')
        try:
            timestamp = ISOTimestamp(timestamp) if timestamp else None
        except (TypeError, ValueError):
            timestamp = None
        # the MSRP message id is made by the stream (the client's may not be a valid one)
        msrp_message_id = stream.send_message(content, content_type=content_type, timestamp=timestamp, additional_headers=additional_headers)
        self._chat_messages[msrp_message_id] = message_id

    def _observe_chat(self, stream):
        # Only text and delivery notifications in a call: file transfers over
        # MSRP are sessions of their own. Set before the stream builds its SDP
        # (accept-wrapped-types); anything else gets a 415 report from the stream.
        stream.accept_wrapped_types = CHAT_WRAPPED_TYPES
        if stream not in self._chat_streams:
            self._chat_streams.add(stream)
            NotificationCenter().add_observer(self, sender=stream)

    def _forget_chat(self, stream):
        if stream in self._chat_streams:
            self._chat_streams.discard(stream)
            NotificationCenter().discard_observer(self, sender=stream)
        if stream is self.chat_stream:
            self.chat_stream = None

    def _chat_message_received(self, message, msrp_message_id):
        if not self._ended and self.session_info is not None:
            self.handler.sip_session_chat_message(self.session_info, message, msrp_message_id)

    def _chat_message_delivery(self, msrp_message_id, code, reason):
        message_id = self._chat_messages.pop(msrp_message_id, None)
        if message_id is not None and self.session_info is not None:
            self.handler.sip_session_chat_message_delivery(self.session_info, message_id, code, reason)

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
        for stream in list(self._chat_streams):
            self._forget_chat(stream)
        self._chat_offer = None
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
        if self._ended or self.session_info is None or not isinstance(event, janus.CoreEvent):
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
        if self.direction != 'outgoing':
            return
        self.session_info.call_id = self.session.call_id
        self.handler.post_backend_event(self.handler.sip_session_proceeding, self.session_info, notification.data.code)

    def _NH_SIPSessionGotRingIndication(self, notification):
        if self.direction != 'outgoing':
            return
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
        if not self._announced:  # an incoming call that ended before the client was told about it
            self.handler.post_backend_event(self._end, True)
            return
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

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            self.handler.post_backend_event(self._remote_proposal, list(notification.data.proposed_streams))

    def _NH_SIPSessionProposalAccepted(self, notification):
        accepted = [stream for stream in notification.data.accepted_streams if isinstance(stream, RelayedRTPStream) and stream not in self.streams]
        self.streams.extend(accepted)
        self._added_streams = [stream for stream in self._added_streams if stream not in accepted]

    def _NH_SIPSessionProposalRejected(self, notification):
        if notification.data.originator == 'local':
            self.handler.post_backend_event(self._local_update_failed, '{} {}'.format(notification.data.code, _text(notification.data.reason)))

    def _NH_SIPSessionHadProposalFailure(self, notification):
        if notification.data.originator == 'local':
            self.handler.post_backend_event(self._local_update_failed, _text(notification.data.failure_reason) or 'update failed')

    def _NH_SIPSessionDidNotUpdateStreams(self, notification):
        data = notification.data
        reason = '{} {}'.format(data.code, _text(data.reason)) if data.code else _text(data.reason)
        self.handler.post_backend_event(self._local_update_failed, reason)

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

    # Chat stream notifications

    def _NH_MediaStreamDidStart(self, notification):
        stream = notification.sender
        if stream in self._chat_streams and not self._ended:
            self.chat_stream = stream
            self.log.info('sipsimple: chat with {} started for session {}'.format(self.session.remote_identity.uri, self.session_info.id if self.session_info else '-'))

    def _NH_MediaStreamDidNotInitialize(self, notification):
        self._chat_stream_gone(notification.sender, _text(getattr(notification.data, 'reason', None)) or 'not initialized')

    def _NH_MediaStreamDidFail(self, notification):
        self._chat_stream_gone(notification.sender, _text(getattr(notification.data, 'reason', None)) or 'failed')

    def _NH_MediaStreamDidEnd(self, notification):
        self._chat_stream_gone(notification.sender, getattr(notification.data, 'error', None) or 'ended')

    def _chat_stream_gone(self, stream, reason):
        if stream not in self._chat_streams:
            return
        if stream is self.chat_stream:
            self.log.info('sipsimple: chat with {} ended ({})'.format(self.session.remote_identity.uri, reason))
        self._forget_chat(stream)

    def _NH_ChatStreamGotMessage(self, notification):
        data = notification.data
        try:
            notification.sender.msrp_session.send_report(data.chunk, 200, 'OK')
        except Exception as e:
            self.log.warning('sipsimple: could not send MSRP report: {}'.format(e))
        self.handler.post_backend_event(self._chat_message_received, data.message, data.chunk.message_id)

    def _NH_ChatStreamGotComposingIndication(self, notification):
        # not relayed to the client
        try:
            notification.sender.msrp_session.send_report(notification.data.chunk, 200, 'OK')
        except Exception:
            pass

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        data = notification.data
        self.handler.post_backend_event(self._chat_message_delivery, data.message_id, data.code, _text(data.reason))

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        data = notification.data
        self.handler.post_backend_event(self._chat_message_delivery, data.message_id, data.code, _text(data.reason))


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
        if account is None or account.handler.state != 'started':
            session.reject(480)  # stale contact, the client is gone
            return
        call = SIPSimpleCall(account.backend, None, account)
        call.setup_incoming(session)
        account.handler.post_backend_event(call.start_incoming)
