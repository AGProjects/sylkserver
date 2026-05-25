
import base64
import hashlib
import json
import urllib.parse
import re
import os
import random
import time
import uuid
from collections import deque
from itertools import count
from shutil import copyfileobj, rmtree
from typing import (Container, Dict, Generic, Iterable, Optional, Set, Sized,
                    TypeVar, Union)

from application.notification import (IObserver, NotificationCenter,
                                      NotificationData)
from application.python import Null, limit
from application.python.weakref import defaultweakobjectmap
from application.system import makedirs, unlink
from eventlib import api, coros, proc
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import (SIPURI, ContactHeader, Credentials, Engine,
                            FromHeader, Header, Message, Referral,
                            ReferToHeader, Route, RouteHeader, SIPCoreError,
                            ToHeader, sipfrag_re)
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.imdn import (DeliveryNotification, DisplayNotification,
                                     IMDNDocument)
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.msrp.chat import (ChatIdentity, CPIMHeader,
                                         CPIMNamespace, CPIMParserError,
                                         CPIMPayload)
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import call_in_green_thread, run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import defer, reactor
from twisted.web.client import Agent
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer
from werkzeug.exceptions import InternalServerError
from zope.interface import implementer

from sylk.accounts import DefaultAccount
from sylk.configuration import SIPConfig
from sylk.session import Session

from . import push
from .addressbook import get_addressbook, update_addressbook
from .auth import AuthHandler
from .configuration import (ExternalAuthConfig, GeneralConfig, JanusConfig,
                            get_room_config)
from .janus import (JanusBackend, JanusError, JanusSession, SIPPluginHandle,
                    VideoroomPluginHandle)
from .logger import ConnectionLogger, VideoroomLogger
from .models import janus, sylkrtc
from .storage import MessageStorage, TokenStorage


_SIP_URI_RE = re.compile(r'^sips?:[^\s@]+@[^\s@]+$')


def _parse_external_publisher_display(display, janus_id):
    """
    Parse the Janus 'display' field of an external publisher (e.g.
    sip-janus-bridge).  The bridge encodes it as "<name>\\t<sip_uri>" so
    we can surface both pieces to WebRTC clients.

    Returns (display_name, sip_uri).  If parsing fails (display is empty
    or the URI half is not a valid SIP URI), falls back to a synthetic
    URI under the reserved 'janus.invalid' TLD so the sylkrtc
    AORValidator still accepts the entry.
    """
    placeholder_uri = 'sip:bridge-{0}@janus.invalid'.format(janus_id)
    placeholder_name = 'bridge-{0}'.format(janus_id)
    if not display:
        return placeholder_name, placeholder_uri
    if '\t' in display:
        name, _, uri = display.partition('\t')
        name = name.strip()
        uri = uri.strip()
        if uri and _SIP_URI_RE.match(uri):
            return (name or placeholder_name), uri
        # URI half malformed — fall through to other heuristics.
    # Display is itself a valid SIP URI (older bridges, or sip:user@host
    # as the literal display field).
    if _SIP_URI_RE.match(display.strip()):
        return placeholder_name, display.strip()
    # Plain display name, no URI piece.
    return display, placeholder_uri


class AccountInfo(object):
    # noinspection PyShadowingBuiltins
    def __init__(self, id, password, display_name=None, user_agent=None, incoming_header_prefixes=None):
        self.id = id
        self.password = password
        self.display_name = display_name
        self.user_agent = user_agent
        self.registration_state = None
        self.janus_handle = None  # type: Optional[SIPPluginHandle]
        self.janus_helpers = []  # type: List[SIPPluginHandle]
        self.contact_params = {}
        # Default to forwarding ALL X-* headers from incoming INVITEs into
        # the webrtcgateway incoming-session WebSocket event so any custom
        # capability advertisement (X-Sylk-ZRTP, X-Sylk-App, future X-
        # vendor headers) reaches the client by default. Clients can still
        # narrow or override the prefix list explicitly via the
        # 'incoming_header_prefixes' field on account-add.
        if incoming_header_prefixes is None:
            self.incoming_header_prefixes = ['X-']
        else:
            self.incoming_header_prefixes = incoming_header_prefixes.__data__
        self.auth_handle = None
        self.auth_state = False

    @property
    def uri(self):
        return 'sip:' + self.id

    @property
    def user_data(self):
        return dict(username=self.uri,
                    display_name=self.display_name,
                    user_agent=self.user_agent,
                    ha1_secret=self.password,
                    contact_params=self.contact_params,
                    incoming_header_prefixes=self.incoming_header_prefixes)


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
        self.account = None            # type: Optional[AccountInfo]
        self.local_identity = None     # type: Optional[SessionPartyIdentity]
        self.remote_identity = None    # type: Optional[SessionPartyIdentity]
        self.janus_handle = None       # type: Optional[SIPPluginHandle]
        self.slow_download = False
        self.slow_upload = False
        self._message_queue = deque()

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
        self.chat_handler = None          # type: Optional[VideoroomChatHandler]
        self.account = None               # type: Optional[AccountInfo]
        self.room = None                  # type: Optional[Videoroom]
        self.bitrate = None
        self.parent_session = None        # type: Optional[VideoroomSessionInfo]  # for subscribers this is their main session (the one used to join), for publishers is None
        self.publisher_id = None          # janus publisher ID for publishers / publisher session ID for subscribers
        self.slow_download = False
        self.slow_upload = False
        self.feeds = PublisherFeedContainer()  # keeps references to all the other participant's publisher feeds that we subscribed to

    def init_publisher(self, account, room):
        self.type = 'publisher'
        self.account = account
        self.room = room
        self.bitrate = room.config.max_bitrate
        self.chat_handler = VideoroomChatHandler(session=self)

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


class ExternalPublisherAccount(object):
    __slots__ = ('id', 'display_name')

    def __init__(self, id, display_name=''):
        self.id = id
        self.display_name = display_name


class ExternalPublisherSession(object):
    __slots__ = ('id', 'publisher_id', 'room', 'account')
    type = 'publisher'

    def __init__(self, id, publisher_id, room):
        self.id = id
        self.publisher_id = publisher_id
        self.room = room
        self.account = ExternalPublisherAccount('janus:{}'.format(id))

    def __repr__(self):
        return '<{0.__class__.__name__}: id={0.id!r} publisher_id={0.publisher_id!r}>'.format(self)


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
    def __init__(self, uri, audio, video):
        self.id = random.getrandbits(32)    # janus needs numeric room names
        self.uri = uri
        self.audio = audio
        self.video = video
        self.config = get_room_config(uri)
        self.log = VideoroomLogger(self)
        # Webrtc-side conference timer. Anchored to videoroom creation
        # (the moment the first WebRTC client opens this room here) and
        # used to compute the duration sent to clients in the
        # conference-participants event. Intentionally independent of
        # whatever start time the SIP focus may report — the webrtc
        # gateway runs its own clock so late joiners see consistent
        # "started N minutes ago" numbers regardless of upstream skew.
        self.start_time = time.time()
        self._active_participants = []
        self._sessions = set()  # type: Set[VideoroomSessionInfo]
        self._id_map = {}       # type: Dict[Union[str, int], VideoroomSessionInfo]  # map session.id -> session and session.publisher_id -> session
        self._shared_files = []
        self._raised_hands = []
        # SIP-side participant roster (uri -> display_text), populated
        # from SIPSessionGotConferenceInfo notifications on any chat
        # session attached to this room.
        self._sip_roster = {}  # type: Dict[str, str]
        # Last admin endpoint seen on the conference-info NOTIFY, keyed by
        # the user entity (the bridge's URI). Used to log the admin URL
        # only when it appears or changes, instead of on every NOTIFY.
        self._admin_endpoints = {}  # type: Dict[str, Tuple[str, str]]
        # Bridge-published conference admin URL and per-room token, cached
        # the first time we see them on a NOTIFY. Used to POST mute
        # commands to the conference focus's admin HTTP API on behalf
        # of the WebRTC client, so the conference's own web handler
        # stays the canonical place where mute is implemented.
        self.admin_endpoint_url = None    # type: Optional[str]
        self.admin_endpoint_token = None  # type: Optional[str]
        # Last-seen audio-level UDP endpoint advertised by the bridge
        # for this room. Cached so the destroy path can drop the
        # subscription immediately and stop the focus from continuing
        # to stream levels at us for the TTL.
        self.audio_levels_udp_endpoint = None  # type: Optional[str]
        # participant_id -> human label ("display <aor>" or just aor),
        # populated from every conference-info NOTIFY. Used by the
        # audio-level periodic log to translate the opaque per-session
        # token published over UDP back into a meaningful identity.
        # Trimmed in sync with the NOTIFY roster (entries for users no
        # longer present are removed below).
        self.participant_labels = {}  # type: Dict[str, str]
        # participant_id of the audio-bridge (the user element carrying
        # the agp-conf:audio_levels_udp_endpoint extension). Used to
        # filter the bridge out of the per-participant audio-level log
        # since its in/out is just plumbing, not a user-facing source.
        self.bridge_participant_id = None  # type: Optional[str]
        # participant_id → VideoroomSession for every WebRTC publisher
        # currently joined to this room. Rebuilt wholesale from each
        # conference-info NOTIFY (see _NH_SIPSessionGotConferenceInfo)
        # and consulted by _RH_videoroom_mute_participant to dispatch a
        # per-participant mute locally over WS instead of POSTing to
        # the conference focus's admin endpoint when the target is a
        # WebRTC peer we already own a session for.
        self.webrtc_participants_by_pid = {}  # type: Dict[str, object]
        # participant_id → user.entity (SIP URI string) for EVERY user
        # in the most recent conference-info NOTIFY, regardless of type.
        # Used by _RH_videoroom_mute_participant to build the Refer-To
        # URI when proxying a SIP-side mute as REFER ;method=MUTE so
        # the request can carry an honest SIP URI alongside the
        # disambiguating participant_id parameter.
        self.participant_uris_by_pid = {}  # type: Dict[str, str]
        # Pids the gateway has already auto-muted on join. Shared
        # across every chat_handler subscribing to this room's
        # conference-info, so the first handler that sees a brand
        # new SIP participant claims the auto-mute and the rest
        # short-circuit. Pids are pruned to current roster on every
        # NOTIFY so a rejoining participant (which gets a fresh pid
        # from the focus) is auto-muted again rather than remembered
        # forever.
        self.auto_muted_pids = set()  # type: Set[str]
        if self.config.record:
            makedirs(self.config.recording_dir, 0o755)
            self.log.info('created (recording on)')
        else:
            self.log.info('created')
        if self.config.video_disabled:
            self.video = False
        if self.config.persistent:
            self.read_files_from_disk()

    @property
    def duration(self):
        """Seconds elapsed since this videoroom was created on the
        webrtcgateway. Computed locally — independent of any duration
        reported by the SIP focus on the other side of the bridge.
        """
        elapsed = int(time.time() - self.start_time)
        return elapsed if elapsed >= 0 else 0

    def update_sip_roster(self, conference_info):
        """
        Apply a conference-info+xml snapshot from sipsimple to this
        videoroom's cached SIP roster. Logs each join / leave once
        (state diff is idempotent — every chat session in this room
        receives the same NOTIFY and calls this method; duplicates
        produce no extra log lines).
        """
        try:
            users = conference_info.users
        except AttributeError:
            return
        new_roster = {}
        new_admin_endpoints = {}
        bridge_admin_url = None
        bridge_admin_token = None
        bridge_udp_endpoint = None
        for u in users:
            entity = str(getattr(u, 'entity', '') or '')
            display = ''
            try:
                if u.display_text and u.display_text.value:
                    display = u.display_text.value
            except AttributeError:
                pass
            if entity:
                new_roster[entity] = display
            # The sylk-janus-audio-bridge participant carries the conference
            # admin endpoint URL + per-room token as agp-conf extensions on
            # its User element. Extract them whenever they are present, and
            # remember the most recent triple so we can log it whenever a
            # new participant joins.
            admin_url = self._extension_value(getattr(u, 'admin_endpoint_url', None))
            admin_token = self._extension_value(getattr(u, 'admin_endpoint_token', None))
            udp_endpoint = self._extension_value(getattr(u, 'audio_levels_udp_endpoint', None))
            if entity and admin_url:
                new_admin_endpoints[entity] = (admin_url, admin_token or '')
                bridge_admin_url = admin_url
                bridge_admin_token = admin_token or ''
                bridge_udp_endpoint = udp_endpoint or ''
        # Diff against previous.
        added = set(new_roster) - set(self._sip_roster)
        removed = set(self._sip_roster) - set(new_roster)
        for uri in sorted(added):
            display = new_roster[uri]
            label = '{} ({})'.format(display, uri) if display else uri
            self.log.info('{} has joined'.format(label))
            # Show the bridge-advertised endpoints alongside every new
            # arrival so the operator can correlate which admin/UDP
            # endpoint that participant should be addressed through.
            # Token is a secret — print only a short prefix.
            if bridge_admin_url:
                token_preview = (bridge_admin_token[:6] + '…') if bridge_admin_token else '(no token)'
                udp_text = bridge_udp_endpoint or '(no UDP)'
                self.log.info('  bridge admin: {url} token={token} udp={udp}'.format(
                    url=bridge_admin_url, token=token_preview, udp=udp_text))
        for uri in sorted(removed):
            display = self._sip_roster[uri]
            label = '{} ({})'.format(display, uri) if display else uri
            self.log.info('{} has left'.format(label))
        self._sip_roster = new_roster
        # Log every change to an admin endpoint advertised by a bridge
        # participant. We log on first appearance and again any time the
        # URL or token value changes (token rotation, server restart with
        # a new room token, IP change, etc.). When the bridge disappears
        # the endpoint vanishes from the payload — log that too.
        for entity, (url, token) in new_admin_endpoints.items():
            if self._admin_endpoints.get(entity) == (url, token):
                continue
            # Token is a secret — only print a short prefix so it doesn't
            # end up in shared log archives in full.
            token_preview = (token[:6] + '…') if token else '(no token)'
            self.log.info('bridge admin endpoint advertised by {entity}: {url} token={token}'.format(
                entity=entity, url=url, token=token_preview))
        for entity in set(self._admin_endpoints) - set(new_admin_endpoints):
            self.log.info('bridge admin endpoint withdrawn by {entity}'.format(entity=entity))
        self._admin_endpoints = new_admin_endpoints

    @staticmethod
    def _extension_value(element):
        """Unwrap a sipsimple XML extension element to its plain Python value.

        The agp-conf extensions (admin_endpoint_url, admin_endpoint_token)
        are XMLStringElement instances — calling `.value` (or str()) yields
        the underlying text. We accept either form and return None when
        the element is missing or empty.
        """
        if element is None:
            return None
        for attr in ('value',):
            v = getattr(element, attr, None)
            if v is not None and v != '':
                return v
        try:
            text = str(element).strip()
            return text or None
        except Exception:
            return None

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

    @property
    def raised_hands(self):
        return self._raised_hands

    @raised_hands.setter
    def raised_hands(self, session_id):
        if session_id in self._raised_hands:
            self.log.info('{session} lowers hand '.format(session=session_id))
            self._raised_hands.remove(session_id)
        else:
            self.log.info('{session} raises hand '.format(session=session_id))
            self._raised_hands.append(session_id)

    def add(self, session):
        assert session not in self._sessions
        assert session.publisher_id is not None
        assert session.publisher_id not in self._id_map and session.id not in self._id_map
        self._sessions.add(session)
        self._id_map[session.id] = self._id_map[session.publisher_id] = session
        self.log.info('{session.account.id} has joined'.format(session=session))
        self._update_bitrate()
        if self._active_participants:
            session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
        if self._shared_files:
            session.owner.send(sylkrtc.VideoroomFileSharingEvent(session=session.id, files=self._shared_files))
        if self._raised_hands:
            session.owner.send(sylkrtc.VideoroomRaisedHandsEvent(session=session.id, raised_hands=self._raised_hands))

        if self.config.invite_participants and len(self._sessions) == 1:
            originator = sylkrtc.SIPIdentity(uri=session.account.id, display_name=session.account.display_name)
            for participant in self.config.invite_participants:
                if session.account.id != participant:
                    push.conference_invite(originator=originator, destination=participant, room=self.uri, call_id=session.id, audio=self.audio, video=self.video)

    # noinspection DuplicatedCode
    def discard(self, session):
        if session in self._sessions:
            self._sessions.discard(session)
            self._id_map.pop(session.id, None)
            self._id_map.pop(session.publisher_id, None)
            self.log.info('{session.account.id} has left'.format(session=session))
            if session.id in self._active_participants:
                self._active_participants.remove(session.id)
                self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
                for session in self._sessions:
                    session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
            self._update_bitrate()

    # noinspection DuplicatedCode
    def remove(self, session):
        self._sessions.remove(session)
        self._id_map.pop(session.id)
        self._id_map.pop(session.publisher_id)
        self.log.info('{session.account.id} has left'.format(session=session))
        if session.id in self._active_participants:
            self._active_participants.remove(session.id)
            self.log.info('active participants: {}'.format(', '.join(self._active_participants) or None))
            for session in self._sessions:
                session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=self._active_participants, originator='videoroom'))
        self._update_bitrate()

    def clear(self):
        for session in self._sessions:
            self.log.info('{session.account.id} has left'.format(session=session))
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

    @staticmethod
    def _fix_path(path):
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
        meta_path = os.path.join(self.config.filesharing_dir, f'meta-{upload_request.shared_file.filename}')
        try:
            with open(path, 'wb') as output_file:
                copyfileobj(upload_request.content, output_file)
            with open(meta_path, 'w+') as output_file:
                output_file.write(json.dumps(upload_request.shared_file.__data__))
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

    @run_in_thread('file-io')
    def read_files_from_disk(self):
        with os.scandir(self.config.filesharing_dir) as file_list:
            for entry in file_list:
                if not entry.name.startswith('.') and entry.is_file() and not entry.name.startswith('meta-'):
                    try:
                        with open(os.path.join(self.config.filesharing_dir, f'meta-{entry.name}'), 'r') as f:
                            content = f.read()
                    except (OSError, IOError):
                        continue
                    try:
                        test = json.loads(content)
                    except (json.JSONDecodeError):
                        continue
                    shared_file = sylkrtc.SharedFile(**test)
                    self._shared_files.append(shared_file)

    def cleanup(self):
        if not self.config.persistent:
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
                self.log.debug('participant bitrate is {} (active) / {} (others)'.format(active_participant_bitrate, other_participant_bitrate))
                for session in self._sessions:
                    if session.id in self._active_participants:
                        bitrate = active_participant_bitrate
                    else:
                        bitrate = other_participant_bitrate
                    if session.bitrate != bitrate:
                        session.bitrate = bitrate
                        session.janus_handle.message(janus.VideoroomUpdatePublisher(bitrate=bitrate), _async=True)
            else:
                bitrate = self.config.max_bitrate // limit(len(self._sessions) - 1, min=1)
                self.log.debug('participant bitrate is {}'.format(bitrate))
                for session in self._sessions:
                    if session.bitrate != bitrate:
                        session.bitrate = bitrate
                        session.janus_handle.message(janus.VideoroomUpdatePublisher(bitrate=bitrate), _async=True)

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
    __normalizer__ = str.maketrans('-', '_')

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


@implementer(IBodyProducer)
class _BytesProducer(object):
    """Minimal IBodyProducer wrapping a fixed bytes payload, for use with
    twisted.web.client.Agent.request when you need to ship a JSON body
    along with a POST. Single-shot — consumer writes the whole buffer
    once and the deferred fires.
    """

    def __init__(self, body):
        self.body = body
        self.length = len(body)

    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
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


class _SipFocusReferralFailed(Exception):
    def __init__(self, data):
        self.data = data


# Process-wide cache for SipFocusReferralHandler DNS lookups.
#
# Every REFER (invite or BYE) does a NAPTR/SRV lookup for the focus or
# outbound-proxy host, which can add 100–300 ms per kick on a slow
# resolver — and the kick path fires N REFERs in parallel when a
# WebRTC user leaves, so the lookups stack. The hostname / SRV records
# we resolve here don't change during a conference's lifetime, so a
# short-lived in-memory cache is safe and shaves the latency to ~0
# after the first lookup.
#
# Key: (lookup_uri_string, transport_list_tuple).
# Value: (routes_list, expiry_unix_ts).
# TTL: 5 minutes — long enough to cover the lifetime of any single
# conference, short enough that a real SRV change is picked up on the
# next conference.
_SIP_PROXY_LOOKUP_CACHE = {}
_SIP_PROXY_LOOKUP_TTL = 300  # seconds


def _cached_lookup_sip_proxy(lookup_uri, transport_list, log):
    """Look up SIP proxy routes for ``lookup_uri`` over ``transport_list``,
    returning a cached result when available and fresh. On a miss the
    DNS lookup is performed inline (same thread, same blocking semantics
    as the underlying ``DNSLookup().lookup_sip_proxy().wait()`` call),
    then cached. Raises ``DNSLookupError`` like the wrapped call.
    """
    key = (str(lookup_uri), tuple(transport_list))
    now = time.time()
    entry = _SIP_PROXY_LOOKUP_CACHE.get(key)
    if entry is not None:
        routes, expiry = entry
        if expiry > now:
            log.info('[conference] DNS lookup for {} (transports={}) — cache hit ({} route(s), {}s left)'.format(
                lookup_uri, list(transport_list), len(routes), int(expiry - now)))
            return routes
        # Stale; drop so a fresh miss is logged below.
        _SIP_PROXY_LOOKUP_CACHE.pop(key, None)
    log.info('[conference] DNS lookup for {} (transports={})'.format(
        lookup_uri, list(transport_list)))
    routes = DNSLookup().lookup_sip_proxy(lookup_uri, list(transport_list)).wait()
    _SIP_PROXY_LOOKUP_CACHE[key] = (routes, now + _SIP_PROXY_LOOKUP_TTL)
    return routes


@implementer(IObserver)
class SipFocusReferralHandler(object):
    def __init__(self, focus_uri, participant_uri, account, log, status_callback=None, method='INVITE', refer_to_extra_params=None):
        self.focus_uri = focus_uri
        self.participant_uri = participant_uri
        self.account = account
        self.log = log
        self.status_callback = status_callback
        # Refer-To method parameter (RFC 4488). 'INVITE' = invite the
        # named URI into the conference; 'BYE' = ask the focus to BYE
        # the named URI out of the conference (RFC 4579); 'MUTE' /
        # 'UNMUTE' = ask the focus to mute / unmute the named participant
        # (sylk extension, handled by the conference application). Anything
        # else is rejected upstream by the focus (488). Default 'INVITE'
        # preserves the original behaviour of every existing caller.
        self.method = (method or 'INVITE').upper()
        # Optional extra Refer-To header parameters appended after the
        # standard ones. Currently used by the MUTE / UNMUTE path to
        # carry `participant_id=<token>` for disambiguating between
        # devices sharing one AoR; safe to add for any method (the
        # focus simply ignores params it doesn't know).
        self.refer_to_extra_params = dict(refer_to_extra_params or {})
        self._channel = coros.queue()
        self._referral = None
        # Set by _safe_run when the handler completes (success or
        # failure). Callers that need to serialise on REFER completion
        # — e.g. the last-publisher auto-kick path in
        # _cleanup_videoroom_session, which must hold the chat session
        # open until every REFER has been dispatched — wait() on this.
        self._done_event = GreenEvent()

    def _emit_status(self, state, code=None, reason=None):
        if self.status_callback is None:
            return
        try:
            self.status_callback(str(self.participant_uri), state, code, reason)
        except Exception as e:
            self.log.warning('REFER status callback failed: {}'.format(e))

    def start(self):
        self.log.info('[conference] SipFocusReferralHandler.start() method={} for {} -> {}'.format(
            self.method, self.participant_uri, self.focus_uri))
        proc.spawn(self._safe_run)

    def _safe_run(self):
        try:
            self._run()
        except Exception as e:
            self.log.exception('[conference] SipFocusReferralHandler crashed for {}: {}'.format(
                self.participant_uri, e))
            try:
                self._emit_status('failed', 0, 'internal error: {}'.format(e))
            except Exception:
                pass
        finally:
            # Always signal completion so waiters don't hang. Both
            # success and crash paths land here.
            try:
                self._done_event.set()
            except Exception:
                pass

    def wait(self, timeout=None):
        """
        Block the calling green thread until this REFER has finished
        (the underlying _safe_run greenlet exited). Optional timeout
        in seconds; without one the wait is unbounded but bounded in
        practice by the REFER send-timeout (30 s) plus the drain loop.
        Used by the last-WebRTC-publisher auto-kick path so the chat
        session stays alive until every kick REFER is dispatched.
        """
        if timeout is None:
            self._done_event.wait()
            return True
        try:
            with api.timeout(timeout):
                self._done_event.wait()
            return True
        except api.TimeoutError:
            return False

    def _run(self):
        self.log.info('[conference] _run entered for {} (focus={})'.format(
            self.participant_uri, self.focus_uri))
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        try:
            sip_account = DefaultAccount()
            if sip_account.sip.outbound_proxy is not None and sip_account.sip.outbound_proxy.transport in settings.sip.transport_list:
                lookup_uri = SIPURI(host=sip_account.sip.outbound_proxy.host,
                                    port=sip_account.sip.outbound_proxy.port,
                                    parameters={'transport': sip_account.sip.outbound_proxy.transport})
            else:
                lookup_uri = self.focus_uri
            # _cached_lookup_sip_proxy logs the lookup itself, including
            # whether it was a cache hit. Don't duplicate the log line
            # here. On a hit the underlying DNSLookup() is not invoked,
            # so reuses of the same proxy/focus for follow-up REFERs
            # (auto-kick batch, repeat invites) skip the resolver hop.
            try:
                routes = _cached_lookup_sip_proxy(lookup_uri, settings.sip.transport_list, self.log)
            except DNSLookupError as e:
                self.log.warning('[conference] REFER to focus {} for {}: DNS lookup failed: {}'.format(self.focus_uri, self.participant_uri, e))
                self._emit_status('failed', 0, 'DNS lookup failed')
                return
            self.log.info('[conference] DNS lookup returned {} route(s) for {}'.format(len(routes), self.participant_uri))
            try:
                from_uri = SIPURI.parse(self.account.uri)
            except SIPCoreError:
                self.log.warning('[conference] REFER to focus {} for {}: invalid account URI'.format(self.focus_uri, self.participant_uri))
                self._emit_status('failed', 0, 'Invalid account URI')
                return
            credentials = Credentials(username=from_uri.user,
                                      password=self.account.password.encode('utf-8'),
                                      digest=True)
            deadline = time.time() + 30
            for route in routes:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                transport = route.transport
                parameters = {} if transport == 'udp' else {'transport': transport}
                contact_uri = SIPURI(user=sip_account.contact.username,
                                     host=SIPConfig.local_ip.normalized,
                                     port=getattr(Engine(), '{}_port'.format(transport)),
                                     parameters=parameters)
                refer_to_header = ReferToHeader(str(self.participant_uri))
                refer_to_header.parameters['method'] = self.method
                # `media=audio` only makes sense for INVITE — it tells
                # the conference's IncomingReferralHandler to restrict
                # the outgoing INVITE to the listed media (chat/MSRP
                # then isn't offered). For BYE / MUTE / UNMUTE the
                # focus doesn't look at media, so we skip it to keep
                # the wire form clean for those branches.
                if self.method == 'INVITE':
                    refer_to_header.parameters['media'] = 'audio'
                # Append any extra params the caller asked for (e.g.
                # `participant_id=<token>` on a MUTE / UNMUTE REFER).
                # Done after the standard params so the caller cannot
                # accidentally clobber `method` / `media`.
                for _k, _v in self.refer_to_extra_params.items():
                    if _k in ('method', 'media'):
                        continue
                    refer_to_header.parameters[_k] = _v
                self.log.info('[conference] sending REFER for {} via route {}:{}/{}'.format(
                    self.participant_uri, route.address, route.port, transport))
                referral = Referral(self.focus_uri,
                                    FromHeader(from_uri, self.account.display_name),
                                    ToHeader(self.focus_uri),
                                    refer_to_header,
                                    ContactHeader(contact_uri),
                                    RouteHeader(route.uri),
                                    credentials)
                notification_center.add_observer(self, sender=referral)
                try:
                    referral.send_refer(timeout=limit(remaining, min=1, max=5))
                except SIPCoreError as e:
                    notification_center.remove_observer(self, sender=referral)
                    self.log.warning('[conference] REFER to focus {} for {}: send failed: {}'.format(self.focus_uri, self.participant_uri, e))
                    continue
                self._referral = referral
                self.log.info('[conference] REFER sent for {} — entering notification drain'.format(self.participant_uri))
                break
            else:
                self.log.warning('[conference] REFER to focus {} for {}: no usable routes'.format(self.focus_uri, self.participant_uri))
                self._emit_status('failed', 0, 'No usable routes')
                return
            final_code = None
            final_reason = None
            saw_start = False
            try:
                while True:
                    notification = self._channel.wait()
                    self.log.debug('[conference] drain got notification {} for {}'.format(notification.name, self.participant_uri))
                    if notification.name == 'SIPReferralDidStart':
                        saw_start = True
                        continue
                    if notification.name == 'SIPReferralGotNotify':
                        body = getattr(notification.data, 'body', None)
                        event_name = getattr(notification.data, 'event', None)
                        self.log.info('[conference] NOTIFY from focus {} for {}: event={!r} body={!r}'.format(
                            self.focus_uri, self.participant_uri, event_name, body))
                        if body:
                            if isinstance(body, bytes):
                                try:
                                    body_str = body.decode('utf-8', errors='replace')
                                except Exception:
                                    body_str = ''
                            else:
                                body_str = body
                            match = None
                            try:
                                match = sipfrag_re.match(body_str)
                            except Exception as e:
                                self.log.warning('[conference] sipfrag_re match raised on {!r}: {}'.format(body_str, e))
                            if match is None:
                                self.log.info('[conference] NOTIFY body did not match sipfrag pattern: {!r}'.format(body_str))
                            else:
                                try:
                                    code = int(match.group('code'))
                                except (ValueError, IndexError):
                                    code = None
                                reason = None
                                try:
                                    reason = match.group('reason')
                                except IndexError:
                                    pass
                                if code is not None:
                                    final_code = code
                                    final_reason = reason
                                    if code >= 200:
                                        state = 'failed' if code >= 300 else 'success'
                                    else:
                                        state = 'progress'
                                    self.log.info('[conference] REFER for {} -> {} {} (state={})'.format(
                                        self.participant_uri, code, reason, state))
                                    self._emit_status(state, code, reason)
                    elif notification.name == 'SIPReferralDidEnd':
                        self.log.info('[conference] REFER subscription ended for {} (final {} {}, saw_start={})'.format(
                            self.participant_uri, final_code, final_reason, saw_start))
                        break
            except _SipFocusReferralFailed as e:
                self.log.warning('[conference] REFER to focus {} for {}: {} {}'.format(
                    self.focus_uri, self.participant_uri, e.data.code, e.data.reason))
                self._emit_status('failed', e.data.code, e.data.reason)
            else:
                if final_code is not None and final_code >= 300:
                    self.log.warning('[conference] REFER to SIP focus {} to invite {} failed: {} {}'.format(
                        self.focus_uri, self.participant_uri, final_code, final_reason))
                else:
                    self.log.info('[conference] REFER to SIP focus {} to invite {} completed (final {} {})'.format(
                        self.focus_uri, self.participant_uri, final_code, final_reason))
                    if final_code is None:
                        self._emit_status('success', 200, 'OK')
            finally:
                if self._referral is not None:
                    notification_center.remove_observer(self, sender=self._referral)
        finally:
            self._referral = None

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPReferralDidStart(self, notification):
        self._channel.send(notification)

    def _NH_SIPReferralDidEnd(self, notification):
        self._channel.send(notification)

    def _NH_SIPReferralDidFail(self, notification):
        self._channel.send_exception(_SipFocusReferralFailed(notification.data))

    def _NH_SIPReferralGotNotify(self, notification):
        self._channel.send(notification)


# noinspection PyPep8Naming
@implementer(IObserver)
class ConnectionHandler(object):

    janus = JanusBackend()

    def __init__(self, protocol):
        self.protocol = protocol
        self.device_id = base64.b64encode(hashlib.md5(protocol.peer.encode('utf-8')).digest()).rstrip(b'=\n').decode('utf-8')
        self.janus_session = None      # type: Optional[JanusSession]
        self.accounts_map = {}         # account ID -> account
        self.devices_map = {}          # device ID -> account
        self.connections_map = {}      # peer connection -> account
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
        self.decline_code = JanusConfig.decline_code or 486

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
                self.protocol.disconnect(3000, str(e))
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
            for account_info in list(self.accounts_map.values()):
                if account_info.janus_handle is not None:
                    self.janus.set_event_handler(account_info.janus_handle.id, None)
                    for helper in account_info.janus_helpers:
                        # helper.detach()
                        self.account_handles_map.pop(helper.id, None)
                    account_info.janus_helpers = []

                notification_center = NotificationCenter()
                notification_center.remove_observer(self, sender=account_info.id)
            for session in self.sip_sessions:
                if session.janus_handle is not None:
                    self.janus.set_event_handler(session.janus_handle.id, None)
            for session in self.videoroom_sessions:
                if session.janus_handle is not None:
                    self.janus.set_event_handler(session.janus_handle.id, None)
                if session.chat_handler is not None:
                    notification_center = NotificationCenter()
                    notification_center.remove_observer(self, sender=session.chat_handler)
                    session.chat_handler.end()
                    session.chat_handler = None
                if session in session.room:
                    # We need to check if the room can be destroyed, else this will never happen
                    reactor.callLater(2, call_in_green_thread, self._maybe_destroy_videoroom_after_disconnect, session.room)
                session.room.discard(session)
                session.feeds.clear()
            self.janus_session.destroy()  # this automatically detaches all plugin handles associated with it, no need to manually do it
        # cleanup
        self.ready_event.clear()
        self.accounts_map.clear()
        self.devices_map.clear()
        self.connections_map.clear()
        self.account_handles_map.clear()
        self.sip_sessions.clear()
        self.videoroom_sessions.clear()
        self.janus_session = None
        self.protocol = None
        self.state = 'stopped'

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

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
                notification_center = NotificationCenter()
                notification_center.remove_observer(self, sender=session.chat_handler)
                session.room.discard(session)
                session.feeds.clear()
                session.janus_handle.detach()
                # Last-publisher auto-kick of SIP participants. When the
                # WebRTC side of the room empties out, every SIP-side
                # participant (PSTN-dialled invitees, bridge, etc.) is
                # left orphaned in the conference focus — nothing's
                # listening to them on the WebRTC end. Send
                # REFER ;method=BYE for each of them through the still-
                # alive chat session BEFORE we tear that session down
                # so the chat dialog (the SUBSCRIBE/NOTIFY anchor the
                # focus uses to validate REFERs from us) is still
                # present when the focus processes each REFER.
                #
                # Fire-and-forget: we spawn the REFER greenlets without
                # waiting on completion. Tearing down the chat session
                # next isn't synchronous with REFER transmission either
                # — the focus has more than enough time to read the
                # REFERs off the wire before our BYE for the chat
                # session lands. This avoids holding the destroy path
                # behind a 15 s/REFER timeout when the focus is slow.
                last_publisher = len(session.room) == 0
                # Log the gate state so it's never invisible WHY the
                # auto-kick did or didn't fire — multiple things can
                # silently disable it (no chat handler yet, no SIP
                # session, focus not detected) and "nobody got kicked"
                # is exactly the failure mode we want to diagnose.
                session.room.log.info(
                    'auto-kick gate: last_publisher={} chat_handler={} sip_session={} roster_size={}'.format(
                        last_publisher,
                        session.chat_handler is not None,
                        session.chat_handler is not None and session.chat_handler.sip_session is not None,
                        len(getattr(session.room, '_sip_roster', {})),
                    )
                )
                if last_publisher and session.chat_handler is not None and session.chat_handler.sip_session is not None:
                    try:
                        self._kick_all_sip_participants_fire_and_forget(session)
                    except Exception as e:
                        session.room.log.warning('auto-kick failed: {}'.format(e))
                session.chat_handler.end()
                self._maybe_destroy_videoroom(session.room)
            else:
                session.parent_session.feeds.discard(session.publisher_id)
                session.janus_handle.detach()

    def _kick_all_sip_participants_fire_and_forget(self, session):
        """
        Walk the room's SIP roster and spawn REFER ;method=BYE for each
        entry except the gateway's own chat-session URI. Does NOT wait
        on completion — REFER greenlets run independently of the
        caller's teardown path. Used on videoroom destroy where we
        don't want to hold the destroy behind a stuck focus's REFER
        response. No-op if the chat session never reached a SIP focus.
        """
        spawned = self._spawn_kick_refers(session)
        if spawned:
            room = session.room
            room.log.info('auto-kick: spawned {} REFER ;method=BYE (fire-and-forget)'.format(spawned))

    def _kick_all_sip_participants_blocking(self, session):
        """
        Walk the room's SIP roster and send REFER ;method=BYE for each
        entry except the gateway's own chat-session URI. Blocks until
        every REFER completes (per-handler timeout) so the caller can
        rely on the auto-kick being done before tearing the chat
        session down. No-op if the chat session never reached a SIP
        focus.
        """
        handlers = self._spawn_kick_refers(session, return_handlers=True)
        if not handlers:
            return
        room = session.room
        room.log.info('auto-kick: waiting for {} REFER(s) to complete'.format(len(handlers)))
        # Bound each wait so a stuck focus can't hold the chat session
        # open indefinitely. 15 s is generous — a healthy REFER usually
        # completes in well under a second.
        for handler in handlers:
            try:
                handler.wait(timeout=15)
            except Exception as e:
                room.log.warning('auto-kick: wait raised for {}: {}'.format(handler.participant_uri, e))
        room.log.info('auto-kick: all REFERs done; closing chat session')

    def _spawn_kick_refers(self, session, return_handlers=False):
        """Spawn REFER ;method=BYE for every SIP-side roster entry except
        the gateway's own chat-session URI. Returns the number of handlers
        spawned (or the list when ``return_handlers`` is True, used by
        the blocking variant which waits on each). Pure greenlet
        spawn — does not block.
        """
        room = session.room
        chat_handler = session.chat_handler
        if chat_handler is None or chat_handler.sip_session is None:
            room.log.info('auto-kick: skipped (chat_handler={} sip_session={})'.format(
                chat_handler is not None,
                chat_handler is not None and chat_handler.sip_session is not None,
            ))
            return [] if return_handlers else 0
        if not chat_handler.sip_session.remote_focus:
            room.log.info('auto-kick: skipped (remote_focus=False on chat session — focus didn\'t advertise isfocus parameter)')
            return [] if return_handlers else 0
        try:
            focus_uri = SIPURI.new(chat_handler.sip_session.remote_identity.uri)
        except SIPCoreError as e:
            room.log.warning('auto-kick: focus URI unresolved: {}'.format(e))
            return [] if return_handlers else 0

        # Exclude the gateway's own SIP chat session URI — that leg is
        # about to be BYE'd by chat_handler.end() in the caller. Compare
        # by AoR (sip:user@host with params stripped) since the entity
        # URI in the roster may carry differing display name / params
        # from the local_identity.uri.
        def _aor(u):
            s = str(u)
            if s.startswith('sip:'):
                s = s[4:]
            elif s.startswith('sips:'):
                s = s[5:]
            return s.split(';', 1)[0].lower()
        self_aor = _aor(chat_handler.sip_session.local_identity.uri)

        handlers = []
        roster = list(room._sip_roster)
        room.log.info('auto-kick: walking roster of {} entries (self_aor={})'.format(
            len(roster), self_aor))
        for participant_uri in roster:
            if _aor(participant_uri) == self_aor:
                room.log.info('auto-kick: skipping {} (self)'.format(participant_uri))
                continue
            # The audio-bridge participant terminates itself when the
            # videoroom on the gateway side empties out (it watches the
            # publisher count via the bridge's own conference-info
            # subscription). REFER ;method=BYE'ing it would race with
            # the bridge's own shutdown and is not needed.
            if 'app=sylk-janus-audio-bridge' in participant_uri.lower():
                room.log.info('auto-kick: skipping {} (audio-bridge — self-terminates)'.format(participant_uri))
                continue
            try:
                participant_sip_uri = SIPURI.parse(participant_uri)
            except SIPCoreError:
                room.log.warning('auto-kick: skipping {} (invalid URI)'.format(participant_uri))
                continue
            room.log.info('auto-kick: REFER ;method=BYE for {}'.format(participant_uri))
            handler = SipFocusReferralHandler(
                focus_uri, participant_sip_uri, session.account, room.log, method='BYE')
            handler.start()
            handlers.append(handler)
        if not handlers:
            room.log.info('auto-kick: no eligible participants to kick after walking roster')
        return handlers if return_handlers else len(handlers)

    def _maybe_destroy_videoroom(self, videoroom):
        # should only be called from a green thread.

        if self.protocol is None or self.janus_session is None:  # The connection was closed, there is nothing to do
            return

        if videoroom in self.protocol.factory.videorooms and not videoroom:
            # Drop the audio-level UDP subscription for this room first.
            # Otherwise the focus keeps streaming levels at us for the
            # full TTL after the last publisher leaves, and the periodic
            # log printer prints "?" entries for participant_ids the
            # gateway can no longer resolve (the Videoroom is gone, so
            # the participant_labels map is unreachable). Unsubscribing
            # also lets the focus drop its end of the registration
            # immediately instead of waiting for the TTL to expire.
            try:
                from sylk.applications.webrtcgateway.audio_level_udp import AudioLevelUDPClient
                udp_endpoint = getattr(videoroom, 'audio_levels_udp_endpoint', None)
                if udp_endpoint:
                    conf_uri = videoroom.uri.replace('videoconference', 'conference', 1)
                    AudioLevelUDPClient().drop_subscription(udp_endpoint, conf_uri)
                # Drop any stale accumulator entries keyed by this room
                # so the next 5 s log tick doesn't print "?" lines for
                # already-orphaned datagrams in flight.
                try:
                    AudioLevelUDPClient()._log_accumulator.pop(
                        videoroom.uri.replace('videoconference', 'conference', 1).lower(), None)
                except Exception:
                    pass
            except Exception as e:
                videoroom.log.debug('audio-level UDP cleanup on destroy failed: {}'.format(e))

            self.protocol.factory.videorooms.remove(videoroom)
            videoroom.cleanup()

            with VideoroomPluginHandle(self.janus_session, event_handler=self._handle_janus_videoroom_event) as videoroom_handle:
                videoroom_handle.destroy(room=videoroom.id)

            videoroom.log.info('destroyed')

    def _maybe_destroy_videoroom_after_disconnect(self, videoroom):
        # should only be called from a green thread.

        if self.protocol is None and not videoroom:
            videoroom.cleanup()

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

    def _callid_to_uuid(self, callid):
        hexa = hashlib.md5(callid.encode()).hexdigest()
        uuidv4 = '%s-%s-%s-%s-%s' % (hexa[:8], hexa[8:12], hexa[12:16], hexa[16:20], hexa[20:])
        return uuidv4

    def _lookup_sip_target_route(self, uri):
        if GeneralConfig.local_sip_messages:
            return Route(address=SIPConfig.local_ip, port=SIPConfig.local_tcp_port, transport='tcp')
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
        self.log.debug('DNS lookup for SIP message proxy for {} yielded {}'.format(uri, route))
        return route

    def _send_sip_message(self, account, uri, message_id, content, content_type='text/plain', timestamp=None, add_disposition=True):
        route = self._lookup_sip_target_route(uri)
        sip_uri = SIPURI.parse('sip:%s' % uri)
        if route:
            identity = str(account.uri)
            if account.display_name:
                identity = '"%s" <%s>' % (account.display_name, identity)
            self.log.debug("sending message from '%s' to '%s' using proxy %s" % (identity, uri, route))

            from_uri = SIPURI.parse(account.uri)
            content = content if isinstance(content, bytes) else content.encode()
            ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
            additional_headers = [CPIMHeader('Message-ID', ns, message_id)]
            additional_sip_headers = []
            if add_disposition:
                additional_headers.append(CPIMHeader('Disposition-Notification', ns, 'positive-delivery, display'))
            if GeneralConfig.local_sip_messages:
                additional_sip_headers.append(Header('X-Sylk-App', 'webrtcgateway'))
            payload = CPIMPayload(content,
                                  content_type,
                                  charset='utf-8',
                                  sender=ChatIdentity(from_uri, account.display_name),
                                  recipients=[ChatIdentity(sip_uri, None)],
                                  timestamp=timestamp if timestamp is not None else str(ISOTimestamp.now()),
                                  additional_headers=additional_headers)
            payload, content_type = payload.encode()

            credentials = Credentials(username=from_uri.user, password=account.password.encode('utf-8'), digest=True)
            message_request = Message(FromHeader(from_uri, account.display_name),
                                      ToHeader(sip_uri),
                                      RouteHeader(route.uri),
                                      content_type,
                                      payload,
                                      credentials=credentials,
                                      extra_headers=additional_sip_headers)
            notification_center = NotificationCenter()
            notification_center.add_observer(self, sender=message_request)
            #self._message_queue.append((message_id, content, content_type))
            message_request.send()

    def _send_simple_sip_message(self, account, uri, content, content_type='text/plain'):
        route = self._lookup_sip_target_route(uri)
        sip_uri = SIPURI.parse('sip:%s' % uri)
        if route:
            identity = str(account)
            self.log.info("sending simple message from '%s' to '%s' using proxy %s" % (identity, uri, route))

            from_uri = SIPURI.parse(f'sip:{identity}')
            content = content if isinstance(content, bytes) else content.encode()

            message_request = Message(FromHeader(from_uri),
                                      ToHeader(sip_uri),
                                      RouteHeader(route.uri),
                                      content_type,
                                      content,
                                      extra_headers=[Header('X-Sylk-To-Sip', 'yes')])

            message_request.send()

    def _fork_event_to_online_accounts(self, account_info, event):
        for protocol in self.protocol.factory.connections.difference([self.protocol]):
            connection_handler = protocol.connection_handler
            try:
                connection_handler.accounts_map[account_info.id]
            except KeyError:
                pass
            else:
                connection_handler.send(event)

    def _send_in_dialog_sip_message(self, session, message_id, content, content_type='text/plain', timestamp=None, add_disposition=True):
        identity = str(session.account.uri)
        if session.account.display_name:
            identity = '"%s" <%s>' % (session.account.display_name, identity)
        self.log.info("sending in dialag message from '%s' to '%s' " % (identity, session.remote_identity.uri))

        from_uri = SIPURI.parse(session.account.uri)
        sip_uri = SIPURI.parse('sip:%s' % session.remote_identity.uri)
        content = content if isinstance(content, bytes) else content.encode()
        if add_disposition:
            ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
            additional_headers = [CPIMHeader('Message-ID', ns, message_id)]
        payload = CPIMPayload(content,
                              content_type,
                              charset='utf-8',
                              sender=ChatIdentity(from_uri, session.account.display_name),
                              recipients=[ChatIdentity(sip_uri, None)],
                              timestamp=timestamp if timestamp is not None else str(ISOTimestamp.now()),
                              additional_headers=additional_headers)
        payload, content_type = payload.encode()

        session.janus_handle.send_message(content_type=content_type, content=payload)
        session._message_queue.append((message_id, payload, content_type))

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

    def _RH_ping(self, request):
        pass

    def _RH_lookup_public_key(self, request):
        storage = MessageStorage()
        public_key = storage.get_public_key(account=request.uri)
        if isinstance(public_key, defer.Deferred):
            public_key.addCallback(lambda result: self.send(sylkrtc.LookupPublicKeyEvent(uri=request.uri, public_key=result)))
        else:
            self.send(sylkrtc.LookupPublicKeyEvent(uri=request.uri, public_key=public_key))

    def _RH_account_add(self, request):
        if request.account in self.accounts_map:
            raise APIError('Account {request.account} already added'.format(request=request))

        # check if domain is acceptable
        domain = request.account.partition('@')[2]
        if not {'*', domain}.intersection(GeneralConfig.sip_domains):
            raise APIError('SIP domain not allowed: %s' % domain)

        # Create and store our mapping
        account_info = AccountInfo(request.account, request.password, request.display_name, request.user_agent, request.incoming_header_prefixes)
        # get the auth config for domain
        account_info.auth_handle = AuthHandler(account_info, self)
        self.accounts_map[account_info.id] = account_info
        self.devices_map[self.device_id] = account_info.id
        self.connections_map[self.protocol.peer] = account_info.id
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=account_info.id)
        self.log.debug(f'Incoming header prefixes: {request.incoming_header_prefixes}')
        self.log.info('added using {request.user_agent}'.format(request=request))

    def _RH_account_remove(self, request):
        try:
            account_info = self.accounts_map.pop(request.account)
        except KeyError:
            raise APIError('Unknown account specified for remove: {request.account}'.format(request=request))

        # cleanup in case the client didn't unregister before removing the account
        if account_info.janus_handle is not None:
            account_info.janus_handle.detach()
            self.account_handles_map.pop(account_info.janus_handle.id)
            for helper in account_info.janus_helpers:
                helper.detach()
                self.account_handles_map.pop(helper.id, None)
            account_info.janus_helpers = []
            notification_center = NotificationCenter()
            notification_center.remove_observer(self, sender=account_info.id)
        self.log.info('removed')

        try:
            del(self.devices_map[request.account])
        except KeyError:
            pass

        try:
            del(self.connections_map[request.account])
        except KeyError:
            pass

    def _RH_account_register(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified for register: {request.account}'.format(request=request))

        proxy = self._lookup_sip_proxy(request.account)

        if account_info.janus_handle is not None:
            # Destroy the existing plugin handle
            account_info.janus_handle.detach()
            self.account_handles_map.pop(account_info.janus_handle.id)
            account_info.janus_handle = None
            for helper in account_info.janus_helpers:
                helper.detach()
                self.account_handles_map.pop(helper.id, None)
            account_info.janus_helpers = []

        # Create a plugin handle
        account_info.janus_handle = SIPPluginHandle(self.janus_session, event_handler=self._handle_janus_sip_event)
        self.account_handles_map[account_info.janus_handle.id] = account_info

        if ExternalAuthConfig.enable:
            account_info.auth_handle.authenticate(proxy)
        else:
            account_info.janus_handle.register(account_info, proxy=proxy)
            self.log.info('registering to SIP Proxy {proxy}...'.format(proxy=proxy))

    def _RH_account_unregister(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified for unregister: {request.account}'.format(request=request))

        if account_info.janus_handle is not None:
            account_info.janus_handle.detach()
            self.account_handles_map.pop(account_info.janus_handle.id)
            account_info.janus_handle = None
            for helper in account_info.janus_helpers:
                helper.detach()
                self.account_handles_map.pop(helper.id, None)
            account_info.janus_helpers = []

        if 'pn_app' in account_info.contact_params:
            storage = TokenStorage()
            storage.remove(request.account, account_info.contact_params['pn_app'], account_info.contact_params['pn_device'])

        self.log.info('unregistered')

    def _RH_account_devicetoken(self, request):
        if request.account not in self.accounts_map:
            raise APIError('Unknown account specified for token: {request.account}'.format(request=request))

        if request.token is not None:
            account_info = self.accounts_map[request.account]
            account_info.contact_params = {
                'pn_app': request.app,
                'pn_tok': request.token,
                'pn_type': request.platform,
                'pn_device': request.device,
                'pn_silent': str(int(request.silent is True))  # janus expects a string
            }
            if account_info.auth_state:
                storage = TokenStorage()
                storage.add(request.account, account_info.contact_params, account_info.user_agent)

            self.log.info('added token on {request.platform} device {request.device})'.format(request=request))

    def _RH_account_message(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        uri = request.uri
        content_type = request.content_type
        content = request.content if content_type.startswith('text') else request.content.encode('latin1')
        message_id = request.message_id
        timestamp = request.timestamp

        storage = MessageStorage()
        storage.add(account=account_info.id,
                    contact=uri,
                    direction="outgoing",
                    content=content if isinstance(content, str) else content.decode('latin1'),
                    content_type=content_type,
                    timestamp=timestamp,
                    disposition_notification=['positive-delivery', 'display'],
                    message_id=message_id,
                    state='pending')

        self.log.info('sending message ({content_type}) to: {uri}'.format(content_type=content_type, uri=uri))
        self._send_sip_message(account_info, uri, message_id, content, content_type, timestamp=timestamp)

        event = sylkrtc.AccountSyncEvent(account=account_info.id, type='message', action='add', content=request)
        self._fork_event_to_online_accounts(account_info, event)

    def _RH_account_disposition_notification(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        uri = request.uri
        message_id = request.message_id
        state = request.state
        if state == 'delivered':
            notification = DeliveryNotification(state)
        elif state == 'displayed':
            notification = DisplayNotification(state)
        elif state == 'error':
            notification = DisplayNotification(state)

        content = IMDNDocument.create(message_id=message_id, datetime=request.timestamp, recipient_uri=uri, notification=notification)
        storage = MessageStorage()
        storage.update(account=account_info.id,
                       state=state,
                       message_id=message_id)
        self.log.info('sending IMDN message ({status}) to: {uri}'.format(status=state, uri=uri))
        self._send_sip_message(account_info, uri, str(uuid.uuid4()), content, IMDNDocument.content_type, add_disposition=False)

    def _RH_account_sync_conversations(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        storage = MessageStorage()
        try:
            since = request.since
        except AttributeError:
            since = None
        messages = storage[[account_info.id, request.message_id, since]]

        if isinstance(messages, defer.Deferred):
            messages.addCallback(lambda result: self.send(sylkrtc.AccountSyncConversationsEvent(account=account_info.id, messages=result[:request.limit])))

    def _RH_account_mark_conversation_read(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        contact = request.contact
        content = sylkrtc.AccountMarkConversationReadEventData(contact=request.contact)

        storage = MessageStorage()
        storage.mark_conversation_read(account_info.id, contact)
        storage.add(account=account_info.id,
                    contact=request.contact,
                    direction='',
                    content=request.contact,
                    content_type='application/sylk-conversation-read',
                    timestamp=str(ISOTimestamp.now()),
                    disposition_notification='',
                    message_id=str(uuid.uuid4()))

        event = sylkrtc.AccountSyncEvent(account=account_info.id, type='conversation', action='read', content=content)
        self._fork_event_to_online_accounts(account_info, event)

        self._send_simple_sip_message(contact, account_info.id, json.dumps(content.__data__), 'application/sylk-conversation-read')

    def _RH_account_remove_message(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        contact = request.contact
        message_id = request.message_id

        storage = MessageStorage()
        storage.removeMessage(account=account_info.id, message_id=message_id)

        content = sylkrtc.AccountMessageRemoveEventData(contact=contact, message_id=message_id)
        storage.add(account=account_info.id,
                    contact=contact,
                    direction='outgoing',
                    content=json.dumps(content.__data__),
                    content_type='application/sylk-message-remove',
                    timestamp=str(ISOTimestamp.now()),
                    disposition_notification='',
                    message_id=str(uuid.uuid4()))

        event = sylkrtc.AccountSyncEvent(account=account_info.id, type='message', action='remove', content=content)
        self._fork_event_to_online_accounts(account_info, event)

        self._send_simple_sip_message(contact, account_info.id, json.dumps(content.__data__), 'application/sylk-message-remove')

        # Delete from receiver
        def receiver_remove_message(msg_id, messages):
            for message in reversed(messages):
                is_dict = isinstance(message, dict)

                message_id = message["message_id"] if is_dict else message.message_id
                direction = message["direction"] if is_dict else message.direction
                account = message["account"] if is_dict else message.account
                contact = message["contact"] if is_dict else message.contact
                if message_id == msg_id and direction == 'incoming':
                    storage = MessageStorage()
                    storage.removeMessage(account=account, message_id=message_id)

                    content = sylkrtc.AccountMessageRemoveEventData(contact=contact, message_id=message_id, direction="incoming")
                    storage.add(account=account,
                                contact=contact,
                                direction='incoming',
                                content=json.dumps(content.__data__),
                                content_type='application/sylk-message-remove',
                                timestamp=str(ISOTimestamp.now()),
                                disposition_notification='',
                                message_id=str(uuid.uuid4()))

                    event = sylkrtc.AccountSyncEvent(account=account, type='message', action='remove', content=content)
                    account_object = type('account_object', (object,), {'id': account})
                    self._fork_event_to_online_accounts(account_object, event)
                    self.log.info("Removed receiver message")
                    break
        messages = storage[[contact, '']]
        if isinstance(messages, defer.Deferred):
            messages.addCallback(lambda result: receiver_remove_message(msg_id=request.message_id, messages=result))

    def _RH_account_remove_conversation(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        contact = request.contact

        storage = MessageStorage()
        storage.removeChat(account=account_info.id, contact=contact)

        timestamp = str(ISOTimestamp.now())
        storage.add(account=account_info.id,
                    contact=contact,
                    direction='',
                    content=contact,
                    content_type='application/sylk-conversation-remove',
                    timestamp=timestamp,
                    disposition_notification='',
                    message_id=str(uuid.uuid4()))

        content = sylkrtc.AccountConversationRemoveEventData(contact=contact, timestamp=timestamp)
        event = sylkrtc.AccountSyncEvent(account=account_info.id, type='conversation', action='remove', content=content)
        self._fork_event_to_online_accounts(account_info, event)

        self._send_simple_sip_message(contact, account_info.id, json.dumps(content.__data__), 'application/sylk-conversation-remove')

    def _RH_account_fetch_addressbook(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        addressbook = defer.maybeDeferred(get_addressbook, account_info)
        addressbook.addCallback(lambda result: self.send(sylkrtc.AccountAddressBookFetchedEvent(addressbook=result, account=account_info.id)))
        return addressbook

    def _RH_account_update_addressbook(self, request):
        try:
            account_info = self.accounts_map[request.account]
        except KeyError:
            raise APIError('Unknown account specified: {request.account}'.format(request=request))

        if not account_info.auth_state:
            raise APIError("Account not authenticated")

        def addressbook_updated(result):
            event = sylkrtc.AccountAddressBookUpdatedEvent(data=result,
                                                           action=request.action,
                                                           type=request.type,
                                                           account=account_info.id)
            self.send(event)
            self._fork_event_to_online_accounts(account_info, event)

        update = defer.maybeDeferred(update_addressbook, account_info, request)
        update.addCallback(addressbook_updated)
        update.addErrback(lambda failure: self.send(sylkrtc.AccountAddressBookUpdateFailedEvent(error=str(failure.value),
                                                                                                type=request.type,
                                                                                                action=request.action,
                                                                                                account=account_info.id,
                                                                                                id=request.data.id)))

        return update

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
        headers = {'headers': request.headers.__data__} if request.headers is not None else {}
        try:
            janus_handle.call(account_info, uri=request.uri, sdp=request.sdp, proxy=proxy, **headers)
        except Exception:
            janus_handle.detach()
            raise

        session_info = SIPSessionInfo(request.session)
        session_info.janus_handle = janus_handle
        session_info.init_outgoing(account_info, request.uri)
        self.sip_sessions.add(session_info)

        self.log.info('outgoing session {request.session} to {request.uri}'.format(request=request))

    def _RH_session_answer(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session {request.session}'.format(request=request))

        if session_info.direction != 'incoming':
            raise APIError('Cannot answer outgoing session {request.session}'.format(request=request))
        if session_info.state != 'connecting':
            raise APIError('Invalid state for answering session {session.id}: {session.state}'.format(session=session_info))

        headers = {'headers': request.headers.__data__} if request.headers is not None else {}
        session_info.janus_handle.accept(sdp=request.sdp, **headers)
        self.log.info('incoming session {session.id} answered'.format(session=session_info))

    def _RH_session_trickle(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session {request.session}'.format(request=request))

        if session_info.state == 'terminated':
            raise APIError('Session {request.session} is terminated'.format(request=request))

        session_info.janus_handle.trickle(request.candidates)

        if not request.candidates:
            self.log.debug('session {session.id} negotiated ICE'.format(session=session_info))

    def _RH_session_terminate(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session {request.session}'.format(request=request))

        if session_info.state not in ('connecting', 'progress', 'early_media', 'accepted', 'established', 'ringing', 'local-updating', 'remote-updating'):
            raise APIError('Invalid state for terminating session {session.id}: {session.state}'.format(session=session_info))

        if session_info.direction == 'incoming' and session_info.state == 'connecting':
            session_info.janus_handle.decline(self.decline_code)
        else:
            session_info.janus_handle.hangup()
        self.log.info('{session.direction} session {session.id} will terminate'.format(session=session_info))

    def _RH_session_update(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session {request.session}'.format(request=request))

        if session_info.state == 'established':
            jsep_type = 'offer'
            session_info.state = 'local-updating'
        elif session_info.state == 'remote-updating':
            jsep_type = 'answer'
        else:
            raise APIError(
                'Invalid state for updating session {session.id}: {session.state}'.format(
                    session=session_info))

        headers = {'headers': request.headers.__data__} if request.headers is not None else {}
        try:
            session_info.janus_handle.update(sdp=request.sdp, jsep_type=jsep_type, **headers)
        except Exception:
            if jsep_type == 'offer':
                session_info.state = 'established'
            raise

        self.log.info('{direction} session {id} sent {jsep} update'.format(
            direction=session_info.direction, id=session_info.id, jsep=jsep_type))

    def _RH_session_message(self, request):
        self.log.info("Sending janus in dialog message")
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session {request.session}'.format(request=request))

        if session_info.state not in ('established'):
            raise APIError('Invalid state session {session.id}: {session.state} for sending messages'.format(session=session_info))

        self._send_in_dialog_sip_message(session_info, message_id=request.message_id, content=request.content, content_type=request.content_type, timestamp=request.timestamp)

    def _RH_session_dtmf_info(self, request):
        try:
            session_info = self.sip_sessions[request.session]
        except KeyError:
            raise APIError('Unknown session {request.session}'.format(request=request))

        if session_info.state not in ('established', 'accepted', 'early_media', 'local-updating', 'remote-updating'):
            raise APIError(
                'Invalid state session {session.id}: {session.state} for DTMF info'.format(
                    session=session_info))

        duration = request.duration if request.duration else None
        session_info.janus_handle.send_dtmf_info(digit=request.digit, duration=duration)
        self.log.debug('{session.direction} session {session.id} send DTMF INFO {digit} {duration}'.format(session=session_info, digit=request.digit, duration=duration))

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
            videoroom = Videoroom(request.uri, request.audio, request.video)
            self.protocol.factory.videorooms.add(videoroom)

        if not videoroom.allow_uri(request.account):
            self._maybe_destroy_videoroom(videoroom)
            raise APIError('is not allowed to join room {request.uri}'.format(request=request))

        if ('m=video' in request.sdp and 'm=audio' in request.sdp):
            media = 'audio/video'
        elif ('m=video' in request.sdp):
            media = 'video only'
        elif ('m=audio' in request.sdp):
            media = 'audio only'
        else:
            media = 'unknown'

        try:
            videoroom_handle = VideoroomPluginHandle(self.janus_session, event_handler=self._handle_janus_videoroom_event)

            try:
                try:
                    videoroom_handle.create(room=videoroom.id, config=videoroom.config, publishers=10)
                except JanusError as e:
                    if e.code != 427:  # 427 means room already exists
                        raise
                else:
                    self.log.info('created room {room}'.format(room=request.uri))
                videoroom_handle.join(room=videoroom.id, sdp=request.sdp, display_name=account_info.display_name, audio=videoroom.audio, video=videoroom.video)
            except Exception:
                videoroom_handle.detach()
                raise
        except Exception:
            self._maybe_destroy_videoroom(videoroom)
            raise

        videoroom_session = VideoroomSessionInfo(request.session, owner=self, janus_handle=videoroom_handle)
        videoroom_session.init_publisher(account=account_info, room=videoroom)
        self.log.info('publish {media} to room {room}'.format(room=request.uri, media=media))
        self.videoroom_sessions.add(videoroom_session)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=videoroom_session.chat_handler)
        videoroom_session.chat_handler.start()

        self.send(sylkrtc.VideoroomSessionProgressEvent(session=videoroom_session.id))

    def _RH_videoroom_leave(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))

        videoroom_session.janus_handle.leave()

        self.send(sylkrtc.VideoroomSessionTerminatedEvent(session=videoroom_session.id))

        # safety net in case we do not get any answer for the leave request
        # todo: to be adjusted later after pseudo-synchronous communication with janus is implemented
        reactor.callLater(2, call_in_green_thread, self._cleanup_videoroom_session, videoroom_session)

        self.log.debug('leaving room {session.room.uri}'.format(session=videoroom_session))

    def _RH_videoroom_configure(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        videoroom = videoroom_session.room
        # todo: should we send out events if the active participant list did not change?
        try:
            videoroom.active_participants = request.active_participants
        except ValueError as e:
            raise APIError(str(e))
        for session in videoroom:
            session.owner.send(sylkrtc.VideoroomConfigureEvent(session=session.id, active_participants=videoroom.active_participants, originator=request.session))

    def _RH_videoroom_feed_attach(self, request):
        # sent when a feed is subscribed for a given publisher
        if request.feed in self.videoroom_sessions:
            raise APIError('Video room session ID {request.feed} already in use'.format(request=request))

        try:
            base_session = self.videoroom_sessions[request.session]  # our 'base' session (the one used to join and publish)
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))

        try:
            publisher_session = base_session.room[request.publisher]
        except KeyError:
            try:
                janus_publisher_id = int(request.publisher)
            except (TypeError, ValueError):
                raise APIError('Unknown publisher room session to attach to: {request.publisher}'.format(request=request))
            publisher_session = ExternalPublisherSession(
                id=request.publisher,
                publisher_id=janus_publisher_id,
                room=base_session.room,
            )
            self.log.info('attaching to external publisher {publisher.id} (janus_id={publisher.publisher_id})'.format(publisher=publisher_session))
        if publisher_session.publisher_id is None:
            raise APIError('Video room session {session.id} does not have a publisher ID'.format(session=publisher_session))

        videoroom_handle = VideoroomPluginHandle(self.janus_session, event_handler=self._handle_janus_videoroom_event)

        try:
            videoroom_handle.feed_attach(room=base_session.room.id, feed=publisher_session.publisher_id, offer_audio=base_session.room.audio, offer_video=base_session.room.video)
        except Exception:
            videoroom_handle.detach()
            raise

        videoroom_session = VideoroomSessionInfo(request.feed, owner=self, janus_handle=videoroom_handle)
        videoroom_session.init_subscriber(publisher_session, parent_session=base_session)
        self.videoroom_sessions.add(videoroom_session)
        # Discard a previous feed entry for the same publisher (id or
        # janus publisher_id) so a re-subscribe doesn't trip the assert
        # in PublisherFeedContainer.add. Happens when a client re-attaches
        # to the same publisher after a transient client-side event.
        try:
            base_session.feeds.discard(publisher_session.id)
        except Exception:
            pass
        try:
            base_session.feeds.discard(publisher_session.publisher_id)
        except Exception:
            pass
        base_session.feeds.add(publisher_session)
        self.log.debug('subscribe to {account} in room {session.room.uri} {feeds}'.format(account=publisher_session.account.id, session=videoroom_session, feeds=len(base_session.feeds)))

    def _RH_videoroom_feed_answer(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.feed]
        except KeyError:
            raise APIError('Unknown room session: {request.feed}'.format(request=request))
        if videoroom_session.parent_session.id != request.session:
            raise APIError('{request.feed} is not an attached feed of {request.session}'.format(request=request))

        if ('m=video' in request.sdp and 'm=audio' in request.sdp):
            media = 'audio/video'
        elif ('m=video' in request.sdp):
            media = 'video only'
        elif ('m=audio' in request.sdp):
            media = 'audio only'
        else:
            media = 'unknown'

        self.log.debug('{media} media accepted by room {session.room.uri}'.format(media=media, session=videoroom_session))
        videoroom_session.janus_handle.feed_start(sdp=request.sdp)

    def _RH_videoroom_feed_detach(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.feed]
        except KeyError:
            raise APIError('Unknown room session to detach: {request.feed}'.format(request=request))
        if videoroom_session.parent_session.id != request.session:
            raise APIError('{request.feed} is not an attached feed of {request.session}'.format(request=request))
        videoroom_session.janus_handle.feed_detach()
        # safety net in case we do not get any answer for the feed_detach request
        # todo: to be adjusted later after pseudo-synchronous communication with janus is implemented
        try:
            publisher_account = videoroom_session.room[videoroom_session.publisher_id].account.id
        except KeyError:
            publisher_account = 'janus:{}'.format(videoroom_session.publisher_id)
        self.log.debug('unsubscribe from {account} in room {session.room.uri}'.format(account=publisher_account, session=videoroom_session))
        reactor.callLater(2, call_in_green_thread, self._cleanup_videoroom_session, videoroom_session)

    def _RH_videoroom_invite(self, request):
        try:
            base_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        room = base_session.room
        participants = set(request.participants)
        originator = sylkrtc.SIPIdentity(uri=base_session.account.id, display_name=base_session.account.display_name)
        session_id = str(random.getrandbits(32))
        event = sylkrtc.AccountConferenceInviteEvent(account='placeholder', room=room.uri, originator=originator, session_id=self._callid_to_uuid(session_id))
        for protocol in self.protocol.factory.connections.difference([self.protocol]):
            connection_handler = protocol.connection_handler
            for account in participants.intersection(connection_handler.accounts_map):
                event.account = account
                connection_handler.send(event)
                room.log.info('invitation from %s for %s', originator.uri, account)
                room.log.debug('invitation from %s for %s with session-id %s', originator.uri, account, session_id)
                connection_handler.log.info('received an invitation from %s for %s to join room %s', originator.uri, account, room.uri)
        for participant in participants.difference([base_session.account.id]):
            if not any(session.account.id == participant for session in base_session.room):
                push.conference_invite(originator=originator, destination=participant, room=room.uri, call_id=session_id, audio=room.audio, video=room.video)

        chat_handler = base_session.chat_handler
        if chat_handler is None or chat_handler.sip_session is None:
            room.log.debug('skipping SIP REFER for invitees: chat session not yet established')
        elif not chat_handler.sip_session.remote_focus:
            room.log.debug('skipping SIP REFER for invitees: remote party is not a SIP focus')
        else:
            try:
                focus_uri = SIPURI.new(chat_handler.sip_session.remote_identity.uri)
            except SIPCoreError as e:
                room.log.warning('skipping SIP REFER for invitees: focus URI unresolved: {}'.format(e))
                focus_uri = None
            if focus_uri is not None:
                base_session_id = base_session.id
                base_session_owner = base_session.owner
                for participant in participants.difference([base_session.account.id]):
                    participant_str = participant if participant.startswith(('sip:', 'sips:')) else 'sip:{}'.format(participant)
                    try:
                        participant_uri = SIPURI.parse(participant_str)
                    except SIPCoreError:
                        room.log.warning('skipping SIP REFER for invitee {}: invalid URI'.format(participant))
                        continue
                    room.log.info('referring {} to SIP focus {} for room {}'.format(participant_uri, focus_uri, room.uri))

                    def _status_cb(p_uri, state, code, reason, _sid=base_session_id, _owner=base_session_owner, _room=room):
                        try:
                            _owner.send(sylkrtc.VideoroomInviteStatusEvent(
                                session=_sid,
                                participant=p_uri,
                                state=state,
                                code=code if code is not None else 0,
                                reason=reason or '',
                            ))
                        except Exception as e:
                            _room.log.warning('failed to forward invite-status event for {}: {}'.format(p_uri, e))

                    SipFocusReferralHandler(focus_uri, participant_uri, base_session.account, room.log, status_callback=_status_cb).start()

    def _RH_videoroom_remove(self, request):
        # Per-URI routing: send REFER ;method=BYE (RFC 4579) for SIP
        # participants — the conference focus BYEs them out of the
        # room — and a Janus videoroom kick request for WebRTC
        # participants (no SIP signalling for them; the kick goes
        # plugin-to-plugin). The client sends a single
        # videoroom-remove regardless of participant type; the gateway
        # picks the right primitive per URI by looking the URI up in
        #   - room publishers      → WebRTC, Janus kick
        #   - everything else      → SIP, REFER ;method=BYE
        # Used for client-driven kicks (per-tile hangup button); the
        # last-WebRTC-publisher cleanup in _kick_all_sip_participants_blocking
        # is a separate SIP-only path that doesn't need this routing.
        try:
            base_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        room = base_session.room
        participants = set(request.participants)

        # Build the WebRTC publisher lookup table once. Keys are the
        # AoR-stripped lower-cased account.id (so URIs sent by the
        # client with sip:/sips: prefix or ;params still match).
        def _aor(u):
            s = str(u)
            if s.startswith('sip:'):
                s = s[4:]
            elif s.startswith('sips:'):
                s = s[5:]
            return s.split(';', 1)[0].lower()
        webrtc_publishers = {}
        for session in room:
            if session.type != 'publisher' or session.account is None:
                continue
            webrtc_publishers[_aor(session.account.id)] = session

        base_session_id = base_session.id
        base_session_owner = base_session.owner

        def _status_cb_factory(p_uri):
            def _status_cb(p_uri_inner, state, code, reason, _sid=base_session_id, _owner=base_session_owner, _room=room):
                try:
                    _owner.send(sylkrtc.VideoroomInviteStatusEvent(
                        session=_sid,
                        participant=p_uri_inner,
                        state=state,
                        code=code if code is not None else 0,
                        reason=reason or '',
                    ))
                except Exception as e:
                    _room.log.warning('failed to forward remove-status event for {}: {}'.format(p_uri_inner, e))
            return _status_cb

        for participant in participants.difference([base_session.account.id]):
            participant_aor = _aor(participant)

            # WebRTC branch — Janus kick. The base_session's janus_handle
            # is in the same room as the kick target, so a kick request
            # on it removes the named publisher.
            if participant_aor in webrtc_publishers:
                target = webrtc_publishers[participant_aor]
                publisher_id = getattr(target, 'publisher_id', None)
                if publisher_id is None:
                    room.log.warning('skipping Janus kick for {}: no publisher_id'.format(participant))
                    continue
                room.log.info('kicking WebRTC publisher {} (pid={}) from room {}'.format(participant, publisher_id, room.uri))
                try:
                    base_session.janus_handle.kick_publisher(room.id, publisher_id)
                except Exception as e:
                    room.log.warning('Janus kick threw for {}: {}'.format(participant, e))
                    continue

                # Janus only sends the 'kicked' event back to US (the
                # moderator) as an ack — it does NOT notify the kicked
                # publisher on their own handle. So Janus stops their
                # media but their gateway-side videoroom session AND
                # the client-side Conference object stay alive,
                # leaving the kicked user's phone silently connected.
                # Drive the teardown from this side: send the
                # terminated event to the target's WebSocket client
                # and clean up the target's gateway state. The room
                # iteration above gave us the target's session object,
                # whose `.owner` is the target's ConnectionHandler.
                target_owner = getattr(target, 'owner', None)
                if target_owner is not None:
                    try:
                        target_owner.send(sylkrtc.VideoroomSessionTerminatedEvent(
                            session=target.id, reason='kicked'))
                    except Exception as e:
                        room.log.warning('failed to forward kicked event to {}: {}'.format(participant, e))
                    try:
                        target_owner._cleanup_videoroom_session(target)
                    except Exception as e:
                        room.log.warning('cross-connection cleanup of kicked session for {} failed: {}'.format(participant, e))

                # Update the moderator's own roster. Janus sends the
                # 'kicked' event as an ack to the moderator INSTEAD of
                # the 'leaving' event other publishers receive when a
                # peer drops, so the normal _EH_janus_videoroom_event_leaving
                # path that emits VideoroomPublishersLeftEvent doesn't
                # fire on this side. Walk our own feeds (subscriptions
                # to the kicked publisher) to clean them up, then send
                # the publishers-left event so the moderator's client
                # removes the tile from its grid.
                try:
                    departed_subscriber = base_session.feeds.pop(publisher_id)
                    departed_id = departed_subscriber.id
                except KeyError:
                    departed_id = str(publisher_id)
                try:
                    self.send(sylkrtc.VideoroomPublishersLeftEvent(
                        session=base_session.id, publishers=[departed_id]))
                except Exception as e:
                    room.log.warning('failed to push publishers-left to moderator for {}: {}'.format(participant, e))

                # Surface "200 OK" on the issuer's UI so the tile
                # reconciles to "removed" right away.
                try:
                    _status_cb_factory(participant)(participant, 'success', 200, 'OK')
                except Exception:
                    pass
                continue

            # SIP branch — REFER ;method=BYE.
            chat_handler = base_session.chat_handler
            if chat_handler is None or chat_handler.sip_session is None:
                room.log.debug('skipping SIP REFER ;method=BYE for {}: chat session not yet established'.format(participant))
                continue
            if not chat_handler.sip_session.remote_focus:
                room.log.debug('skipping SIP REFER ;method=BYE for {}: remote party is not a SIP focus'.format(participant))
                continue
            try:
                focus_uri = SIPURI.new(chat_handler.sip_session.remote_identity.uri)
            except SIPCoreError as e:
                room.log.warning('skipping SIP REFER ;method=BYE for {}: focus URI unresolved: {}'.format(participant, e))
                continue
            participant_str = participant if participant.startswith(('sip:', 'sips:')) else 'sip:{}'.format(participant)
            try:
                participant_uri = SIPURI.parse(participant_str)
            except SIPCoreError:
                room.log.warning('skipping SIP REFER ;method=BYE for {}: invalid URI'.format(participant))
                continue
            room.log.info('removing SIP participant {} via REFER ;method=BYE from focus {} for room {}'.format(participant_uri, focus_uri, room.uri))
            SipFocusReferralHandler(focus_uri, participant_uri, base_session.account, room.log,
                                    status_callback=_status_cb_factory(participant), method='BYE').start()

    def _RH_videoroom_mute_participant(self, request):
        """Dispatch a per-participant mute request to the right backend.

        Two paths, picked from the room's `webrtc_participants_by_pid`
        map (rebuilt from every conference-info NOTIFY):

        1. WebRTC peer — `participant_id` resolves to a publisher session
           we own a WS connection to. Send a `mute-request` event over
           that connection so the recipient mutes its mic at the source
           and updates its local UI. Nothing is forwarded to the focus
           in this path: source-level mute IS the authoritative state
           for a WebRTC peer, and the recipient stays in control.

        2. SIP / bridge participant — no local WS session exists for the
           target. Send a SIP REFER ;method=MUTE / UNMUTE to the
           conference focus we already have a chat session with. The
           focus's conference application (see
           IncomingReferralHandler in conference/__init__.py) handles
           the method, identifies the target by Refer-To AoR with the
           optional ;participant_id=<token> override for multi-device
           disambiguation, and applies set_participant_muted() on the
           room. This replaces the earlier admin-HTTP-POST path that
           failed in deployments where the focus's admin endpoint is
           bound to a private IP unreachable from the webrtcgateway.
        """
        try:
            base_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        room = base_session.room
        # Path 1: local WebRTC peer.
        pid = str(getattr(request, 'participant_id', '') or '')
        target_session = None
        if pid:
            try:
                target_session = room.webrtc_participants_by_pid.get(pid)
            except AttributeError:
                target_session = None
        if target_session is not None:
            try:
                target_session.owner.send(sylkrtc.VideoroomMuteRequestEvent(
                    session=target_session.id,
                    muted=bool(request.muted),
                    originator=request.session,
                ))
                room.log.info('mute dispatch (webrtc): {} muted={} via session {}'.format(
                    pid, bool(request.muted), target_session.id))
            except Exception as e:
                room.log.warning('mute dispatch (webrtc): {} muted={} failed: {}'.format(
                    pid, bool(request.muted), e))
            return
        # Path 2: SIP / bridge participant — REFER to the focus.
        chat_handler = base_session.chat_handler
        if chat_handler is None or chat_handler.sip_session is None:
            raise APIError('Conference chat session not yet established')
        if not chat_handler.sip_session.remote_focus:
            raise APIError('Remote party is not a SIP focus')
        try:
            focus_uri = SIPURI.new(chat_handler.sip_session.remote_identity.uri)
        except SIPCoreError as e:
            raise APIError('Focus URI unresolved: {}'.format(e))
        # Resolve a participant SIP URI from the NOTIFY-derived map so
        # the Refer-To carries something honest. With multiple devices
        # behind one AoR `participant_id` is still the authoritative
        # disambiguator (carried as a Refer-To parameter below), so a
        # missing entry is not fatal — we fall back to the focus's own
        # AoR and let the conference handler key off participant_id.
        participant_uri_str = None
        try:
            participant_uri_str = room.participant_uris_by_pid.get(pid)
        except AttributeError:
            participant_uri_str = None
        if not participant_uri_str:
            # Synthesise a Refer-To target that's at least syntactically
            # valid. The participant_id parameter is what the focus will
            # use for the actual lookup.
            participant_uri_str = str(focus_uri)
        if not participant_uri_str.lower().startswith(('sip:', 'sips:')):
            participant_uri_str = 'sip:{}'.format(participant_uri_str)
        try:
            participant_uri = SIPURI.parse(participant_uri_str)
        except SIPCoreError:
            raise APIError('Invalid participant URI: {!r}'.format(participant_uri_str))
        refer_method = 'MUTE' if bool(request.muted) else 'UNMUTE'
        room.log.info('referring {} ;method={} (pid={}) to SIP focus {} for room {}'.format(
            participant_uri, refer_method, pid, focus_uri, room.uri))

        def _status_cb(target, state, code, reason):
            if state == 'failed':
                room.log.warning('mute REFER for {} pid={} failed: {} {}'.format(
                    target, pid, code or '', reason or ''))
            else:
                room.log.info('mute REFER for {} pid={} state={} code={} reason={}'.format(
                    target, pid, state, code, reason))

        SipFocusReferralHandler(
            focus_uri,
            participant_uri,
            base_session.account,
            room.log,
            status_callback=_status_cb,
            method=refer_method,
            refer_to_extra_params={'participant_id': pid} if pid else None,
        ).start()

    def _RH_videoroom_session_trickle(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        videoroom_session.janus_handle.trickle(request.candidates)
        if not request.candidates and videoroom_session.type == 'publisher':
            self.log.debug('ICE negotiation to room {session.room.uri} completed'.format(session=videoroom_session))

    def _RH_videoroom_session_update(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        options = request.options.__data__
        if options:
            videoroom_session.janus_handle.update_publisher(options)
            modified = ', '.join('{}={}'.format(key, options[key]) for key in options)
            media = 'video'

            try:
                has_video = options['video']
            except KeyError:
                pass
            else:
                if not has_video:
                    media = 'audio only'

            try:
                publisher_account = videoroom_session.room[videoroom_session.publisher_id].account.id
            except KeyError:
                publisher_account = 'janus:{}'.format(videoroom_session.publisher_id)
            self.log.info('switched to {media} media to {account} in room {session.room.uri}'.format(account=publisher_account, session=videoroom_session, media=media))

    def _RH_videoroom_message(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        content_type = request.content_type
        content = request.content if content_type.startswith('text') else request.content.encode('latin1')
        message_id = request.message_id
        videoroom_session.chat_handler.send_message(message_id, content, content_type)

    def _RH_videoroom_composing_indication(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        videoroom_session.chat_handler.send_composing_indication(request.state, request.refresh)

    def _RH_videoroom_mute_audio_participants(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        videoroom = videoroom_session.room
        # Step 1: tell every WebRTC publisher in the room to mute its
        # own microphone (existing behaviour — the recipient client's
        # mute-audio handler will toggle the local mic at the source).
        for session in videoroom:
            session.owner.send(sylkrtc.VideoroomMuteAudioEvent(session=session.id, originator=request.session))
        # Step 2: for SIP-side participants (anyone in the room who
        # is not a WebRTC publisher and not the audio bridge itself),
        # send REFER ;method=MUTE to the conference focus through the
        # existing SipFocusReferralHandler path. The bridge advertises
        # its own pid on the room (`bridge_participant_id`); WebRTC
        # publishers were already addressed in step 1 above and live
        # in `room.webrtc_participants_by_pid`. Anything else in
        # `room.participant_uris_by_pid` is a real SIP caller behind
        # the bridge that won't get muted by the WebRTC mute-audio
        # broadcast. We need the focus URI from the chat handler's
        # SIP session for the referrer side of the REFER; bail out
        # quietly when the chat session isn't established yet (the
        # WebRTC mute portion has already done its work in that case).
        chat_handler = videoroom_session.chat_handler
        if chat_handler is None or chat_handler.sip_session is None:
            videoroom.log.debug('mute-all: chat session not ready; SIP-side REFERs skipped')
            return
        if not chat_handler.sip_session.remote_focus:
            videoroom.log.debug('mute-all: remote party is not a SIP focus; SIP-side REFERs skipped')
            return
        try:
            focus_uri = SIPURI.new(chat_handler.sip_session.remote_identity.uri)
        except SIPCoreError as e:
            videoroom.log.warning('mute-all: focus URI unresolved: {}'.format(e))
            return
        webrtc_pids = set()
        try:
            webrtc_pids = set(videoroom.webrtc_participants_by_pid.keys())
        except AttributeError:
            webrtc_pids = set()
        bridge_pid = getattr(videoroom, 'bridge_participant_id', None)
        pid_to_uri = {}
        try:
            pid_to_uri = dict(videoroom.participant_uris_by_pid)
        except AttributeError:
            pid_to_uri = {}
        referred = 0
        for pid, uri_str in pid_to_uri.items():
            if not pid or not uri_str:
                continue
            if pid in webrtc_pids:
                continue  # already covered by mute-audio broadcast
            if bridge_pid and pid == bridge_pid:
                continue  # the bridge itself isn't a participant
            if not uri_str.lower().startswith(('sip:', 'sips:')):
                uri_str = 'sip:{}'.format(uri_str)
            try:
                participant_uri = SIPURI.parse(uri_str)
            except SIPCoreError:
                videoroom.log.warning('mute-all: skipping SIP pid={} (invalid URI {!r})'.format(pid, uri_str))
                continue
            videoroom.log.info('mute-all: referring {} ;method=MUTE (pid={}) to focus {}'.format(
                participant_uri, pid, focus_uri))
            try:
                SipFocusReferralHandler(
                    focus_uri,
                    participant_uri,
                    videoroom_session.account,
                    videoroom.log,
                    method='MUTE',
                    refer_to_extra_params={'participant_id': pid},
                ).start()
                referred += 1
            except Exception as e:
                videoroom.log.warning('mute-all: REFER MUTE for pid={} failed: {}'.format(pid, e))
        if referred:
            videoroom.log.info('mute-all: dispatched {} SIP-side REFER MUTE(s)'.format(referred))

    def _RH_videoroom_toggle_hand(self, request):
        try:
            videoroom_session = self.videoroom_sessions[request.session]
        except KeyError:
            raise APIError('Unknown room session: {request.session}'.format(request=request))
        videoroom = videoroom_session.room
        if request.session_id:
            request_session = request.session_id
        else:
            request_session = request.session
        videoroom.raised_hands = request_session
        for session in videoroom:
            session.owner.send(sylkrtc.VideoroomRaisedHandsEvent(session=session.id, raised_hands=videoroom.raised_hands))

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
        self.log.info('{session.direction} session {session.id} established'.format(session=session_info))

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
                self.log.debug('poor download connectivity for session {session.id}'.format(session=session_info))
            session_info.slow_download = True
        else:
            if not session_info.slow_upload:
                self.log.debug('poor upload connectivity for session {session.id}'.format(session=session_info))
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
            account_info.auth_state = True


            helper = SIPPluginHandle(self.janus_session, event_handler=self._handle_janus_sip_event)
            master_id = event.plugindata.data.result.master_id
            helper.register_helper(account_info, master_id=master_id)
            self.account_handles_map[helper.id] = account_info
            account_info.janus_helpers.append(helper)

            self.send(sylkrtc.AccountRegisteredEvent(account=account_info.id))
            self.log.info('registered')
            storage = MessageStorage()
            storage.add_account(account=account_info.id)
            if 'pn_app' in account_info.contact_params:
                token_storage = TokenStorage()
                token_storage.add(account_info.id, account_info.contact_params, account_info.user_agent)

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
            self.log.info('registration failed: {reason}'.format(reason=reason))

    def _EH_janus_sip_event_incomingcall(self, event):
        # Janus re-emits 'incomingcall' for in-dialog re-INVITEs in
        # some builds (instead of using a separate 'updatingcall'
        # event). Detect that case by checking whether event.sender
        # corresponds to an already-known session handle — if so,
        # this is a mid-call renegotiation and we route through the
        # same logic as the explicit 'updatingcall' event handler.
        # Without this guard, an in-dialog re-INVITE would land here,
        # fall through the account_handles_map lookup with a warning,
        # and the client (callee) would never get a SessionUpdateEvent
        # — symptom on the wire: A's session-update gets accepted, B's
        # PeerConnection is never told to expect new media, A's video
        # frames arrive but B has no receiver wired to render them.
        if event.sender in self.sip_sessions:
            self.log.info('incomingcall on existing session — routing as re-INVITE')
            return self._EH_janus_sip_event_updatingcall(event)
        try:
            account_info = self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for incoming call event'.format(event=event))
            return
        assert event.jsep is not None
        data = event.plugindata.data.result  # type: janus.SIPResultIncomingCall
        call_id = event.plugindata.data.call_id
        originator = sylkrtc.SIPIdentity(uri=data.username, display_name=data.displayname)
        headers = {'headers': data.headers} if data.headers else {}
        session = SIPSessionInfo(self._callid_to_uuid(call_id))

        handle = None
        if event.sender == account_info.janus_handle.id:
            handle = account_info.janus_handle
        else:
            handle = next((helper for helper in account_info.janus_helpers if helper.id == event.sender),None)
        if handle:
            session.janus_handle = handle
        else:
            self.log.warning('could not find handle ID {event.sender} for incoming call event'.format(event=event))
            return

        session.init_incoming(account_info, originator.uri, originator.display_name)
        self.sip_sessions.add(session)
        self.send(sylkrtc.AccountIncomingSessionEvent(account=account_info.id, session=session.id, originator=originator, sdp=event.jsep.sdp, call_id=call_id, **headers))
        self.log.info('incoming session {session.id} from {session.remote_identity.uri!s}'.format(session=session))

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

        if session_info.state in ('local-updating', 'remote-updating'):
            previous_state = session_info.state
            session_info.state = 'established'
            if event.jsep is not None:
                self.send(sylkrtc.SessionUpdateEvent(session=session_info.id, state='accepted', sdp=event.jsep.sdp))
                self.log.info('{session.direction} session {session.id} update accepted ({prev}→established)'.format(
                    session=session_info, prev=previous_state))
            else:
                self.log.info('{session.direction} session {session.id} update completed without JSEP (was {prev})'.format(
                    session=session_info, prev=previous_state))
            return

        if session_info.state == 'established' or session_info.state == 'early_media':  # We had early media
            session_info.state = 'accepted'
            self.send(sylkrtc.SessionAcceptedEvent(session=session_info.id))
            self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))
            return

        session_info.state = 'accepted'
        if session_info.direction == 'outgoing':
            assert event.jsep is not None
            data = event.plugindata.data.result  # type: janus.SIPResultAccepted
            headers = {'headers': data.headers} if data.headers else {}
            self.send(sylkrtc.SessionAcceptedEvent(session=session_info.id, sdp=event.jsep.sdp, call_id=event.plugindata.data.call_id, **headers))
        else:
            self.send(sylkrtc.SessionAcceptedEvent(session=session_info.id))
        self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))

    def _EH_janus_sip_event_updating(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            return
        self.log.debug('{session.direction} session {session.id} update in progress'.format(session=session_info))

    def _EH_janus_sip_event_updatingcall(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for updatingcall event'.format(event=event))
            return
        if event.jsep is None:
            self.log.warning('updatingcall event without JSEP for session {session.id}'.format(session=session_info))
            return
        session_info.state = 'remote-updating'
        self.send(sylkrtc.SessionUpdateEvent(session=session_info.id, state='received', sdp=event.jsep.sdp))
        self.log.info('{session.direction} session {session.id} got remote re-INVITE'.format(session=session_info))

    def _EH_janus_sip_event_updated(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for updated event'.format(event=event))
            return
        previous_state = session_info.state
        session_info.state = 'established'
        if event.jsep is not None:
            self.send(sylkrtc.SessionUpdateEvent(session=session_info.id, state='accepted', sdp=event.jsep.sdp))
            self.log.info('{session.direction} session {session.id} update accepted'.format(session=session_info))
        else:
            self.log.info('{session.direction} session {session.id} update completed (was {state})'.format(session=session_info, state=previous_state))

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
        data = event.plugindata.data.result  # type: janus.SIPResultMessage

        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            return

        self.send(sylkrtc.ProceedingEvent(session=session_info.id, code=data.code))

    def _EH_janus_sip_event_progress(self, event):
        if (event.jsep):
            try:
                session_info = self.sip_sessions[event.sender]
            except KeyError:
                self.log.warning('could not find SIP session with handle ID {event.sender} for progress event'.format(event=event))
                return
            session_info.state = 'early_media'
            self.log.info('{session.direction} session {session.id} has early media'.format(session=session_info))
            self.send(sylkrtc.SessionEarlyMediaEvent(session=session_info.id, sdp=event.jsep.sdp, call_id=event.plugindata.data.call_id))
            self.log.debug('{session.direction} session {session.id} state: {session.state}'.format(session=session_info))

    def _EH_janus_sip_event_ringing(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            return

        self.send(sylkrtc.RingingEvent(session=session_info.id))

    def _EH_janus_sip_event_message(self, event):
        data = event.plugindata.data.result  # type: janus.SIPResultMessage

        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            return

        if not event.plugindata.data.call_id:
            return

        cpim_message = None
        if data.content_type in ("application/im-iscomposing+xml", "text/pgp-public-key"):
            return
        elif data.content_type == "message/cpim":
            try:
                content = data.content if isinstance(data.content, str) else data.content.decode('latin1')
                cpim_message = CPIMPayload.decode(content.encode('utf-8'))
            except CPIMParserError:
                self.log.info('message rejected: CPIM parse error')
                return
            else:
                body = cpim_message.content if isinstance(cpim_message.content, str) else cpim_message.content.decode()
                content_type = cpim_message.content_type
                sender = cpim_message.sender or FromHeader(SIPURI.parse('{}'.format(data.sender)), data.displayname)
                disposition = next(([item.strip() for item in header.value.split(',')] for header in cpim_message.additional_headers if header.name == 'Disposition-Notification'), None)
                message_id = next((header.value for header in cpim_message.additional_headers if header.name == 'Message-ID'), None)
        else:
            body = data.content
            content_type = data.content_type
            sender = FromHeader(SIPURI.parse('{}'.format(data.sender)), data.displayname)
            disposition = None
            message_id = str(uuid.uuid4())

        timestamp = str(cpim_message.timestamp) if cpim_message is not None and cpim_message.timestamp is not None else str(ISOTimestamp.now())
        sender = sylkrtc.SIPIdentity(uri=str(sender.uri), display_name=sender.display_name)

        if content_type in ("application/im-iscomposing+xml", "text/pgp-public-key"):
            return

        if content_type == IMDNDocument.content_type:
            document = IMDNDocument.parse(body)
            imdn_message_id = document.message_id.value
            imdn_status = document.notification.status.__str__()
            imdn_datetime = document.datetime.__str__()
            self.log.info('received in dialog IMDN message ({status}) from: {originator.uri}'.format(status=imdn_status, originator=sender))

            self.send(sylkrtc.SessionMessageDispositionNotificationEvent(session=session_info.id,
                                                                         state=imdn_status,
                                                                         message_id=imdn_message_id,
                                                                         message_timestamp=imdn_datetime,
                                                                         timestamp=timestamp,
                                                                         code=200,
                                                                         reason=''))
        else:
            self.log.info('received in dialog message ({content_type}) from: {originator.uri}'.format(content_type=content_type, originator=sender))

            self.send(sylkrtc.SessionMessageEvent(session=session_info.id,
                                                  sender=sender,
                                                  content=body,
                                                  content_type=content_type,
                                                  timestamp=timestamp,
                                                  disposition_notification=disposition,
                                                  message_id=message_id))

    def _EH_janus_sip_event_messagesent(self, event):
        pass

    def _EH_janus_sip_event_messagedelivery(self, event):
        try:
            session_info = self.sip_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find SIP session with handle ID {event.sender} for delivery event'.format(event=event))
            return

        data = event.plugindata.data.result
        message_id, content, content_type = session_info._message_queue.popleft()
        body = CPIMPayload.decode(content)
        timestamp = body.timestamp

        if data.code < 300:
            self.log.info('in dialog message was delivered to remote party: %s', data.reason)
            state = 'accepted'
        else:
            self.log.info('message was not delivered to remote party %s: %s', data.code, data.reason)
            state = 'failed'
        self.send(sylkrtc.SessionMessageDispositionNotificationEvent(session=session_info.id,
                                                                     code=data.code,
                                                                     reason=data.reason,
                                                                     state=state,
                                                                     message_id=message_id,
                                                                     message_timestamp=str(timestamp),
                                                                     timestamp=str(ISOTimestamp.now())))

    def _EH_janus_sip_event_dtmfsent(self, event):
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
            self.log.warning('could not find room session with handle ID {event.sender} for error event'.format(event=event))
            return
        if videoroom_session.type == 'publisher':
            pass
        else:
            pass

    def _EH_janus_videoroom_webrtcup(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find room session with handle ID {event.sender} for webrtcup event'.format(event=event))
            return
        if videoroom_session.type == 'publisher':
            self.log.debug('media published to room {session.room.uri}'.format(session=videoroom_session))
            self.send(sylkrtc.VideoroomSessionEstablishedEvent(session=videoroom_session.id))
        else:
            self.send(sylkrtc.VideoroomFeedEstablishedEvent(session=videoroom_session.parent_session.id, feed=videoroom_session.id))

    def _EH_janus_videoroom_hangup(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            return
        reactor.callLater(2, call_in_green_thread, self._cleanup_videoroom_session, videoroom_session)
        self.log.debug('session with room {session.room.uri} ended'.format(session=videoroom_session))

    def _EH_janus_videoroom_slowlink(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find room session with handle ID {event.sender} for slowlink event'.format(event=event))
            return
        if event.uplink:  # uplink is from janus' point of view
            if not videoroom_session.slow_download:
                self.log.debug('poor download connectivity to room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
            videoroom_session.slow_download = True
        else:
            if not videoroom_session.slow_upload:
                self.log.debug('poor upload connectivity to room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
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
            self.log.warning('could not find room session with handle ID {event.sender} for joined event'.format(event=event))
            return

        if ('m=video' in event.jsep.sdp and 'm=audio' in event.jsep.sdp):
            media = 'audio/video'
        elif ('m=video' in event.jsep.sdp):
            media = 'video only'
        elif ('m=audio' in event.jsep.sdp):
            media = 'audio only'
        else:
            media = 'unknown'

        self.log.info('joined room {session.room.uri} with {media}'.format(session=videoroom_session, media=media))
        self.log.debug('joined room {session.room.uri} with session {session.id}'.format(session=videoroom_session))
        data = event.plugindata.data  # type: janus.VideoroomJoined
        videoroom_session.publisher_id = data.id
        room = videoroom_session.room
        assert event.jsep is not None
        self.send(sylkrtc.VideoroomSessionAcceptedEvent(session=videoroom_session.id, sdp=event.jsep.sdp, audio=room.audio, video=room.video, duration=room.duration))
        # send information about existing publishers
        publishers = []
        for publisher in data.publishers:  # type: janus.VideoroomPublisher
            try:
                publisher_session = room[publisher.id]
            except KeyError:
                # External publisher (e.g. sip-janus-bridge connected
                # directly to Janus). The bridge packs its display name
                # and SIP URI into the Janus 'display' field with a TAB
                # separator; split them back out so clients see a real
                # user@domain entity with its own name.
                ext_name, ext_uri = _parse_external_publisher_display(
                    publisher.display, publisher.id,
                )
                publishers.append(dict(
                    id=str(publisher.id),
                    uri=ext_uri,
                    display_name=ext_name,
                ))
            else:
                publishers.append(dict(id=publisher_session.id, uri=publisher_session.account.id, display_name=publisher.display or ''))
        self.send(sylkrtc.VideoroomInitialPublishersEvent(session=videoroom_session.id, publishers=publishers))
        room.add(videoroom_session)  # adding the session to the room might also trigger sending an event with the active participants which must be sent last

    def _EH_janus_videoroom_attached(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find room session with handle ID {event.sender} for attached event'.format(event=event))
            return

        # get the session which originated the subscription
        base_session = videoroom_session.parent_session
        assert base_session is not None
        assert event.jsep is not None and event.jsep.type == 'offer'

        if ('m=video' in event.jsep.sdp and 'm=audio' in event.jsep.sdp):
            media = 'audio/video'
        elif ('m=video' in event.jsep.sdp):
            media = 'video only'
        elif ('m=audio' in event.jsep.sdp):
            media = 'audio only'
        else:
            media = 'unknown'

        self.log.debug('{media} media proposed to room {session.room.uri}'.format(session=videoroom_session, media=media))
        self.send(sylkrtc.VideoroomFeedAttachedEvent(session=base_session.id, feed=videoroom_session.id, sdp=event.jsep.sdp))

    def _EH_janus_videoroom_slow_link(self, event):
        pass

    def _EH_janus_videoroom_updated(self, event):
        pass

    def _EH_janus_videoroom_event_publishers(self, event):
        try:
            videoroom_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('could not find room session with handle ID {event.sender} for publishers event'.format(event=event))
            return
        room = videoroom_session.room
        # send information about new publishers
        publishers = []
        for publisher in event.plugindata.data.publishers:  # type: janus.VideoroomPublisher
            try:
                publisher_session = room[publisher.id]
            except KeyError:
                # External publisher (e.g. sip-janus-bridge connected
                # directly to Janus). The bridge packs its display name
                # and SIP URI into the Janus 'display' field with the
                # form "<name>|<uri>"; split them back out so clients
                # see a real user@domain entity with its own name.
                ext_name, ext_uri = _parse_external_publisher_display(
                    publisher.display, publisher.id,
                )
                publishers.append(dict(
                    id=str(publisher.id),
                    uri=ext_uri,
                    display_name=ext_name,
                ))
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
                self.log.warning('could not find room session with handle ID {event.sender} for leaving event'.format(event=event))
            return
        if publisher_id == 'ok':
            self.log.info('left room {session.room.uri}'.format(session=base_session))
            self.log.debug('left room {session.room.uri} with session {session.id}'.format(session=base_session))
            self._cleanup_videoroom_session(base_session)
            return
        try:
            publisher_session = base_session.feeds.pop(publisher_id)
            departed_id = publisher_session.id
        except KeyError:
            # The leaving publisher wasn't in our feeds — either we never
            # subscribed (e.g. an external bridge whose audio we don't
            # consume) or the publisher is unknown. Either way the client
            # may still have it in its participants list (we relayed the
            # publishers event earlier), so notify with the raw Janus id
            # so the UI removes the entry.
            departed_id = str(publisher_id)
        self.send(sylkrtc.VideoroomPublishersLeftEvent(session=base_session.id, publishers=[departed_id]))

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

    def _EH_janus_videoroom_event_kicked(self, event):
        # Janus videoroom plugin sends the "kicked" event in two
        # situations:
        #   (a) on the KICKED publisher's handle, telling them they
        #       were removed from the room — this is the one we have
        #       to act on (terminate the client's session so their
        #       phone drops the conference instead of sitting silently
        #       connected),
        #   (b) on the MODERATOR's handle as the synchronous ack of
        #       the kick request they just issued — same event shape
        #       but it just means "your kick worked".
        # We tell the two apart by checking the `kicked` field
        # (the kicked publisher's id) against our own publisher id:
        # equal → (a), we were kicked; not equal → (b), ignore.
        try:
            base_session = self.videoroom_sessions[event.sender]
        except KeyError:
            self.log.warning('kicked event for unknown handle {event.sender}'.format(event=event))
            return
        kicked_pid = getattr(event.plugindata.data, 'kicked', None)
        my_pid = getattr(base_session, 'publisher_id', None)
        if kicked_pid is not None and my_pid is not None and kicked_pid != my_pid:
            # Moderator ack — the kick succeeded but we weren't the
            # one removed from the room. Nothing to do on this side.
            self.log.debug('kicked ack: pid {} removed from room {} (we are pid {})'.format(
                kicked_pid, base_session.room.uri, my_pid))
            return
        self.log.info('kicked from room {session.room.uri} by moderation request'.format(session=base_session))
        # Tell the client the session was terminated so its Call /
        # Conference state-machine moves to 'terminated', closes the
        # PeerConnection and unmounts the conference UI.
        try:
            self.send(sylkrtc.VideoroomSessionTerminatedEvent(
                session=base_session.id, reason='kicked'))
        except Exception as e:
            self.log.warning('failed to forward kicked event to client: {}'.format(e))
        # Then clean up the gateway-side state — same path the normal
        # "publisher leaving" flow takes when our own publisher leaves
        # ("ok" branch of _EH_janus_videoroom_event_leaving).
        try:
            self._cleanup_videoroom_session(base_session)
        except Exception as e:
            self.log.warning('cleanup after kicked event failed: {}'.format(e))

    def _EH_janus_videoroom_event_display(self, event):
        # No-op: sylkrtc has no native "publisher-updated" event, and
        # re-emitting publishers-joined caused Android clients to add
        # the publisher a second time. Left+joined would force a full
        # WebRTC re-subscription — also unacceptable. So we just drop
        # display updates on the floor until the client/protocol grows
        # proper support.
        pass

    # Notification handlers

    def _NH_ChatSessionGotMessage(self, notification):
        session = notification.sender.sylk_session  # type: VideoroomSessionInfo
        message = notification.data.message
        sender = sylkrtc.SIPIdentity(uri=str(message.sender.uri), display_name=message.sender.display_name)
        content = message.content if isinstance(message.content, str) else message.content.decode('latin1')  # preserve binary data for transmitting over JSON
        if any(header.name == 'Message-Type' and header.value == 'status' and header.namespace == 'urn:ag-projects:xml:ns:cpim' for header in message.additional_headers):
            message_type = 'status'
        else:
            message_type = 'normal'
        self.send(sylkrtc.VideoroomMessageEvent(session=session.id, content=content, content_type=message.content_type, sender=sender, timestamp=str(message.timestamp), type=message_type))

    def _NH_ChatSessionGotComposingIndication(self, notification):
        session = notification.sender.sylk_session  # type: VideoroomSessionInfo
        composing = notification.data
        sender = sylkrtc.SIPIdentity(uri=str(composing.sender.uri), display_name=composing.sender.display_name)
        self.send(sylkrtc.VideoroomComposingIndicationEvent(session=session.id, state=composing.state, refresh=composing.refresh, content_type=composing.content_type, sender=sender))

    def _NH_ChatSessionDidDeliverMessage(self, notification):
        session = notification.sender.sylk_session  # type: VideoroomSessionInfo
        data = notification.data
        self.send(sylkrtc.VideoroomMessageDeliveryEvent(session=session.id, delivered=True, message_id=data.message_id, code=data.code, reason=data.reason))

    def _NH_ChatSessionDidNotDeliverMessage(self, notification):
        session = notification.sender.sylk_session  # type: VideoroomSessionInfo
        data = notification.data
        self.send(sylkrtc.VideoroomMessageDeliveryEvent(session=session.id, delivered=False, message_id=data.message_id, code=data.code, reason=data.reason))

    def _NH_SIPApplicationGotAccountDispositionNotification(self, notification):
        try:
            account_info = self.accounts_map[notification.sender]
        except KeyError:
            return

        if not account_info.auth_state:
            return

        message = notification.data.message
        self.log.info('received IMDN message ({status}) from: {originator.uri}'.format(status=message.state, originator=notification.data.sender))
        self.send(message)

    def _NH_SIPApplicationGotAccountMessage(self, notification):
        try:
            account_info = self.accounts_map[notification.sender]
        except KeyError:
            return

        if not account_info.auth_state:
            return

        message = notification.data
        self.log.info('received message ({content_type}) from: {originator.uri}'.format(content_type=message.content_type, originator=message.sender))
        self.send(message)

    def _NH_SIPApplicationGotOutgoingAccountMessage(self, notification):
        try:
            account_info = self.accounts_map[notification.sender]
        except KeyError:
            return

        if not account_info.auth_state:
            return

        message = notification.data
        self.log.info('received outgoing message ({content_type}) to {destination}'.format(content_type=message.content.content_type, destination=message.content.uri))
        self.send(message)

    def _NH_SIPApplicationGotAccountRemoveMessage(self, notification):
        try:
            account_info = self.accounts_map[notification.sender]
        except KeyError:
            return

        if not account_info.auth_state:
            return

        message = notification.data
        self.send(message)

    def _NH_SIPApplicationGotConversationReadMessage(self, notification):
        try:
            account_info = self.accounts_map[notification.sender]
        except KeyError:
            return

        if not account_info.auth_state:
            return

        message = notification.data
        self.send(message)

    def _NH_SIPApplicationGotConversationRemoveMessage(self, notification):
        try:
            account_info = self.accounts_map[notification.sender]
        except KeyError:
            return

        if not account_info.auth_state:
            return

        message = notification.data
        self.send(message)

    def _NH_SIPMessageDidSucceed(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)

        self.log.info('message was accepted by remote party')
        data = notification.data

        body = CPIMPayload.decode(notification.sender.body)
        message_id = next((header.value for header in body.additional_headers if header.name == 'Message-ID'), None)
        account_info = self.accounts_map['{}@{}'.format(body.sender.uri.user.decode('utf-8'), body.sender.uri.host.decode('utf-8'))]
        timestamp = body.timestamp

        if body.content_type != IMDNDocument.content_type:
            storage = MessageStorage()
            storage.update(account=account_info.id,
                           state='accepted',
                           message_id=message_id)

            event = sylkrtc.AccountDispositionNotificationEvent(account=account_info.id,
                                                                state='accepted',
                                                                message_id=message_id,
                                                                message_timestamp=str(timestamp),
                                                                code=data.code,
                                                                reason=data.reason,
                                                                timestamp=str(ISOTimestamp.now()))
            self.send(event)
            self._fork_event_to_online_accounts(account_info, event)

    def _NH_SIPMessageDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        data = notification.data
        body = CPIMPayload.decode(notification.sender.body)
        reason = data.reason.decode() if isinstance(data.reason, bytes) else data.reason
        callid = data.headers.get('Call-ID', Null).body if hasattr(data, 'headers') else None
        self.log.warning('could not deliver message to %s: %d %s (%s)' % (', '.join(([str(item.uri) for item in body.recipients])), data.code, reason, callid))
        message_id = next((header.value for header in body.additional_headers if header.name == 'Message-ID'), None)
        account = '{}@{}'.format(body.sender.uri.user.decode('utf-8'), body.sender.uri.host.decode('utf-8'))
        timestamp = body.timestamp

        if body.content_type != IMDNDocument.content_type:
            storage = MessageStorage()
            storage.update(account=account,
                           state='failed',
                           message_id=message_id)

            event = sylkrtc.AccountDispositionNotificationEvent(account=account,
                                                                state='failed',
                                                                message_id=message_id,
                                                                code=data.code,
                                                                reason=reason,
                                                                message_timestamp=str(timestamp),
                                                                timestamp=str(ISOTimestamp.now()))
            self.send(event)
            try:
                account_info = self.accounts_map[account]
            except KeyError:
                pass
            else:
                self._fork_event_to_online_accounts(account_info, event)


# noinspection PyPep8Naming
@implementer(IObserver)
class VideoroomChatHandler(object):

    def __init__(self, session):
        self.sylk_session = session  # type: VideoroomSessionInfo
        self.sip_session = None      # type: Optional[Session]
        self.chat_stream = None
        self._started = False
        self._ended = False
        self._message_queue = deque()
        self._conference_participants = set()  # type: Set[str]
        self._last_emitted_participants = set()  # type: Set[str]

    @property
    def account(self):
        return self.sylk_session.account

    @property
    def room(self):
        return self.sylk_session.room

    @run_in_green_thread
    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        from_uri = SIPURI.parse(self.account.uri)
        to_uri = SIPURI.parse('sip:{}'.format(self.room.uri))
        to_uri.host = to_uri.host.replace(b'videoconference', b'conference', 1)  # TODO: find a way to define this
        credentials = Credentials(username=from_uri.user, password=self.account.password.encode('utf-8'), digest=True)
        sip_account = DefaultAccount()
        sip_settings = SIPSimpleSettings()
        if sip_account.sip.outbound_proxy is not None:
            uri = SIPURI(host=sip_account.sip.outbound_proxy.host, port=sip_account.sip.outbound_proxy.port, parameters={'transport': sip_account.sip.outbound_proxy.transport})
        else:
            uri = to_uri
        # Route the chat-session DNS lookup through the shared cache so
        # every REFER (invite / BYE) the gateway later sends to the same
        # focus reuses this result instead of doing its own resolver
        # round-trip. The cache key is (uri, transport_list); the REFER
        # path composes the exact same key (outbound_proxy if set, else
        # the focus URI), so the chat handler's miss populates the entry
        # the REFER then hits.
        try:
            routes = _cached_lookup_sip_proxy(uri, sip_settings.sip.transport_list, self.room.log)
            route = routes[0]
        except (DNSLookupError, IndexError):
            self.end()
            self.room.log.error('DNS lookup for SIP proxy for {} failed'.format(uri))
            self.room.log.error('chat session for {} failed: DNS lookup error'.format(self.account.id))
            notification_center.post_notification('ChatSessionDidFail', sender=self, data=NotificationData(originator='local', code=0, reason=None, failure_reason='DNS lookup error'))
            return
        if self._ended:  # end was called during DNS lookup
            self.room.log.debug('chat session for {} ended'.format(self.account.id))
            notification_center.post_notification('ChatSessionDidEnd', sender=self)
            return
        self.sip_session = Session(sip_account)
        self.chat_stream = MediaStreamRegistry.ChatStream()
        notification_center.add_observer(self, sender=self.sip_session)
        notification_center.add_observer(self, sender=self.chat_stream)
        self.room.log.debug('chat {} starting at {}'.format(to_uri, route))
        self.sip_session.connect(FromHeader(from_uri, self.account.display_name), ToHeader(to_uri), route=route, streams=[self.chat_stream], credentials=credentials)

    @run_in_twisted_thread
    def end(self):
        if self._ended:
            return
        notification_center = NotificationCenter()
        if self.sip_session is not None:
            notification_center.remove_observer(self, sender=self.sip_session)
            notification_center.remove_observer(self, sender=self.chat_stream)
            self.sip_session.end()
            self.sip_session = None
            self.chat_stream = None
            self._conference_participants = set()
            self._last_emitted_participants = set()
            self.room.log.debug('chat session for {} ended'.format(self.account.id))
            notification_center.post_notification('ChatSessionDidEnd', sender=self)
        while self._message_queue:
            message_id, content, content_type = self._message_queue.popleft()
            data = NotificationData(message_id=message_id, message=None, code=0, reason='Chat session ended')
            notification_center.post_notification('ChatSessionDidNotDeliverMessage', sender=self, data=data)
        self._ended = True

    @run_in_twisted_thread
    def send_message(self, message_id, content, content_type='text/plain'):
        if self._ended:
            notification_center = NotificationCenter()
            data = NotificationData(message_id=message_id, message=None, code=0, reason='Chat session ended')
            notification_center.post_notification('ChatSessionDidNotDeliverMessage', sender=self, data=data)
        else:
            self._message_queue.append((message_id, content, content_type))
            if self.chat_stream is not None:
                self._send_queued_messages()

    @run_in_twisted_thread
    def send_composing_indication(self, state, refresh=None):
        if self.chat_stream is not None:
            self.chat_stream.send_composing_indication(state, refresh=refresh)

    def _send_queued_messages(self):
        while self._message_queue:
            message_id, content, content_type = self._message_queue.popleft()
            self.chat_stream.send_message(content, content_type, message_id=message_id)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        sess = self.sip_session
        remote_focus = getattr(sess, 'remote_focus', False)
        remote_identity = getattr(sess, 'remote_identity', None)
        self.room.log.info(
            'chat session connected: account=%s remote=%s isfocus=%s '
            '(conference event subscription %s)',
            self.account.id,
            getattr(remote_identity, 'uri', '?') if remote_identity else '?',
            remote_focus,
            'will run automatically' if remote_focus else 'WILL NOT run (remote is not a focus)',
        )
        notification.center.post_notification('ChatSessionDidStart', sender=self)
        self._send_queued_messages()

    def _NH_SIPConferenceDidAddParticipant(self, notification):
        """
        Fires when we ourselves successfully add a participant to the
        conference via session.conference.add(uri). NOT triggered for
        other participants joining on their own — that comes through
        SIPSessionGotConferenceInfo. Logged here for completeness so
        every conference-related notification leaves a trace.
        """
        self.room.log.info(
            'SIPConferenceDidAddParticipant participant=%r',
            notification.data.participant,
        )

    def _NH_SIPConferenceDidRemoveParticipant(self, notification):
        """Fires when we ourselves remove a participant."""
        self.room.log.info(
            'SIPConferenceDidRemoveParticipant participant=%r',
            notification.data.participant,
        )

    def _NH_SIPConferenceDidNotAddParticipant(self, notification):
        self.room.log.warning(
            'SIPConferenceDidNotAddParticipant participant=%r code=%r reason=%r',
            notification.data.participant,
            getattr(notification.data, 'code', None),
            getattr(notification.data, 'reason', None),
        )

    def _NH_SIPConferenceDidNotRemoveParticipant(self, notification):
        self.room.log.warning(
            'SIPConferenceDidNotRemoveParticipant participant=%r code=%r reason=%r',
            notification.data.participant,
            getattr(notification.data, 'code', None),
            getattr(notification.data, 'reason', None),
        )

    def _NH_SIPSessionDidEnd(self, notification):
        notification.center.remove_observer(self, sender=self.sip_session)
        notification.center.remove_observer(self, sender=self.chat_stream)
        self.sip_session = None
        self.chat_stream = None
        self.end()
        self.room.log.debug('chat session for {} ended'.format(self.account.id))
        notification.center.post_notification('ChatSessionDidEnd', sender=self, data=notification.data)

    def _NH_SIPSessionDidFail(self, notification):
        notification.center.remove_observer(self, sender=self.sip_session)
        notification.center.remove_observer(self, sender=self.chat_stream)
        self.sip_session = None
        self.chat_stream = None
        self.end()
        self.room.log.error('chat session for {} failed: {}'.format(self.account.id, notification.data.failure_reason))
        notification.center.post_notification('ChatSessionDidFail', sender=self, data=notification.data)

    # noinspection PyUnusedLocal
    def _NH_SIPSessionNewProposal(self, notification):
        self.sip_session.reject_proposal()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        # sylkserver's SIP Session class doesn't implement the transfer API
        # self.sip_session.reject_transfer(403)
        pass

    def _auto_mute_new_sip_participants(self, new_pid_uri_map, new_webrtc_pid_map, bridge_pid):
        """
        Send REFER ;method=MUTE for each newly-arrived SIP participant.

        Called from _NH_SIPSessionGotConferenceInfo once the per-NOTIFY
        pid maps have been rebuilt. The room-shared `auto_muted_pids`
        set records pids we have already auto-muted so multiple chat
        handlers (one per WebRTC client subscribed to this room) don't
        race to issue redundant REFERs for the same arrival.

        Pids no longer present in the current roster are dropped from
        the set so a participant that leaves and rejoins (focus mints
        a fresh pid for the new session) gets auto-muted again.

        WebRTC publishers and the audio-bridge endpoint are excluded —
        WebRTC peers are addressed through the regular client-side
        mute flow / mute-all broadcast, and muting the bridge would
        silence the entire PSTN leg of the conference.
        """
        room = self.room
        # Prune stale pids first so the set never grows unbounded.
        try:
            current_pids = set(new_pid_uri_map.keys())
            room.auto_muted_pids = {p for p in room.auto_muted_pids if p in current_pids}
        except Exception:
            room.auto_muted_pids = set()
        # Pre-resolve the focus URI once; bail if the chat session
        # isn't ready yet (a brand-new NOTIFY can race the SIP
        # session setup on initial join).
        if self.sip_session is None or not getattr(self.sip_session, 'remote_focus', False):
            return
        try:
            focus_uri = SIPURI.new(self.sip_session.remote_identity.uri)
        except SIPCoreError as e:
            room.log.debug('auto-mute: focus URI unresolved: {}'.format(e))
            return
        # Find an AccountInfo to attribute the REFER to. The chat
        # handler's own session is bound to a specific account already
        # (self.account); reuse it. Without an account credentials are
        # missing and SipFocusReferralHandler can't authenticate.
        if self.account is None:
            return
        new_sip_pids = []
        for pid, uri_str in new_pid_uri_map.items():
            if not pid or not uri_str:
                continue
            if pid in new_webrtc_pid_map:
                continue  # WebRTC peer; not a SIP arrival
            if bridge_pid and pid == bridge_pid:
                continue  # audio-bridge endpoint
            if pid in room.auto_muted_pids:
                continue  # already auto-muted this pid in this room
            new_sip_pids.append((pid, uri_str))
        if not new_sip_pids:
            return
        for pid, uri_str in new_sip_pids:
            if not uri_str.lower().startswith(('sip:', 'sips:')):
                uri_str = 'sip:{}'.format(uri_str)
            try:
                participant_uri = SIPURI.parse(uri_str)
            except SIPCoreError:
                room.log.warning('auto-mute: skipping SIP pid={} (invalid URI {!r})'.format(pid, uri_str))
                continue
            # Mark the pid as claimed BEFORE dispatching so a second
            # chat handler hitting this code path on the same NOTIFY
            # (rare but possible if NOTIFYs interleave) sees the set
            # entry and skips. The REFER itself is async; if it fails
            # downstream the pid stays marked until the participant
            # leaves and rejoins.
            room.auto_muted_pids.add(pid)
            room.log.info('auto-mute: referring {} ;method=MUTE (pid={}) — new SIP arrival'.format(
                participant_uri, pid))
            try:
                SipFocusReferralHandler(
                    focus_uri,
                    participant_uri,
                    self.account,
                    room.log,
                    method='MUTE',
                    refer_to_extra_params={'participant_id': pid},
                ).start()
            except Exception as e:
                room.log.warning('auto-mute: REFER MUTE for pid={} failed: {}'.format(pid, e))
                # Roll back so a retry on the next NOTIFY is possible.
                room.auto_muted_pids.discard(pid)

    def _NH_SIPSessionGotConferenceInfo(self, notification):
        conference_info = notification.data.conference_info
        current_display = {}
        for user in conference_info.users:
            display = user.display_text.value if user.display_text else None
            current_display[user.entity] = display
        # Diagnostic: surface incoming NOTIFY so we can correlate
        # mute REFER → focus republish → gateway forward → client
        # event end-to-end. Prints one line per arriving NOTIFY with
        # a short "uri[muted=…]" summary per endpoint. Cheap and
        # essential when chasing "icon didn't update" reports.
        try:
            _summary = []
            for u in conference_info.users:
                _ent = str(getattr(u, 'entity', '') or '')
                _eps = []
                for ep in u:
                    _m = getattr(ep, 'muted', None)
                    if _m is not None and hasattr(_m, 'value'):
                        _m = _m.value
                    _eps.append('muted={}'.format(_m))
                _summary.append('{}[{}]'.format(_ent, ','.join(_eps) or '-'))
            self.room.log.info('conference-info NOTIFY in: {}'.format(' | '.join(_summary)))
        except Exception as e:
            self.room.log.debug('conference-info NOTIFY summary failed: {}'.format(e))
        def _aor(uri):
            if uri.startswith('sip:'):
                uri = uri[4:]
            elif uri.startswith('sips:'):
                uri = uri[5:]
            return uri.split(';', 1)[0]

        def _is_bridge(uri):
            return 'app=sylk-janus-audio-bridge' in (uri or '').lower()

        webrtc_publishers = {}
        for session in self.room:
            if session.type != 'publisher' or session.account is None:
                continue
            webrtc_publishers[session.account.id] = session

        # Find the bridge participant up front so we can log its admin
        # + UDP endpoints on every fresh arrival below. The bridge is
        # the only User in the NOTIFY that carries the agp-conf:admin*
        # extensions; scanning the list once here is cheap and avoids
        # threading state down through the per-user loop further below
        # just for logging.
        bridge_admin_url = None
        bridge_admin_token = None
        bridge_udp_endpoint = None
        for u in conference_info.users:
            if _is_bridge(getattr(u, 'entity', None)):
                bridge_admin_url = Videoroom._extension_value(getattr(u, 'admin_endpoint_url', None))
                bridge_admin_token = Videoroom._extension_value(getattr(u, 'admin_endpoint_token', None))
                bridge_udp_endpoint = Videoroom._extension_value(getattr(u, 'audio_levels_udp_endpoint', None))
                break

        sip_set = set(current_display)
        # Mirror the SIP-side roster onto the Videoroom so the
        # auto-kick path (which fires when the last WebRTC publisher
        # leaves) has an accurate, up-to-date list of SIP participants
        # to REFER ;method=BYE. The room outlives any single chat
        # handler, so this is the only place all handlers converge on
        # a shared view of who's currently in the conference focus.
        self.room._sip_roster = dict(current_display)
        previous_sip_set = self._conference_participants
        for entity in sip_set - previous_sip_set:
            if _aor(entity) in webrtc_publishers:
                continue
            display = current_display[entity]
            label = '{} <{}>'.format(display, _aor(entity)) if display else _aor(entity)
            self.room.log.info('SIP participant joined: {}'.format(label))
            # Show the bridge-advertised endpoints alongside every new
            # arrival so the operator can correlate which admin / UDP
            # endpoint that participant should be addressed through.
            # Token is a secret — print only a short prefix.
            if bridge_admin_url:
                token_preview = (bridge_admin_token[:6] + '…') if bridge_admin_token else '(no token)'
                udp_text = bridge_udp_endpoint or '(no UDP)'
                self.room.log.info('  bridge admin: {url} token={token} udp={udp}'.format(
                    url=bridge_admin_url, token=token_preview, udp=udp_text))
        for entity in previous_sip_set - sip_set:
            if _aor(entity) in webrtc_publishers:
                continue
            self.room.log.info('SIP participant left: {}'.format(_aor(entity)))
        self._conference_participants = sip_set

        sip_aor_map = {_aor(entity): entity for entity in sip_set}
        combined_set = set(sip_aor_map) | set(webrtc_publishers)
        # Build a richer dedup signature than just the participant URI
        # set: also fold in each endpoint's muted / status state, so a
        # mute/unmute toggle (which doesn't change the roster) still
        # passes the dedup gate and reaches the client. Without this,
        # a moderator-driven mute via REFER ;method=MUTE updates the
        # focus's audio_stream.muted flag and republishes conference
        # info, but the gateway saw an unchanged participant set and
        # returned early — the WebRTC client's tile never repainted.
        #
        # Tiny pre-scan over conference_info.users; mirrors the EXACT
        # extraction the diagnostic NOTIFY summary above uses (which
        # demonstrably sees muted=True), to avoid any divergence
        # between what gets logged and what gets dedup-compared. The
        # earlier attempt used a `_muted_of()` helper that called
        # `getattr(muted_elem, 'value', muted_elem)` — that path
        # silently lost the value for the sipsimple bool descriptor
        # in some cases, producing muted=None even when the summary
        # right above logged muted=True. Inline + minimal here.
        state_sig_parts = []
        for u in conference_info.users:
            ep_states = []
            for ep in u:
                ep_pid = Videoroom._extension_value(getattr(ep, 'participant_id', None))
                ep_status = getattr(ep, 'status', None)
                if ep_status is not None and hasattr(ep_status, 'value'):
                    ep_status = ep_status.value
                # Muted: same two-step the summary does — fetch the
                # attribute, unwrap .value if the descriptor returned
                # a wrapper. Whatever bool/None comes out goes into
                # the signature tuple as-is; equality between two
                # signatures fires only if BOTH the bool flag and the
                # absence/presence change.
                _ep_muted = getattr(ep, 'muted', None)
                if _ep_muted is not None and hasattr(_ep_muted, 'value'):
                    _ep_muted = _ep_muted.value
                ep_states.append((ep_pid or '', _ep_muted, str(ep_status or '')))
            state_sig_parts.append((str(getattr(u, 'entity', '') or ''), tuple(ep_states)))
        state_signature = tuple(state_sig_parts)
        # Dedup intentionally disabled. The previous dedup compared the
        # current state signature against `_last_emitted_state` (per-
        # chat-handler) and skipped the forward when nothing changed.
        # In the wild this hid the muted-state initial snapshot from
        # clients that reconnected (bundle reload, transport blip) AFTER
        # the muted state had already been emitted to the OLD handler —
        # the new handler had None _last_emitted_state, but the OLD
        # handler's state was preserved on its singleton _sip_roster
        # and (despite the per-handler reset) the FIRST NOTIFY a fresh
        # client receives can end up identical to its self-initialised
        # baseline if the join NOTIFY isn't a fresh-roster event. Net
        # effect for the user: the SIP-tile mute icon was stuck on
        # unmuted even after the focus reported muted=True.
        # Conference-info NOTIFYs are infrequent (one per join/leave/
        # mute/state change), so emitting every one to the client is
        # not a noise problem. Keep the bookkeeping fields so any
        # external observer relying on them survives; they just don't
        # gate the forward anymore.
        self._last_emitted_participants = combined_set
        self._last_emitted_state = state_signature
        self.room.log.info('conference-info forward (dedup disabled): roster_size={} endpoints_total={}'.format(
            len(combined_set),
            sum(len(p[1]) for p in state_signature)))

        # Rebuild the participant_id → label cache for this room from
        # the current NOTIFY. Audio-level UDP datagrams arrive keyed by
        # participant_id (an opaque token); the periodic log resolves
        # back to "display <aor>" via this map.
        new_labels = {}
        # participant_id → VideoroomSession for every WebRTC publisher
        # currently in this room. Used by _RH_videoroom_mute_participant
        # to dispatch a per-participant mute event over the publisher's
        # own WS connection instead of proxying through the conference
        # focus's admin API — for WebRTC peers we want the mic to be
        # muted at the source, not just suppressed at the mix.
        new_webrtc_pid_map = {}
        # participant_id → user.entity (SIP URI string) for every user
        # in this NOTIFY. The SIP-side mute path uses it to build a
        # truthful Refer-To URI for the REFER ;method=MUTE / UNMUTE
        # the gateway sends back to the focus.
        new_pid_uri_map = {}
        payload_participants = []
        for user in conference_info.users:
            user_aor = _aor(getattr(user, 'entity', '') or '')
            user_display = ''
            try:
                if user.display_text and user.display_text.value:
                    user_display = user.display_text.value
            except AttributeError:
                pass
            # Short human-readable identifier used in the per-participant
            # audio-level log line: display name if SIP carried one,
            # otherwise the user part of the AoR (e.g. "ag" from
            # sip:ag@sip2sip.info). Falls back to the full AoR when even
            # the user part is empty, so the log never shows just "".
            user_local = user_aor.split('@', 1)[0] if '@' in user_aor else user_aor
            user_label = user_display or user_local or user_aor
            endpoints = []
            for endpoint in user:
                media_items = []
                for media in endpoint:
                    media_type = getattr(media, 'media_type', None) or getattr(media, 'type', None)
                    media_status = getattr(media, 'status', None)
                    if media_status is not None and hasattr(media_status, 'value'):
                        media_status = media_status.value
                    media_items.append(sylkrtc.VideoroomConferenceMedia(
                        type=str(media_type) if media_type is not None else None,
                        status=str(media_status) if media_status is not None else None,
                    ))
                endpoint_status = getattr(endpoint, 'status', None)
                if endpoint_status is not None and hasattr(endpoint_status, 'value'):
                    endpoint_status = endpoint_status.value
                endpoint_display = getattr(endpoint, 'display_text', None)
                if endpoint_display is not None and hasattr(endpoint_display, 'value'):
                    endpoint_display = endpoint_display.value
                # Sylk-specific extensions on the Endpoint: participant_id
                # (the stable per-session token) and muted (server-side
                # input mute flag, set on every non-bridge endpoint).
                participant_id = Videoroom._extension_value(getattr(endpoint, 'participant_id', None))
                if participant_id:
                    new_labels[participant_id] = user_label
                    # Record pid → user.entity for the SIP-side mute
                    # path. user.entity is the canonical SIP URI as
                    # published by the focus; falling back to user_aor
                    # if the entity slipped through empty.
                    _entity = getattr(user, 'entity', None)
                    if _entity:
                        new_pid_uri_map[participant_id] = str(_entity)
                    elif user_aor:
                        new_pid_uri_map[participant_id] = 'sip:{}'.format(user_aor)
                # Muted state extraction. The diagnostic NOTIFY summary
                # at the top of this function reliably reads muted=True
                # for endpoints the focus has flagged muted, but the
                # earlier `getattr(elem,'value',elem)` + isinstance path
                # we used here ended up serialising muted as null on the
                # WS. Use the same hasattr-then-.value path the summary
                # uses, and tolerate string forms ("true"/"True"/"1")
                # because sipsimple's MutedFlag descriptor has shipped
                # both shapes across versions. Cast the final result to
                # a plain Python bool so BooleanProperty doesn't see a
                # truthy-but-non-bool value and skip emit.
                muted_elem = getattr(endpoint, 'muted', None)
                muted_value = None
                if muted_elem is not None:
                    if hasattr(muted_elem, 'value'):
                        raw = muted_elem.value
                    else:
                        raw = muted_elem
                    if isinstance(raw, bool):
                        muted_value = bool(raw)
                    elif isinstance(raw, str):
                        muted_value = raw.strip().lower() in ('true', '1', 'yes')
                    elif isinstance(raw, int):
                        muted_value = bool(raw)
                self.room.log.debug(
                    'muted-extract endpoint pid={} muted_elem_type={} muted_elem_repr={!r} muted_value={!r}'.format(
                        participant_id, type(muted_elem).__name__, muted_elem, muted_value))
                _vce = sylkrtc.VideoroomConferenceEndpoint(
                    uri=str(endpoint.entity) if getattr(endpoint, 'entity', None) else None,
                    display_name=endpoint_display,
                    status=str(endpoint_status) if endpoint_status is not None else None,
                    media=media_items,
                    participant_id=participant_id,
                    muted=muted_value,
                )
                # Confirm the model round-tripped the value — if the
                # BooleanProperty descriptor refuses to keep True for
                # some reason (e.g. optional=True with a falsey check)
                # we want to see it in the log rather than silently
                # shipping null to the mobile.
                self.room.log.info(
                    'endpoint payload pid={} input_muted={!r} stored_muted={!r}'.format(
                        participant_id, muted_value, getattr(_vce, 'muted', '<missing>')))
                endpoints.append(_vce)
            participant_aor = _aor(user.entity)
            if participant_aor in webrtc_publishers:
                ptype = 'webrtc'
                # Bind each endpoint's participant_id to the matching
                # WebRTC publisher session so a per-participant mute
                # request can be dispatched locally over WS. With one
                # device per AoR (the common case) there is exactly one
                # endpoint and the mapping is unambiguous; with multiple
                # devices behind the same AoR `webrtc_publishers` keeps
                # only one session (last-write-wins, same caveat the
                # rest of this module already accepts).
                _wpub = webrtc_publishers.get(participant_aor)
                if _wpub is not None:
                    for _ep in endpoints:
                        if _ep.participant_id:
                            new_webrtc_pid_map[_ep.participant_id] = _wpub
            elif _is_bridge(user.entity) or any(_is_bridge(getattr(e, 'uri', None)) for e in endpoints):
                ptype = 'bridge'
            else:
                ptype = 'sip'
            # Surface the admin endpoint URL + per-room token only on the
            # bridge participant — that's the only User in the NOTIFY that
            # carries the agp-conf:admin_endpoint_* extensions.
            admin_url = None
            admin_token = None
            udp_endpoint = None
            if ptype == 'bridge':
                admin_url = Videoroom._extension_value(getattr(user, 'admin_endpoint_url', None))
                admin_token = Videoroom._extension_value(getattr(user, 'admin_endpoint_token', None))
                udp_endpoint = Videoroom._extension_value(getattr(user, 'audio_levels_udp_endpoint', None))
                # Cache on the Videoroom so the WS request handler for
                # mute can reach the conference admin API on demand
                # (the request handler runs on a different async path
                # than the NOTIFY arrival).
                if admin_url:
                    self.room.admin_endpoint_url = admin_url
                if admin_token:
                    self.room.admin_endpoint_token = admin_token
                if udp_endpoint:
                    # Cache so the destroy path can drop_subscription()
                    # immediately instead of waiting for the focus's
                    # TTL to expire and "?" audio-level lines to leak
                    # past the destroyed videoroom.
                    self.room.audio_levels_udp_endpoint = udp_endpoint
                # Cache the bridge's participant_id so the audio-level
                # periodic log can skip the bridge entry — the bridge's
                # pjmedia in/out is plumbing, not a user-facing source.
                # Pick the first endpoint that carries one; the bridge
                # publishes a stable per-session pid on its endpoint.
                for ep in endpoints:
                    if ep.participant_id:
                        self.room.bridge_participant_id = ep.participant_id
                        # Drop the bridge entry from the label cache too,
                        # so the log never even attempts to render it.
                        new_labels.pop(ep.participant_id, None)
                        break
                # As soon as the bridge tells us where its audio-level UDP
                # server lives, register/refresh our subscription so the
                # remote focus starts streaming levels back. ensure_subscription
                # is idempotent — repeated NOTIFYs just bump the timestamp.
                if udp_endpoint and admin_token:
                    try:
                        from .audio_level_udp import AudioLevelUDPClient
                        AudioLevelUDPClient().ensure_subscription(
                            udp_endpoint, self.room.uri.replace('videoconference', 'conference', 1),
                            admin_token,
                        )
                    except Exception as e:
                        self.room.log.debug('audio-level UDP subscribe failed: %s' % e)
            payload_participants.append(sylkrtc.VideoroomConferenceParticipant(
                type=ptype,
                uri=user.entity,
                display_name=current_display[user.entity],
                endpoints=endpoints,
                admin_endpoint_url=admin_url,
                admin_endpoint_token=admin_token,
                audio_levels_udp_endpoint=udp_endpoint,
            ))
        for account_id, session in webrtc_publishers.items():
            if account_id in sip_aor_map:
                continue
            payload_participants.append(sylkrtc.VideoroomConferenceParticipant(
                type='webrtc',
                uri='sip:{}'.format(account_id),
                display_name=session.account.display_name,
                endpoints=[],
            ))
        # Replace the room's label cache wholesale. participant_ids
        # that vanish from the NOTIFY drop out, fresh ones get the
        # current label. Used by the audio-level periodic log.
        self.room.participant_labels = new_labels
        # Refresh the participant_id → WebRTC session map for this room
        # using the same wholesale-replace approach. Consumed by
        # _RH_videoroom_mute_participant to decide whether a mute target
        # is a local WebRTC peer (dispatched as a mute-request WS event)
        # or a SIP-only participant behind the bridge (proxied as a
        # REFER ;method=MUTE / UNMUTE to the conference focus).
        self.room.webrtc_participants_by_pid = new_webrtc_pid_map
        # And the pid → URI map used to build the Refer-To URI on the
        # SIP-side branch of the same handler.
        self.room.participant_uris_by_pid = new_pid_uri_map
        # Auto-mute new SIP arrivals. Run AFTER the pid maps above are
        # updated so we can cleanly distinguish "WebRTC publisher" (in
        # new_webrtc_pid_map), "audio bridge" (room.bridge_participant_id),
        # and "SIP caller" (everything else in new_pid_uri_map). For each
        # newly-seen SIP pid we fire a REFER ;method=MUTE to the focus
        # exactly once, recording the pid in room.auto_muted_pids so
        # other chat handlers (one per WebRTC client subscribed to the
        # same room) don't re-issue the REFER. Pids no longer in the
        # roster are pruned so a rejoining participant — which the
        # focus would mint a fresh pid for — gets re-auto-muted.
        try:
            self._auto_mute_new_sip_participants(
                new_pid_uri_map, new_webrtc_pid_map,
                getattr(self.room, 'bridge_participant_id', None))
        except Exception as e:
            self.room.log.warning('auto-mute pass failed: {}'.format(e))
        # Conference duration is computed locally from the videoroom's
        # own start_time anchor. Intentionally NOT taken from the SIP
        # focus's `agp-conf:duration` field — the webrtcgateway runs an
        # independent timer so the duration shown to WebRTC clients is
        # consistent even if the SIP bridge restarts, the focus reports
        # a different value, or the SIP side of the conference came up
        # earlier than the webrtc side.
        duration = self.room.duration
        try:
            self.sylk_session.owner.send(sylkrtc.VideoroomConferenceParticipantsEvent(
                session=self.sylk_session.id,
                participants=payload_participants,
                duration=duration,
            ))
        except Exception as e:
            self.room.log.warning('failed to forward SIP conference participants event: {}'.format(e))

    def _NH_ChatStreamGotMessage(self, notification):
        self.chat_stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
        notification.center.post_notification('ChatSessionGotMessage', sender=self, data=notification.data)

    def _NH_ChatStreamGotComposingIndication(self, notification):
        notification.center.post_notification('ChatSessionGotComposingIndication', sender=self, data=notification.data)

    def _NH_ChatStreamDidSendMessage(self, notification):
        notification.center.post_notification('ChatSessionDidSendMessage', sender=self, data=notification.data)

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        notification.center.post_notification('ChatSessionDidDeliverMessage', sender=self, data=notification.data)

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        notification.center.post_notification('ChatSessionDidNotDeliverMessage', sender=self, data=notification.data)
