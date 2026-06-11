
import math
import os
import random
import re
import secrets
import shutil
import string
import time
import weakref
import base64

from collections import Counter, deque
from glob import glob
from itertools import chain, count, cycle

from application.notification import IObserver, NotificationCenter, NotificationData
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
from twisted.internet.task import LoopingCall
from zope.interface import implementer

from sylk.accounts import DefaultAccount
from sylk.applications.conference.audio_level_udp import LevelUDPServer
from sylk.applications.conference.configuration import get_room_config, ConferenceConfig
from sylk.applications.conference.logger import log
from sylk.payloads.conference_info_extensions import Duration
from sylk.bonjour import BonjourService
from sylk.configuration import ServerConfig, ThorNodeConfig
from sylk.configuration.datatypes import URL
from sylk.resources import Resources
from sylk.session import Session, IllegalStateError
from sylk.web import server as web_server


def format_identity(identity):
    uri = identity.uri

    user = uri.user.decode() if isinstance(uri.user, bytes) else uri.user
    host = uri.host.decode() if isinstance(uri.host, bytes) else uri.host

    if identity.display_name:
        return '%s <%s@%s>' % (identity.display_name, user, host)
    else:
        return '%s@%s' % (user, host)


class ScreenImage(object):
    def __init__(self, room, sender):
        self.room = weakref.ref(room)
        self.room_uri = room.uri
        self.sender = sender
        self.filename = os.path.join(ConferenceConfig.screensharing_images_dir, room.uri, '%s@%s_%s.jpg' % (sender.uri.user.decode(), sender.uri.host.decode(), ''.join(random.sample(string.ascii_letters+string.digits, 10))))
        url = web_server.url + '/conference/' + room.uri + '/screensharing'        
        self.url = URL(url)
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
            with open(tmp_filename, 'wb+') as file:
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


class _InviterEviction(object):
    """Per-invitee anti-fraud eviction timer.

    Armed when the recorded inviter of a tracked invitee leaves the
    conference. While armed it logs once per minute showing the
    minutes remaining, then fires by BYE'ing every session in the
    room whose remote AoR matches the protected invitee. Cancelled
    if the invitee leaves on their own, if the inviter rejoins (any
    device, AoR match), or if the room shuts down.

    The lifecycle is driven by a single recurring `reactor.callLater`
    handle (`_tick_call`). Each tick decides whether to log + reschedule
    or to fire + stop — keeping one timer per invitee is simpler to
    cancel correctly than two parallel timers (a logger and an
    evictor). The tick interval is the lesser of 60s and the time
    remaining, so the final tick lands exactly on the eviction deadline
    no matter what the grace period is.
    """

    def __init__(self, room, invitee_aor, inviter_aor, grace_seconds):
        self.room = room
        self.invitee_aor = invitee_aor
        self.inviter_aor = inviter_aor
        self.deadline = time.time() + grace_seconds
        self._tick_call = None
        log.info('Room %s - anti-fraud eviction armed: %s (invited by %s) will be released in %d minute(s) if inviter does not return' %
                 (room.uri, invitee_aor, inviter_aor, max(1, int(round(grace_seconds / 60.0)))))
        self._schedule_next_tick()

    def _schedule_next_tick(self):
        remaining = self.deadline - time.time()
        # Final tick lands on the deadline; intermediate ticks every 60s.
        delay = max(0.0, min(60.0, remaining))
        self._tick_call = reactor.callLater(delay, self._tick)

    def _tick(self):
        self._tick_call = None
        remaining = self.deadline - time.time()
        if remaining <= 0.5:
            self._fire()
            return
        # Round UP so the user-visible countdown never reads "0 minutes
        # remaining" on a tick that isn't the firing one.
        minutes_left = max(1, int(math.ceil(remaining / 60.0)))
        log.info('Room %s - eviction countdown: %s (invited by %s) — %d minute(s) remaining' %
                 (self.room.uri, self.invitee_aor, self.inviter_aor, minutes_left))
        self._schedule_next_tick()

    def cancel(self, reason):
        if self._tick_call is not None and self._tick_call.active():
            self._tick_call.cancel()
        self._tick_call = None
        log.info('Room %s - eviction cancelled for %s (invited by %s): %s' %
                 (self.room.uri, self.invitee_aor, self.inviter_aor, reason))

    def _fire(self):
        log.info('Room %s - eviction fired: releasing leg %s (invited by %s, grace period elapsed)' %
                 (self.room.uri, self.invitee_aor, self.inviter_aor))
        # Remove ourselves from the pending map BEFORE BYE'ing — the
        # session.end() below will fire SIPSessionDidEnd → remove_session
        # which would otherwise try to cancel us again and log a spurious
        # "cancelled" line for a timer that already fired.
        self.room._pending_evictions.pop(self.invitee_aor, None)
        # One-shot: also drop the invitee_inviter mapping. If the
        # same AoR comes back into the room by some other route later
        # we treat that as a fresh, untracked join (per design).
        self.room._invitee_inviter.pop(self.invitee_aor, None)
        try:
            self.room._bye_invitee_sessions(self.invitee_aor)
        except Exception as e:
            log.warning('Room %s - eviction BYE for %s raised: %s' % (self.room.uri, self.invitee_aor, e))


@implementer(IObserver)
class Room(object):
    """
    Object representing a conference room, it will handle the message dispatching
    among all the participants.
    """

    def __init__(self, uri):
        self.config = get_room_config(uri)
        self.uri = uri
        self.identity = ChatIdentity(SIPURI.parse('sip:%s' % self.uri), display_name='Conference Room')
        self.files = []
        self.screen_images = {}
        self.subject = ''
        self.sessions = []
        self.subscriptions = []
        # subscription object -> subscriber From URI. IncomingSubscription is a
        # C-extension type and can't hold arbitrary attributes, so the
        # subscriber identity (used to pick the SIP-only vs videoroom NOTIFY
        # variant) is kept here instead. Cleaned up when the subscription ends.
        self._subscription_uris = {}
        self.state = 'stopped'
        # Latest videoroom roster published to this room by the webrtcgateway
        # via SIP PUBLISH (Event: conference). Used to enrich the conference-info
        # NOTIFY sent to SIP-only subscribers with the WebRTC participants.
        # {'body': <str|None>, 'content_type': <str|None>}.
        self.videoroom_roster = None
        # Anti-fraud eviction state. Two parallel maps:
        #   _invitee_inviter   : invitee_aor (lower "user@host") ->
        #                        inviter_aor (lower "user@host")
        #                        Populated by add_session() when a
        #                        tracked invited session joins (the
        #                        IncomingReferralHandler tagged the
        #                        session with _sylk_inviter_aor based
        #                        on inviter_eviction_destinations).
        #                        Cleared when the invitee leaves or
        #                        the eviction timer fires.
        #   _pending_evictions : invitee_aor -> _InviterEviction
        #                        Armed by remove_session() when the
        #                        inviter's last session leaves the
        #                        room. Cancelled when the invitee
        #                        leaves on their own, when the
        #                        inviter rejoins (any device, same
        #                        AoR), or when the room shuts down.
        # Both maps are keyed by lower-cased AoR so they survive
        # parameter / scheme differences between the REFER's
        # Refer-To URI and the session's remote_identity.uri at
        # join time. See _InviterEviction for the timer mechanics.
        self._invitee_inviter = {}
        self._pending_evictions = {}
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
        # Audio-level sampling state (populated by _sample_audio_levels) and
        # the set of audio streams currently force-muted by the admin API.
        # Keys/elements are id(audio_stream).
        self.audio_levels = {}
        self.muted_streams = set()
        self._level_sampler = None
        # Two independent rolling accumulators fed by the same sampler.
        # Keys are participant_id; values are {'tx_sum', 'rx_sum',
        # 'tx_peak', 'rx_peak', 'count'} integers. Each consumer flushes
        # (resets) its own accumulator on its own cadence.
        #   * _level_log_accumulator    — flushed every audio_level_log_period
        #                                 seconds by _log_audio_levels
        #   * _level_notify_accumulator — flushed every audio_level_notify_period
        #                                 ms by _emit_level_notification
        # Both track per-window peak alongside the sum, because pjmedia's
        # signal level is a per-frame µ-law-averaged absolute value — the
        # mean of mean-of-µ-law's washes out useful peaks, the per-window
        # max is the meaningful "did this participant speak" signal.
        self._level_log_accumulator = {}
        self._level_notify_accumulator = {}
        self._level_logger = None
        self._level_notifier = None
        # One-shot diagnostic flags used to explain (exactly once per
        # room) why no audio level lines are being emitted. Without
        # these the sample loop swallows every error and the operator
        # has no visibility into what went wrong.
        self._level_diag_logged_no_method = False
        self._level_diag_logged_no_streams = False
        self._level_diag_logged_sample_error = False
        self._level_diag_logged_empty_window = False
        # Per-room random token. Published in the conference-info payload
        # to the sylk-janus-audio-bridge participant and accepted by the
        # admin HTTP API as an alternative to the global auth secret —
        # restricted to endpoints scoped to *this* room. Generated once,
        # stable for the room's lifetime.
        self.auth_token = secrets.token_urlsafe(32)
        # Wallclock timestamp the conference room was created. Rooms are
        # lazily created on the first INVITE that lands in their URI, so
        # this is effectively the conference's true start time. Surfaced
        # to subscribers as <agp-conf:start_time> on the conference-info
        # NOTIFY and relayed by the webrtcgateway to WebRTC clients, so
        # late joiners know how long the conference has been running.
        self.start_time = ISOTimestamp.utcnow()

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
        return set(stream.type for stream in chain(*(session.streams for session in self.sessions if session.streams)))

    @property
    def conference_info(self):
        return self.build_conference_info()

    def build_conference_info(self, hide_bridges=False):
        # When hide_bridges is True the bridge components (the
        # sylk-janus-audio-bridge leg) are omitted — used for the NOTIFY sent to
        # SIP-only subscribers. The videoroom (webrtcgateway) subscriber gets
        # the full roster (hide_bridges=False) and does its own merge.
        if self.conference_info_payload is None:
            settings = SIPSimpleSettings()
            conference_description = conference.ConferenceDescription(display_text='Ad-hoc conference', free_text='Hosted by %s' % settings.user_agent, subject=self.subject)
            conference_description.conf_uris = conference.ConfUris()
            conference_description.conf_uris.add(conference.ConfUrisEntry('sip:%s' % self.uri, purpose='participation'))
            if self.config.advertise_xmpp_support:
                conference_description.conf_uris.add(conference.ConfUrisEntry('xmpp:%s' % self.uri, purpose='participation'))
                # TODO: add grouptextchat service uri
            for number in self.config.pstn_access_numbers:
                conference_description.conf_uris.add(conference.ConfUrisEntry('tel:%s' % number, purpose='participation'))
            host_info = conference.HostInfo(web_page=conference.WebPage('http://sylkserver.com'))
            self.conference_info_payload = conference.Conference(self.identity.uri, conference_description=conference_description, host_info=host_info, users=conference.Users())
        # Refresh the conference duration (seconds since room creation)
        # on every NOTIFY build. Authoritative and computed server-side,
        # so the client doesn't have to deal with timezone or clock-skew.
        # Late joiners read it once from their initial NOTIFY and run a
        # local counter from there. The Duration extension (registered
        # in sylk.payloads.conference_info_extensions on application load)
        # wraps the int through its XML descriptor automatically.
        try:
            elapsed = int((ISOTimestamp.utcnow() - self.start_time).total_seconds())
            if elapsed < 0:
                elapsed = 0
            self.conference_info_payload.conference_description.duration = Duration(elapsed)
        except (AttributeError, Exception):
            # sylk.payloads.conference_info_extensions not imported yet —
            # the descriptor on conference_description.duration doesn't
            # exist. Skip silently rather than break the whole payload.
            pass
        self.conference_info_payload.version = next(self.conference_info_version)
        user_count = len(self.participants_counter)
        self.conference_info_payload.conference_state = conference.ConferenceState(user_count=user_count, active=True)
        # Resolve the admin base URL once per build; cheap (just config lookup).
        # Imported lazily to avoid a circular dependency at module load time.
        from sylk.applications.conference import ConferenceApplication
        try:
            admin_url = ConferenceApplication().admin_url
        except Exception:
            admin_url = None

        users = conference.Users()
        for session in (session for session in self.sessions if not (len(session.streams) == 1 and session.streams[0].type == 'file-transfer')):
            if hide_bridges and self._session_matches_room(session):
                continue
            try:
                user = next(user for user in users if user.entity == str(session.remote_identity.uri))
            except StopIteration:
                display_text = self.last_nicknames_map.get(str(session.remote_identity.uri), session.remote_identity.display_name)
                user = conference.User(str(session.remote_identity.uri), display_text=display_text)
                user_uri = '%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)
                screen_image = self.screen_images.get(user_uri, None)
                if screen_image is not None and screen_image.active:
                    user.screen_image_url = screen_image.url
                # Publish the admin endpoint URL + the per-room auth token,
                # but only to the sylk-janus-audio-bridge participant — it's
                # the one expected to drive the admin API to mute participants
                # and read audio levels. Other endpoints don't get the token,
                # so they can't impersonate the bridge.
                if admin_url and getattr(session, '_sylk_audio_bridge', False):
                    try:
                        user.admin_endpoint_url = admin_url
                        user.admin_endpoint_token = self.auth_token
                    except Exception:
                        # Older sipsimple without the User extensions — skip
                        # silently rather than break the whole NOTIFY payload.
                        pass
                    # Publish the audio-levels UDP server endpoint too,
                    # so the webrtcgateway can subscribe for real-time
                    # level streaming. None when the UDP server isn't
                    # running on this focus (audio_level_udp_listen unset).
                    try:
                        from sylk.applications.conference.audio_level_udp import LevelUDPServer
                        udp_endpoint = LevelUDPServer().endpoint
                    except Exception:
                        udp_endpoint = None
                    if udp_endpoint:
                        try:
                            user.audio_levels_udp_endpoint = udp_endpoint
                        except Exception:
                            pass
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
            # Always publish the SylkServer-assigned participant id so the
            # bridge can target this exact device (multiple devices behind
            # the same AoR get distinct ids). Set on every endpoint —
            # including the bridge — so any subscriber can identify
            # itself in the payload it receives.
            participant_id = getattr(session, '_sylk_participant_id', None)
            if participant_id is not None:
                try:
                    endpoint.participant_id = participant_id
                except Exception:
                    pass
            # Publish the per-endpoint server-side mute state, but skip the
            # audio bridge itself — it's the one driving the mute commands
            # and its own audio path through the conference is uninteresting
            # to publish back to it. For all other participants we always
            # set the flag (True or False) so subscribers see a definitive
            # answer instead of having to infer absence as "not muted".
            if not getattr(session, '_sylk_audio_bridge', False):
                try:
                    audio_stream = next(s for s in session.streams if s.type == 'audio')
                except StopIteration:
                    audio_stream = None
                if audio_stream is not None:
                    try:
                        endpoint.muted = bool(getattr(audio_stream, 'muted', False))
                    except Exception:
                        # Older sipsimple without the Endpoint extension —
                        # don't break the rest of the NOTIFY payload.
                        pass
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
            room_user = self.identity.uri.user.decode()
            self.bonjour_services = BonjourService(service='sipuri', name='Conference Room %s' % room_user, uri_user=room_user)
            self.bonjour_services.start()
        self.message_dispatcher = proc.spawn(self._message_dispatcher)
        self.audio_conference = AudioConference()
        self.audio_conference.hold()
        self.moh_player = MoHPlayer(self.audio_conference)
        self.moh_player.start()
        self.state = 'started'
        log.info('Room %s - music on hold is %s' % (self.uri, 'disabled' if self.config.disable_music_on_hold else 'enabled'))
        # Start periodic audio-level sampling. Sampling is cheap (a single
        # pjmedia call per audio stream) and ungated by subscribers — the
        # snapshot endpoint always returns the latest value too. When the
        # configured sample period is zero, sampling is disabled.
        period_ms = int(getattr(ConferenceConfig, 'audio_level_sample_period', 0) or 0)
        if period_ms > 0:
            self._level_sampler = LoopingCall(self._sample_audio_levels)
            self._level_sampler.start(period_ms / 1000.0, now=False)
        # Separate (slower) loop emits one summary log line per room every
        # audio_level_log_period seconds, averaging every sample taken in
        # the window. Driven independently from the sampler so the log
        # cadence is decoupled from the publishing cadence.
        log_period_s = float(getattr(ConferenceConfig, 'audio_level_log_period', 0) or 0)
        if log_period_s > 0:
            self._level_logger = LoopingCall(self._log_audio_levels)
            self._level_logger.start(log_period_s, now=False)
        # Real-time notification publisher. Runs faster than the logger
        # (default 4 Hz / 250ms) and dispatches a ConferenceRoomAudioLevels
        # NotificationCenter event each tick carrying mean + peak per
        # participant. The webrtcgateway listens for this and forwards
        # to every connected WebRTC client in the videoroom; the admin
        # API's SSE stream listens for the same notification.
        notify_period_ms = int(getattr(ConferenceConfig, 'audio_level_notify_period', 0) or 0)
        if notify_period_ms > 0:
            self._level_notifier = LoopingCall(self._emit_level_notification)
            self._level_notifier.start(notify_period_ms / 1000.0, now=False)
        # Log the admin endpoint + per-room token now that the room is
        # live. Useful for operators that watch syslog: gives them the
        # exact URL/token to drive the admin API for this room, without
        # having to wait for an audio-bridge participant to join and
        # publish them in the conference NOTIFY. Lazy-import the
        # application to avoid a circular dependency at module load.
        try:
            from sylk.applications.conference import ConferenceApplication
            admin_url = ConferenceApplication().admin_url
        except Exception:
            admin_url = None
        if admin_url:
            log.info('Room %s - admin endpoint %s token=%s' %
                     (self.uri, admin_url, self.auth_token))
        else:
            log.info('Room %s - admin endpoint disabled (no http_management_interface) token=%s' %
                     (self.uri, self.auth_token))
        log.info('Room %s - conference started at %s' % (self.uri, self.start_time))

    def stop(self):
        if not self.started:
            return
        self.state = 'stopping'
        if self._level_sampler is not None:
            if self._level_sampler.running:
                self._level_sampler.stop()
            self._level_sampler = None
        if self._level_logger is not None:
            if self._level_logger.running:
                self._level_logger.stop()
            self._level_logger = None
        if self._level_notifier is not None:
            if self._level_notifier.running:
                self._level_notifier.stop()
            self._level_notifier = None
        self.audio_levels.clear()
        self._level_log_accumulator.clear()
        self._level_notify_accumulator.clear()
        self.muted_streams.clear()
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
        self._subscription_uris = {}
        self.cleanup_files()
        # Cancel every armed anti-fraud eviction timer — the room is
        # going away, so any reactor.callLater holding a reference to
        # `self` would prevent garbage collection AND fire after the
        # Room has been torn down (logging against a half-destroyed
        # state). The mappings themselves are dropped too for the
        # same reason.
        for _invitee_aor, _ev in list(self._pending_evictions.items()):
            try:
                _ev.cancel('room stopping')
            except Exception:
                pass
        self._pending_evictions.clear()
        self._invitee_inviter.clear()
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

    # ------------------------------------------------------------------
    # Audio mixer introspection (admin API)
    # ------------------------------------------------------------------

    def _sample_audio_levels(self):
        """Sample TX/RX signal levels for every audio stream in the mixer.

        Stores the result in self.audio_levels keyed by participant_id
        (the same stable per-session token that's published in the
        conference-info payload), and feeds the per-window accumulators
        used by the periodic logger and the real-time notifier. No
        notifications are fired from here — that's _emit_level_notification's
        job on its own cadence. Errors are swallowed per stream so a
        single bad slot doesn't kill the whole sample, but the first
        occurrence of each failure mode is logged so silent breakage
        is visible.
        """
        if self.audio_conference is None:
            return
        try:
            mixer = self.audio_conference.bridge.mixer
        except AttributeError:
            return
        # Probe once whether the rebuilt sipsimple core actually exposes
        # get_signal_level(). Without it every sample below would raise
        # AttributeError and the accumulator would never fill.
        if not hasattr(mixer, 'get_signal_level'):
            if not self._level_diag_logged_no_method:
                self._level_diag_logged_no_method = True
                log.warning('Room %s - audio level sampling disabled: '
                            'AudioMixer.get_signal_level is missing — the '
                            'python3-sipsimple core has not been rebuilt '
                            'with the level helper patch' % self.uri)
            return
        streams_seen = 0
        streams_sampled = 0
        last_sample_error = None
        levels = {}
        for stream in list(self.audio_conference.streams):
            streams_seen += 1
            session = getattr(stream, 'session', None)
            pid = getattr(session, '_sylk_participant_id', None) if session is not None else None
            if pid is None:
                continue
            transport = getattr(stream, '_transport', None)
            slot = getattr(transport, 'slot', None) if transport is not None else None
            if slot is None:
                continue
            try:
                # Prefer the high-level helper added in python3-sipsimple
                # (AudioStream.signal_level) — falls back to calling the
                # mixer directly if running against an older sipsimple.
                tx_rx = getattr(stream, 'signal_level', None)
                if tx_rx is None or tx_rx == (0, 0):
                    tx_rx = mixer.get_signal_level(slot)
            except Exception as e:
                last_sample_error = e
                continue
            tx, rx = int(tx_rx[0]), int(tx_rx[1])
            levels[pid] = {'tx': tx, 'rx': rx}
            streams_sampled += 1
            # Feed both rolling accumulators. They're flushed by
            # different consumers on different cadences but both want
            # the same statistics: sum (for mean) and per-window peak.
            for accumulator in (self._level_log_accumulator, self._level_notify_accumulator):
                acc = accumulator.get(pid)
                if acc is None:
                    acc = {'tx_sum': 0, 'rx_sum': 0, 'tx_peak': 0, 'rx_peak': 0, 'count': 0}
                    accumulator[pid] = acc
                acc['tx_sum'] += tx
                acc['rx_sum'] += rx
                if tx > acc['tx_peak']:
                    acc['tx_peak'] = tx
                if rx > acc['rx_peak']:
                    acc['rx_peak'] = rx
                acc['count'] += 1
        self.audio_levels = levels
        # Diagnostics — fire at most once per room each.
        if streams_seen == 0 and not self._level_diag_logged_no_streams:
            self._level_diag_logged_no_streams = True
            log.info('Room %s - audio level sampling: no audio streams '
                     'are attached to the conference mixer yet' % self.uri)
        elif streams_seen > 0 and streams_sampled == 0 and last_sample_error is not None \
                and not self._level_diag_logged_sample_error:
            self._level_diag_logged_sample_error = True
            log.warning('Room %s - audio level sampling: %d stream(s) '
                        'present but all reads failed (first error: %s: %s)' %
                        (self.uri, streams_seen,
                         type(last_sample_error).__name__, last_sample_error))

    def _emit_level_notification(self):
        """Flush the real-time accumulator and fire a single
        ConferenceRoomAudioLevels notification.

        Carries per-participant mean (`tx`, `rx`) and per-window peak
        (`tx_peak`, `rx_peak`) since the last notification. The peak is
        the meaningful "is this slot speaking right now" signal — the
        mean is washed out by pjmedia's µ-law-averaging-per-frame
        (speech is bursty, and µ-law compresses dynamic range).
        Consumed by the admin SSE stream and by the webrtcgateway,
        which forwards a conference-audio-levels event to every
        WebRTC client in the matching videoroom.
        """
        accumulator, self._level_notify_accumulator = self._level_notify_accumulator, {}
        if not accumulator:
            return
        levels = {}
        for pid, acc in accumulator.items():
            count = acc['count']
            if count <= 0:
                continue
            levels[pid] = {
                'tx': acc['tx_sum'] // count,
                'rx': acc['rx_sum'] // count,
                'tx_peak': acc['tx_peak'],
                'rx_peak': acc['rx_peak'],
            }
        if not levels:
            return
        try:
            NotificationCenter().post_notification(
                'ConferenceRoomAudioLevels',
                sender=self,
                data=NotificationData(uri=self.uri, levels=levels),
            )
        except Exception:
            pass
        # Cross-host fanout. The UDP server hosts a subscribe/unsubscribe
        # protocol and streams datagrams to every live subscription for
        # this room URI. Webrtcgateways running on different hosts find
        # the endpoint via the agp-conf:audio_levels_udp_endpoint
        # extension we publish in the conference-info NOTIFY (on the
        # audio-bridge participant's User element). No-op when there are
        # no subscribers — the common case for rooms no one is bridging.
        try:
            LevelUDPServer().send_levels(
                self.uri,
                levels,
                ts=int(ISOTimestamp.utcnow().timestamp() * 1000),
            )
        except Exception:
            pass

    def _log_audio_levels(self):
        """Emit one summary log line for the room with the mean tx/rx of
        each participant over the current audio_level_log_period window.

        The mean is computed from every sample taken since the previous
        log tick (so it self-adjusts when the sample period changes or
        when participants join/leave mid-window). The accumulator is
        reset on every call. Rooms with no audio streams produce no log
        output — but we log once when the window is empty despite the
        room having audio streams, so silent breakage stays visible.
        """
        # Snapshot + reset the accumulator atomically (single-threaded
        # reactor — no lock needed).
        accumulator, self._level_log_accumulator = self._level_log_accumulator, {}
        if not accumulator:
            # Empty window. If there are audio streams in the room then
            # the sampler is failing — say so once so the operator has
            # somewhere to start looking. Don't repeat on every tick.
            if (self.audio_conference is not None
                    and len(self.audio_conference.streams) > 0
                    and not self._level_diag_logged_empty_window):
                self._level_diag_logged_empty_window = True
                log.warning('Room %s - audio level log window was empty '
                            'despite %d active audio stream(s); the '
                            'periodic sampler is not producing data' %
                            (self.uri, len(self.audio_conference.streams)))
            return
        # Resetting the empty-window flag once we do have data means we
        # will warn again if the pipeline breaks later.
        self._level_diag_logged_empty_window = False

        # Build a label per participant id from the current session list.
        # Also note which pid belongs to the audio-bridge so we can drop
        # it from the log — the bridge's pjmedia in/out is just gateway
        # plumbing, not a user-visible participant. "who" is the short
        # human-readable identifier — display name if SIP carried one,
        # otherwise the user part of the AoR (e.g. "ag" from
        # sip:ag@sip2sip.info).
        label_by_pid = {}
        bridge_pids = set()
        for session in self.sessions:
            pid = getattr(session, '_sylk_participant_id', None)
            if pid is None:
                continue
            if getattr(session, '_sylk_audio_bridge', False):
                bridge_pids.add(pid)
                continue
            try:
                identity = session.remote_identity
                uri = identity.uri
                user = uri.user.decode() if isinstance(uri.user, bytes) else (uri.user or '')
                display = identity.display_name or ''
                if isinstance(display, bytes):
                    display = display.decode()
                who = display or user or '?'
            except Exception:
                who = '?'
            label_by_pid[pid] = who

        # One log line per participant — short, greppable, and consistent
        # across the three audio-level emitters (focus, webrtcgateway,
        # audio-bridge). The username column is padded/truncated to a
        # fixed 10-char width so columns line up across log lines.
        # We use the full Room URI prefix here ("Room <user>@<host>")
        # to match every other room.py log line so an operator can grep
        # by full room URI without surprises.
        # Format:
        #   Room <uri> audio level: "<who:10>" "<pid>" <N>s mean/peak, n=<samples>, tx=A/B rx=C/D
        # pjmedia's tx/rx are per-frame µ-law-averaged absolute amplitudes;
        # peak tracks perceived speech bursts (bursty speech reads high
        # on peak but low on mean).
        period = ConferenceConfig.audio_level_log_period
        for pid, acc in accumulator.items():
            if pid in bridge_pids:
                continue  # audio-bridge plumbing — not user-facing
            count = acc['count']
            if count <= 0:
                continue
            avg_tx = acc['tx_sum'] // count
            avg_rx = acc['rx_sum'] // count
            peak_tx = acc['tx_peak']
            peak_rx = acc['rx_peak']
            who = label_by_pid.get(pid, '?')
            log.debug(
                'Room %s audio level: "%-10.10s" "%s" %ss mean/peak, n=%d, tx=%d/%d rx=%d/%d' %
                (self.uri, who, pid, period, count,
                 avg_tx, peak_tx, avg_rx, peak_rx)
            )

    def _find_audio_session(self, identifier):
        """Resolve a participant identifier to (session, audio_stream).

        Accepted forms, tried in this order:

        1. The participant_id token (string) — the canonical, stable
           per-session id published as <agp-conf:participant_id> in the
           conference-info NOTIFY payload. Disambiguates multiple devices
           sharing the same AoR. This is what tooling should use.
        2. The session's Contact URI as a string — also unique per device
           (it's the standard Endpoint `entity` attribute).
        3. A SIP URI (with sip:/sips: prefix) or AoR (user@host),
           case-insensitive. When multiple sessions share the same AoR
           the first match wins — use the participant_id instead to be
           deterministic.
        4. An integer — `id(audio_stream)`, internal-only.

        Returns (None, None) if no match is found.
        """
        def _aor(uri):
            try:
                user = (uri.user or b'').decode() if isinstance(uri.user, bytes) else (uri.user or '')
                host = (uri.host or b'').decode() if isinstance(uri.host, bytes) else (uri.host or '')
            except Exception:
                return None
            if not user or not host:
                return None
            return '{}@{}'.format(user, host).lower()

        wanted_pid = None
        wanted_contact = None
        wanted_id = None
        wanted_aor = None
        wanted_uri = None
        if isinstance(identifier, int):
            wanted_id = identifier
        elif isinstance(identifier, str):
            value = identifier.strip()
            if value.lower().startswith(('sip:', 'sips:')):
                wanted_uri = value.lower()
                wanted_aor = value.split(':', 1)[1].split(';', 1)[0].split('?', 1)[0].lower()
                wanted_contact = value
            elif '@' in value:
                wanted_aor = value.lower()
            else:
                # No scheme, no '@' — treat as opaque participant id.
                wanted_pid = value
        else:
            return (None, None)

        for session in self.sessions:
            try:
                audio_stream = next(s for s in session.streams if s.type == 'audio')
            except StopIteration:
                continue
            # 1. Participant id (preferred).
            if wanted_pid is not None and getattr(session, '_sylk_participant_id', None) == wanted_pid:
                return (session, audio_stream)
            # 2. Contact URI.
            if wanted_contact is not None:
                try:
                    contact = str(session._invitation.remote_contact_header.uri)
                except Exception:
                    contact = ''
                if contact == wanted_contact:
                    return (session, audio_stream)
            # 3. AoR / SIP URI on the remote identity.
            if wanted_aor is not None and _aor(session.remote_identity.uri) == wanted_aor:
                return (session, audio_stream)
            if wanted_uri is not None and str(session.remote_identity.uri).lower() == wanted_uri:
                return (session, audio_stream)
            # 4. Internal stream id.
            if wanted_id is not None and id(audio_stream) == wanted_id:
                return (session, audio_stream)
        return (None, None)

    def get_participants(self):
        """Snapshot of the room as a list of plain dicts (JSON-friendly).

        Each entry carries:
          - participant_id (canonical id, matches keys of audio_levels and
            the <agp-conf:participant_id> tag in the conference NOTIFY)
          - the SIP AoR plus the per-device Contact URI
          - is_audio_bridge flag (True for the sylk-janus-audio-bridge leg)
          - mute / hold state, active media, latest signal levels.
        """
        out = []
        for session in self.sessions:
            try:
                audio_stream = next(s for s in session.streams if s.type == 'audio')
            except StopIteration:
                audio_stream = None
            stream_id = id(audio_stream) if audio_stream is not None else None
            pid = getattr(session, '_sylk_participant_id', None)
            holdable = [s for s in session.streams if getattr(s, 'hold_supported', False)]
            on_hold = bool(holdable) and all(getattr(s, 'on_hold_by_remote', False) for s in holdable)
            try:
                contact_uri = str(session._invitation.remote_contact_header.uri)
            except Exception:
                contact_uri = None
            entry = {
                'participant_id': pid,
                'uri': str(session.remote_identity.uri),
                'contact_uri': contact_uri,
                'display_name': session.remote_identity.display_name or '',
                'stream_id': stream_id,
                'is_audio_bridge': bool(getattr(session, '_sylk_audio_bridge', False)),
                'muted': bool(audio_stream is not None and getattr(audio_stream, 'muted', False)),
                'on_hold': on_hold,
                'media': sorted({s.type for s in session.streams}),
                'levels': self.audio_levels.get(pid, {'tx': 0, 'rx': 0}),
                'call_id': getattr(session, 'call_id', None),
            }
            out.append(entry)
        return out

    def set_participant_muted(self, identifier, muted):
        """Force the input mute state of a participant's audio stream.

        Returns True on success, False if no matching audio stream was
        found in the room. When muted=True the participant's voice stops
        reaching the mix; they continue hearing the conference normally.
        Idempotent — calling with the current state is a no-op (besides
        re-issuing the conference info update).
        """
        session, audio_stream = self._find_audio_session(identifier)
        if audio_stream is None:
            return False
        target = bool(muted)
        current = bool(getattr(audio_stream, 'muted', False))
        if current != target:
            audio_stream.muted = target
            log.info('Room %s - participant %s %smuted by admin API' %
                     (self.uri, session.remote_identity.uri, '' if target else 'un'))
            try:
                self.dispatch_server_message(
                    '%s has been %smuted by the moderator' %
                    (format_identity(session.remote_identity), '' if target else 'un'))
            except Exception:
                pass
        if target:
            self.muted_streams.add(id(audio_stream))
        else:
            self.muted_streams.discard(id(audio_stream))
        # Republish conference info so SIP subscribers (and the admin API
        # listing) see the change immediately.
        try:
            self.dispatch_conference_info()
        except Exception:
            pass
        return True

    def _message_dispatcher(self):
        """Read from self.incoming_message_queue and dispatch the messages to other participants"""
        while True:
            session, message_type, data = self.incoming_message_queue.wait()
            if message_type == 'message':
                message = data.message

                if str(message.sender.uri) != str(session.remote_identity.uri):
                    continue

                if isinstance(message.content, bytes) and message.content.startswith(b'?OTR:'):
                    continue

                if message.timestamp is None:
                    message.timestamp = ISOTimestamp.utcnow()

                message.sender.display_name = self.last_nicknames_map.get(str(session.remote_identity.uri), message.sender.display_name)
                recipient = message.recipients[0]
                private = len(message.recipients) == 1 and '%s@%s' % (recipient.uri.user.decode(), recipient.uri.host.decode()) != str(self.uri)

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

    def _session_matches_room(self, session):
        """True if a session's AoR user-part equals this room's user-part — the
        gateway/bridge plumbing that joins as the room itself: the
        sylk-janus-audio-bridge leg (<room>@conference.<domain>) and the
        webrtcgateway videoroom chat legs (<room>@videoconference.<domain>).
        These are hidden from SIP-only subscribers, whose roster shows the real
        WebRTC participants (published separately) instead."""
        try:
            def _s(x):
                return (x.decode() if isinstance(x, bytes) else (x or '')).lower()
            return _s(session.remote_identity.uri.user) == _s(self.identity.uri.user)
        except Exception:
            return getattr(session, '_sylk_audio_bridge', False)

    def _is_videoroom_subscriber(self, uri):
        """True if a conference-info subscriber is a webrtcgateway participant
        leg rather than a native SIP phone.

        The gateway opens each WebRTC participant's chat leg — and therefore
        its conference-info SUBSCRIBE — under that participant's OWN real AoR
        (see VideoroomChatHandler.start), so it appears as itself in the
        roster. That makes the subscriber URI alone indistinguishable from a
        SIP phone: the earlier `<user>@videoconference.<host>` heuristic never
        matched any real subscriber, so every gateway leg was misclassified as
        SIP-only and served the bridge-stripped NOTIFY — which dropped the
        audio-bridge User carrying agp-conf:audio_levels_udp_endpoint and the
        gateway then never subscribed to the audio-level UDP stream.

        Instead, match the subscriber's AoR against the sessions this room has
        already flagged as gateway-originated (`_sylk_from_gateway`, set in
        ConferenceApplication.incoming_session from the INVITE's
        `X-Sylk-App: conference` marker). Gateway legs get the full roster
        (hide_bridges=False); native SIP phones get the bridge filtered out."""
        if uri is None:
            return False
        def _s(x):
            return (x.decode() if isinstance(x, bytes) else (x or '')).lower()
        subscriber_aor = '%s@%s' % (_s(uri.user), _s(uri.host))
        if not _s(uri.user) or not _s(uri.host):
            return False
        for session in self.sessions:
            if not getattr(session, '_sylk_from_gateway', False):
                continue
            try:
                ruri = session.remote_identity.uri
            except Exception:
                continue
            if '%s@%s' % (_s(ruri.user), _s(ruri.host)) == subscriber_aor:
                return True
        return False

    def dispatch_conference_info(self):
        full_data = self.build_conference_info(hide_bridges=False)
        sip_data = None  # built on demand when a SIP-only subscriber is present
        for subscription in (subscription for subscription in self.subscriptions if subscription.state.lower() == 'active'):
            if self._is_videoroom_subscriber(self._subscription_uris.get(subscription)):
                data = full_data
            else:
                if sip_data is None:
                    sip_data = self.build_conference_info(hide_bridges=True)
                data = sip_data
            try:
                subscription.push_content(conference.ConferenceDocument.content_type, data)
            except (SIPCoreError, SIPCoreInvalidStateError):
                pass

    def set_videoroom_roster(self, body, content_type=None, etag=None):
        """Store (or clear, when body is None) the videoroom roster most
        recently PUBLISHed to this room by the webrtcgateway. `etag` is the
        SIP-ETag the focus handed back on the 200, used to validate the
        SIP-If-Match on subsequent refresh/modify PUBLISHes. Consumed when
        building the conference-info NOTIFY for SIP-only subscribers."""
        if body is None:
            self.videoroom_roster = None
        else:
            self.videoroom_roster = {'body': body, 'content_type': content_type, 'etag': etag}

    def dispatch_file(self, file):
        sender_uri = file.sender.uri
        for uri in set(session.remote_identity.uri for session in self.sessions if str(session.remote_identity.uri) != str(sender_uri)):
            handler = FileTransferHandler(self)
            handler.init_outgoing(uri, file)

    @staticmethod
    def _session_aor(session):
        """Lower-cased "user@host" for a session's remote identity, or ''
        when the URI fields don't yield a usable AoR. Tolerates the
        bytes-typed SIPURI attributes that sipsimple sometimes returns
        for URIs that came off the wire — same defensive pattern used
        by terminate_sessions.
        """
        try:
            u = session.remote_identity.uri
            user = u.user
            host = u.host
            user = user.decode() if isinstance(user, bytes) else (user or '')
            host = host.decode() if isinstance(host, bytes) else (host or '')
        except Exception:
            return ''
        if not user or not host:
            return ''
        return '{}@{}'.format(user, host).lower()

    def _bye_invitee_sessions(self, invitee_aor):
        """End every session in self.sessions whose AoR matches the given
        invitee_aor (lower-cased). Called by _InviterEviction._fire().
        Defensive against the session being already gone — session.end()
        is a no-op once the session has reached terminated state.
        """
        ended = 0
        for session in list(self.sessions):
            if self._session_aor(session) != invitee_aor:
                continue
            try:
                session.end()
                ended += 1
            except Exception as e:
                log.warning('Room %s - anti-fraud BYE for %s raised: %s' % (self.uri, invitee_aor, e))
        if ended == 0:
            log.info('Room %s - eviction fire: no live session matched %s (already gone)' % (self.uri, invitee_aor))

    @staticmethod
    def _device_id_from_contact(session):
        """Return the device identity advertised by the UA in its Contact
        header, or None if it didn't advertise one.

        Looks for the RFC 5626 ``+sip.instance`` Contact-header parameter,
        whose value is the device's instance-id (typically a
        ``"<urn:uuid:...>"``). The urn:uuid wrapper is stripped and the
        value is reduced to SIP/XML token-safe characters so the result
        can be published as the <agp-conf:participant_id> attribute and
        ride in a Refer-To parameter (mute/unmute) without quoting.
        Returns None when no instance-id is present or nothing usable
        survives sanitisation, so the caller falls back to a generated
        token. Never raises.
        """
        try:
            inv = getattr(session, '_invitation', None)
            contact_hdr = getattr(inv, 'remote_contact_header', None) if inv is not None else None
            params = getattr(contact_hdr, 'parameters', None) or {} if contact_hdr is not None else {}
            for k, v in params.items():
                key = k.decode() if isinstance(k, bytes) else k
                if str(key).strip().lower() != '+sip.instance':
                    continue
                val = v.decode() if isinstance(v, bytes) else v
                val = str(val).strip().strip('"').strip()
                if val.startswith('<') and val.endswith('>'):
                    val = val[1:-1].strip()
                if val.lower().startswith('urn:uuid:'):
                    val = val[len('urn:uuid:'):]
                # Keep only token-safe characters (alnum and -._~).
                val = re.sub(r'[^A-Za-z0-9._~-]', '', val)
                return val or None
        except Exception as e:
            log.warning('extracting device id from Contact failed: %s' % e)
        return None

    def add_session(self, session):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        self.sessions.append(session)
        remote_uri = str(session.remote_identity.uri)
        self.participants_counter[remote_uri] += 1
        # Anti-fraud bookkeeping. Two distinct hooks fire here:
        #
        #   1. If this session was created by a REFER ;method=INVITE
        #      to a destination matching the configured tracking
        #      pattern, IncomingReferralHandler stamped it with
        #      `_sylk_inviter_aor` (and only in that case). Record
        #      the invitee→inviter mapping so a later departure of
        #      the inviter can arm the eviction timer. Direct
        #      (incoming) dial-ins have no such tag — they pay their
        #      own bill and are not tracked.
        #
        #   2. The joining session's own AoR may match an inviter who
        #      had previously left and triggered timers for their
        #      invitees. Treat any reappearance of the inviter
        #      (same AoR, any device) as the inviter retaking
        #      responsibility — cancel every pending eviction they
        #      own. The map entry is left intact: if they leave again
        #      we re-arm.
        try:
            inviter_aor = getattr(session, '_sylk_inviter_aor', None)
            if inviter_aor:
                invitee_aor = self._session_aor(session)
                if invitee_aor:
                    self._invitee_inviter[invitee_aor] = inviter_aor
                    log.info('Room %s - tracking invitee %s (invited by %s) for anti-fraud eviction' %
                             (self.uri, invitee_aor, inviter_aor))
        except Exception as e:
            log.warning('Room %s - add_session: anti-fraud registration raised: %s' % (self.uri, e))
        try:
            joining_aor = self._session_aor(session)
            if joining_aor and self._pending_evictions:
                for ev_invitee_aor, ev in list(self._pending_evictions.items()):
                    if ev.inviter_aor == joining_aor:
                        ev.cancel('inviter rejoined the room')
                        self._pending_evictions.pop(ev_invitee_aor, None)
        except Exception as e:
            log.warning('Room %s - add_session: inviter-rejoin sweep raised: %s' % (self.uri, e))
        # Assign a stable identifier for this session. Used by the
        # conference admin API and published in the conference-info
        # NOTIFY payload as <agp-conf:participant_id>. Disambiguates
        # multiple devices that share the same AoR.
        #
        # Prefer the device's own identity advertised in its Contact
        # header (the RFC 5626 +sip.instance / instance-id): that way the
        # participant_id IS the device id the UA chose, stays consistent
        # for that physical device, and lets other components correlate a
        # participant to its device instead of an opaque server token. We
        # only fall back to a generated token when the UA didn't advertise
        # an instance-id, or when the advertised id would collide with
        # another live session in this room (e.g. several endpoints reusing
        # one +sip.instance), since participant_id must stay unique here.
        if not getattr(session, '_sylk_participant_id', None):
            device_id = self._device_id_from_contact(session)
            if device_id and any(getattr(other, '_sylk_participant_id', None) == device_id
                                  for other in self.sessions if other is not session):
                log.info('Room %s - device id %r already in use by another session, generating a token instead' % (self.uri, device_id))
                device_id = None
            session._sylk_participant_id = device_id or secrets.token_urlsafe(8)
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
            log.info('Room %s - audio stream %s/%sHz, end-points: %s:%d <-> %s:%d' % (self.uri, audio_stream.codec, audio_stream.sample_rate,
                                                                                      audio_stream.local_rtp_address, audio_stream.local_rtp_port,
                                                                                      audio_stream.remote_rtp_address, audio_stream.remote_rtp_port))
            if audio_stream.encryption.type != 'ZRTP':
                # We don't listen for stream notifications early enough
                if audio_stream.encryption.active:
                    log.info('Room %s - %s audio stream enabled %s encryption' % (self.uri,
                                                                                  format_identity(session.remote_identity),
                                                                                  audio_stream.encryption.type))
                else:
                    log.info('Room %s - %s audio stream did not enable encryption' % (self.uri,
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
                txt = 'Room %s - %s is uploading file %s (%s)' % (self.uri, format_identity(session.remote_identity), filename,self.format_file_size(transfer_stream.file_selector.size))
            else:
                filename = os.path.basename(transfer_stream.file_selector.name)
                txt = 'Room %s - %s requested file %s' % (self.uri, format_identity(session.remote_identity), filename)
            log.info(txt)
            self.dispatch_server_message(txt)
            if len(session.streams) == 1:
                return

        welcome_handler = WelcomeHandler(self, initial=True, session=session, streams=session.streams)
        welcome_handler.run()
        self.dispatch_conference_info()

        if len(self.sessions) == 1:
            log.info('Room %s - started by %s with %s' % (self.uri, format_identity(session.remote_identity), self.format_stream_types(session.streams)))
        else:
            log.info('Room %s - %s joined with %s' % (self.uri, format_identity(session.remote_identity), self.format_stream_types(session.streams)))
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
        # Anti-fraud bookkeeping on departure. Three independent
        # actions, all gated on the leaving session's AoR; we compute
        # it once and reuse. None of these raise on missing entries,
        # so a non-tracked session (incoming dial-in, SIP user with
        # no recorded inviter) flows straight through.
        try:
            leaving_aor = self._session_aor(session)
            if leaving_aor:
                # (1) If the leaver IS a tracked invitee, drop
                #     its inviter mapping. The leg is gone — there is
                #     nothing left to evict and the inviter no longer
                #     owes anything for it.
                self._invitee_inviter.pop(leaving_aor, None)
                # (2) Same target: cancel any eviction timer that was
                #     armed for this invitee. Defensive — normally the
                #     invitee leaving WHILE a timer is armed means the
                #     leg ended voluntarily inside the grace period.
                _pending = self._pending_evictions.pop(leaving_aor, None)
                if _pending is not None:
                    _pending.cancel('invitee left voluntarily')
                # (3) Did the leaver own any other sessions in the
                #     room? If not — i.e. their last device just
                #     dropped — they were the inviter of one or more
                #     tracked invitees who are still here, arm an
                #     eviction timer for each. Match by AoR so all
                #     devices of the inviter count as "still here".
                still_present_aors = set()
                for s in self.sessions:
                    a = self._session_aor(s)
                    if a:
                        still_present_aors.add(a)
                if leaving_aor not in still_present_aors:
                    grace = int(getattr(ConferenceConfig, 'inviter_eviction_grace_period', 0) or 0)
                    if grace > 0:
                        for invitee_aor, inviter_aor in list(self._invitee_inviter.items()):
                            if inviter_aor != leaving_aor:
                                continue
                            if invitee_aor in self._pending_evictions:
                                # Already armed (e.g. inviter had
                                # multiple sessions and we already
                                # processed their previous one).
                                continue
                            # The invitee must actually still be in
                            # the room — if they had left already
                            # action (2) above on THEIR removal would
                            # have cleared the mapping. Belt and
                            # braces in case of out-of-order events.
                            if invitee_aor not in still_present_aors:
                                self._invitee_inviter.pop(invitee_aor, None)
                                continue
                            self._pending_evictions[invitee_aor] = _InviterEviction(self, invitee_aor, inviter_aor, grace)
        except Exception as e:
            log.warning('Room %s - remove_session: anti-fraud bookkeeping raised: %s' % (self.uri, e))
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
            elif len(self.audio_conference.streams) == 1 and not self.config.disable_music_on_hold:
                self.moh_player.play()
        try:
            next(stream for stream in session.streams if stream.type == 'file-transfer')
        except StopIteration:
            pass
        else:
            if len(session.streams) == 1:
                return

        self.dispatch_conference_info()
        log.info('Room %s - %s left conference after %s' % (self.uri, format_identity(session.remote_identity), self.format_session_duration(session)))
        if not self.sessions:
            log.info('Room %s - Last participant left conference' % self.uri)
        if str(session.remote_identity.uri) not in set(str(s.remote_identity.uri) for s in self.sessions if s is not session):
            self.dispatch_server_message('%s has left the room after %s' % (format_identity(session.remote_identity), self.format_session_duration(session)))

        if ServerConfig.enable_bonjour:
            self._update_bonjour_presence()

    def terminate_sessions(self, uri, participant_id=None):
        if not self.started:
            return
        # Per-device path: when a participant_id token is supplied (a
        # REFER ;method=BYE carrying ;participant_id=, or an admin kick by
        # pid) end ONLY the session bearing that token, so one device can
        # be removed without dropping its siblings on the same AoR. Reuses
        # the same resolver the mute path uses, so SIP and HTTP moderation
        # disambiguate devices identically.
        if participant_id:
            session, _audio_stream = self._find_audio_session(participant_id)
            if session is None:
                log.info('Room %s - terminate_sessions: no session matched participant_id %s' % (self.uri, participant_id))
                return
            log.info('Room %s - terminate_sessions: ending session for participant_id %s (%s)' % (
                self.uri, participant_id, session.remote_identity.uri))
            session.end()
            return
        # Match by AoR (user@host, lower-cased) rather than by full
        # SIPURI equality. SIPURI's __eq__ compares all attributes —
        # parameters, port, scheme — so a Refer-To URI built from the
        # REFER request never compared equal to the session's stored
        # remote_identity.uri (which always carries the tag/params
        # from the INVITE). The loop matched nothing and the kicked
        # participant kept their leg alive even though the gateway
        # had logged "removed from conference". Comparing AoRs gives
        # the BYE a chance to actually fire.
        def _aor(u):
            try:
                user = (u.user or b'').decode() if isinstance(u.user, bytes) else (u.user or '')
                host = (u.host or b'').decode() if isinstance(u.host, bytes) else (u.host or '')
            except Exception:
                return None
            if not user or not host:
                return None
            return '{}@{}'.format(user, host).lower()
        target_aor = _aor(uri)
        if target_aor is None:
            log.warning('Room %s - terminate_sessions: cannot derive AoR from %r' % (self.uri, uri))
            return
        # Also CANCEL any outgoing INVITEs this room issued via REFER
        # ;method=INVITE that are still ringing for the same target.
        # `self.sessions` only carries legs that have reached
        # SIPSessionDidStart — without this hook a REFER ;method=BYE
        # or admin-kick that arrives while the callee's phone is still
        # ringing would walk an empty match set and return silently,
        # and the callee would then be parked in the room if they
        # eventually answered. Lazy import to avoid the circular
        # conference/__init__ ↔ conference/room dependency.
        from sylk.applications.conference import IncomingReferralHandler
        pending_cancelled = IncomingReferralHandler.cancel_pending_invites(self.uri, target_aor)
        if pending_cancelled:
            log.info('Room %s - terminate_sessions: cancelled %d in-flight invite(s) to %s' %
                     (self.uri, pending_cancelled, target_aor))
        terminated = pending_cancelled
        for session in list(self.sessions):
            if _aor(session.remote_identity.uri) == target_aor:
                log.info('Room %s - terminate_sessions: ending session of %s' % (self.uri, target_aor))
                session.end()
                terminated += 1
        if terminated == 0:
            log.info('Room %s - terminate_sessions: no session matched %s' % (self.uri, target_aor))

    def handle_incoming_subscription(self, subscribe_request, data):
        log.info('Room %s - subscription from %s' % (self.uri, data.headers['From'].uri))
        if subscribe_request.event != b'conference':
            #log.info('Room %s - Subscription for event %s rejected: only conference event is supported' % (self.uri, subscribe_request.event))
            subscribe_request.reject(489)
            return
        subscriber_uri = data.headers['From'].uri
        self._subscription_uris[subscribe_request] = subscriber_uri
        NotificationCenter().add_observer(self, sender=subscribe_request)
        self.subscriptions.append(subscribe_request)
        try:
            hide_bridges = not self._is_videoroom_subscriber(subscriber_uri)
            subscribe_request.accept(conference.ConferenceDocument.content_type, self.build_conference_info(hide_bridges=hide_bridges))
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
        txt = '%s participant%s' % (num_str, '' if num==1 else 's')
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
        log.info('Room %s - %s %s stream enabled %s encryption' % (self.uri,
                                                                   format_identity(session.remote_identity),
                                                                   stream.type,
                                                                   stream.encryption.type))

    def _NH_RTPStreamDidNotEnableEncryption(self, notification):
        stream = notification.sender
        session = stream.session
        log.info('Room %s - %s %s stream did not enable encryption: %s' % (self.uri,
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
        log.info('Room %s - audio stream for session %s timed out' % (self.uri, format_identity(session.remote_identity)))
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
            try:
                image = base64.b64decode(message.content.encode())
            except AttributeError as e:
                image = message.content
            self.add_screen_image(message.sender, image)
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
            if nickname in list(self.session_nickname_map.values()) and (session not in self.session_nickname_map or self.session_nickname_map[session] != nickname):
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
        self._subscription_uris.pop(subscription, None)
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
                log.info('Room %s - %s has put the audio session on hold' % (self.uri, format_identity(session.remote_identity)))
            else:
                log.info('Room %s - %s has taken the audio session out of hold' % (self.uri, format_identity(session.remote_identity)))
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
            txt = '%s has added %s' % (format_identity(session.remote_identity), stream.type)
            log.info('Room %s - %s' % (self.uri, txt))
            self.dispatch_server_message(txt, exclude=session)
            if stream.type == 'audio':
                log.info('Room %s - audio stream %s/%sHz, end-points: %s:%d <-> %s:%d' % (self.uri, stream.codec, stream.sample_rate,
                                                                                          stream.local_rtp_address, stream.local_rtp_port,
                                                                                          stream.remote_rtp_address, stream.remote_rtp_port))
                if stream.encryption.type != 'ZRTP':
                    # We don't listen for stream notifications early enough
                    if stream.encryption.active:
                        log.info('Room %s - %s %s stream enabled %s encryption' % (self.uri,
                                                                                   format_identity(session.remote_identity),
                                                                                   stream.type,
                                                                                   stream.encryption.type))
                    else:
                        log.info('Room %s - %s %s stream did not enable encryption' % (self.uri,
                                                                                       format_identity(session.remote_identity),
                                                                                       stream.type))

        if notification.data.added_streams:
            welcome_handler = WelcomeHandler(self, initial=False, session=session, streams=notification.data.added_streams)
            welcome_handler.run()

        for stream in notification.data.removed_streams:
            notification.center.remove_observer(self, sender=stream)
            txt = '%s has removed %s' % (format_identity(session.remote_identity), stream.type)
            log.info('Room %s - %s' % (self.uri, txt))
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
                elif len(self.audio_conference.streams) == 1 and not self.config.disable_music_on_hold:
                    self.moh_player.play()
            if not session.streams:
                log.info('Room %s - %s has removed all streams, session will be terminated' % (self.uri, format_identity(session.remote_identity)))
                session.end()
        self.dispatch_conference_info()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        log.info('Room %s - Call transfer request rejected, REFER must be out of dialog (RFC4579 5.5)' % self.uri)
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


@implementer(IObserver)
class MoHPlayer(object):

    def __init__(self, conference):
        self.conference = conference
        self.files = None
        self.paused = None
        self._player = None

    def start(self):
        files = glob('%s/*.wav' % Resources.get('sounds/moh'))
        if not files:
            log.error('No files found, MoH is disabled')
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


@implementer(IObserver)
class WelcomeHandler(object):

    def __init__(self, room, initial, session, streams):
        self.room = room
        self.initial = initial
        self.session = session
        self.streams = streams
        self.procs = proc.RunningProcSet()

    def run(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)

        for stream in self.streams:
            if stream.type == 'audio':
                self.procs.spawn(self.audio_welcome, stream)
            elif stream.type == 'chat':
                self.procs.spawn(self.chat_welcome, stream)

        @run_in_green_thread
        def finalize():
            try:
                self.procs.waitall()
            finally:
                notification_center.remove_observer(self, sender=self.session)
                self.session = None
                self.streams = None
                self.room = None
                self.procs = None

        finalize()

    def play_file_in_player(self, player, file, delay):
        player.filename = file
        player.pause_time = delay
        try:
            player.play().wait()
        except WavePlayerError as e:
            log.warning('Error playing file %s: %s' % (file, e))

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
            if len(self.room.audio_conference.streams) == 1 and not self.room.config.disable_music_on_hold:
                self.room.moh_player.play()
            else:
                self.room.moh_player.pause()
        finally:
            player.stop()

    def chat_welcome(self, stream):
        user_count = len({str(s.remote_identity.uri) for s in self.room.sessions if s.remote_identity.uri != self.session.remote_identity.uri})
        if user_count == 0:
            participant_message = 'You are the first participant'
        elif user_count == 1:
            participant_message = 'There is one more participant'
        else:
            participant_message = 'There are {} more participants'.format(user_count)
        message = 'Welcome! {} in the conference. Others can join by using:\n\n'.format(participant_message)
        if self.room.config.advertise_xmpp_support:
            message += 'SIP/XMPP: {}\n'.format(self.room.uri)
        else:
            message += 'SIP: {}\n'.format(self.room.uri)
        if self.room.config.pstn_access_numbers:
            message += 'Phones: {}\n'.format(' or '.join(', '.join(sorted(self.room.config.pstn_access_numbers)).rsplit(', ', 1)))
        if self.room.config.webrtc_gateway_url:
            message += 'WEB: {}\n'.format(str(self.room.config.webrtc_gateway_url).replace('$room', self.room.uri))
        stream.send_message(message.rstrip(), 'text/plain', sender=self.room.identity, recipients=[self.room.identity])
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
                        message = 'Received ZRTP Short Authentication String: %s' % sas
                        # Don't set the remote identity, that way it will appear as a private message
                        ns = CPIMNamespace('urn:ag-projects:xml:ns:cpim', prefix='agp')
                        message_type = CPIMHeader('Message-Type', ns, 'status')
                        stream.send_message(message, 'text/plain', sender=self.room.identity, additional_headers=[message_type])

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


@implementer(IObserver)
class FileTransferHandler(object):

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

        from_header = FromHeader(SIPURI.new(room.identity.uri), 'Conference File Transfer')
        to_header = ToHeader(SIPURI.new(destination))
        extra_headers = []
        if ThorNodeConfig.enabled:
            extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
        extra_headers.append(Header('X-Originator-From', str(file.sender.uri)))
        extra_headers.append(SubjectHeader('File uploaded by %s' % file.sender))
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

