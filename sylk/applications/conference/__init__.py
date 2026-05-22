
import os
import re
import shutil

from application.notification import IObserver, NotificationCenter
from application.python import Null
from eventlib import proc
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.core import Header, FromHeader, ToHeader, SubjectHeader
from sipsimple.lookup import DNSLookup
from sipsimple.streams import MediaStreamRegistry
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from zope.interface import implementer

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


def _uri_field(value):
    """Helper: SIPURI.user/.host may be bytes or str depending on call path."""
    if isinstance(value, bytes):
        return value.decode()
    return value


class _RoomTargetURI(object):
    """Minimal user@host carrier with .user/.host as plain strings.

    SIPURI.parse(...) and SIPURI(user=..., host=...) both yield objects whose
    .user/.host attributes are bytes in this build of sipsimple, which breaks
    `'%s@%s' % (uri.user, uri.host)` formatting (it produces "b'…'@b'…'").
    The session.request_uri objects that arrive via real SIP parsing have
    these attributes as strings, which is what ConferenceApplication.get_room,
    validate_acl, and _NH_SIPSessionDidEnd assume. We only need .user and
    .host here, so a tiny proxy is the safest fix.
    """

    def __init__(self, user, host):
        self.user = user
        self.host = host

    def __repr__(self):
        return '<_RoomTargetURI %s@%s>' % (self.user, self.host)


class ACLValidationError(Exception): pass

class RoomNotFoundError(Exception): pass


@implementer(IObserver)
class ConferenceApplication(SylkApplication):

    def __init__(self):
        self._rooms = {}
        self.invited_participants_map = {}
        # Maps (selector_uri_str, caller_from_uri_str) -> real room_uri_str.
        # Populated when a caller is routed through the conference selector IVR;
        # used so in-dialog SUBSCRIBE requests for the selector URI can be
        # routed to the actual room the caller joined.
        self._selector_redirects = {}
        self.bonjour_focus_service = Null
        self.bonjour_room_service = Null
        self.web = Null

    def start(self):
        self.web = ConferenceWeb(self)
        web_server.register_resource(b'conference', self.web.resource)

        # We listen to SIPSessionNewIncoming directly so we can capture the
        # original INVITE headers on the session. The application loader only
        # passes us the Session object via incoming_session(session), which
        # doesn't expose arbitrary headers. Registering here, before the
        # loader installs its own observer, means our handler runs first.
        NotificationCenter().add_observer(self, name='SIPSessionNewIncoming')

        # cleanup old files
        for path in (ConferenceConfig.file_transfer_dir, ConferenceConfig.screensharing_images_dir):
            try:
                shutil.rmtree(path)
            except EnvironmentError:
                pass

        if ServerConfig.enable_bonjour and ServerConfig.default_application == 'conference':
            self.bonjour_focus_service = BonjourService(service='sipfocus')
            self.bonjour_focus_service.start()
            log.info("Bonjour publication started for service 'sipfocus'")
            self.bonjour_room_service = BonjourService(service='sipuri', name='Conference Room', uri_user='conference')
            self.bonjour_room_service.start()
            self.bonjour_room_service.presence_state = BonjourPresenceState('available', 'No participants')
            log.info("Bonjour publication started for service 'sipuri'")

    def stop(self):
        try:
            NotificationCenter().remove_observer(self, name='SIPSessionNewIncoming')
        except KeyError:
            pass
        self.bonjour_focus_service.stop()
        self.bonjour_room_service.stop()

    def _NH_SIPSessionNewIncoming(self, notification):
        # Stash the INVITE headers on the session so incoming_session() can
        # inspect them. The application loader fires our incoming_session
        # synchronously in this notification fan-out, but its handler is
        # decorated @run_in_twisted_thread (so deferred), while ours is sync —
        # we therefore run first within the notification post and the session
        # has the attribute set by the time incoming_session is invoked.
        notification.sender._sylk_invite_headers = notification.data.headers

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

    # --- conference selector redirects --------------------------------------
    #
    # When the IVR hands a session off, we remember which room each caller
    # actually landed in, keyed by (selector_uri, caller_from_uri). This lets
    # in-dialog SUBSCRIBE requests (which still carry the selector URI in the
    # Request-URI / To header) find the right room.

    @staticmethod
    def _uri_key(uri):
        return '%s@%s' % (_uri_field(uri.user), _uri_field(uri.host))

    def register_selector_redirect(self, session, target_uri):
        selector_key = self._uri_key(session.request_uri)
        from_key = str(session.remote_identity.uri)
        target_key = self._uri_key(target_uri)
        self._selector_redirects[(selector_key, from_key)] = target_key
        log.info('select_conference: redirect registered %s/%s -> %s' %
                 (selector_key, from_key, target_key))

    def unregister_selector_redirect(self, session):
        selector_key = self._uri_key(session.request_uri)
        from_key = str(session.remote_identity.uri)
        self._selector_redirects.pop((selector_key, from_key), None)

    def _lookup_selector_redirect(self, request_uri, from_uri):
        if request_uri is Null or from_uri is Null:
            return None
        try:
            selector_key = self._uri_key(request_uri)
        except AttributeError:
            return None
        target_key = self._selector_redirects.get((selector_key, str(from_uri)))
        if target_key is None:
            return None
        return self._rooms.get(target_key)

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

    @staticmethod
    def _should_disable_moh(session):
        """Decide whether the INVITE asks for MoH to be off in its room.

        Only checks ConferenceConfig.moh_disable_header — when set, an INVITE
        carrying that header with body 'Yes' (case-insensitive) disables MoH
        for the room the caller joins. The global / per-room
        `disable_music_on_hold` setting is already honored by the room config
        itself; this helper only deals with the per-INVITE header override.
        """
        header_name = ConferenceConfig.moh_disable_header
        if not header_name:
            return False
        headers = getattr(session, '_sylk_invite_headers', None) or {}
        h = headers.get(header_name)
        if h is None:
            return False
        body = getattr(h, 'body', h)
        if isinstance(body, bytes):
            try:
                body = body.decode()
            except Exception:
                return False
        return str(body).strip().lower() == 'yes'

    def incoming_session(self, session):
        peer = '%s:%s' % (session.transport, session.peer_address)
        log.info('Session %s from %s: %s -> %s' % (session.call_id, peer, session.remote_identity.uri, session.local_identity.uri))
        settings = SIPSimpleSettings()

        # Decide MoH disable up front while we still have easy access to the
        # invite headers. The flag is carried on the session and consumed when
        # the room is created in _NH_SIPSessionDidStart / IVR handoff. The
        # global default (ConferenceConfig.disable_music_on_hold) and any
        # per-room override come from RoomConfig and apply automatically.
        if self._should_disable_moh(session):
            session._sylk_disable_moh = True
            log.info('Session %s: music-on-hold disabled by %r header' %
                     (session.call_id, ConferenceConfig.moh_disable_header))

        audio_streams = [stream for stream in session.proposed_streams if stream.type=='audio']
        chat_streams = [stream for stream in session.proposed_streams if stream.type=='chat']
        transfer_streams = [stream for stream in session.proposed_streams if stream.type=='file-transfer']
        if not audio_streams and not chat_streams and not transfer_streams:
            log.info(u'Session rejected: invalid media')
            session.reject(488)
            return
        audio_stream = audio_streams[0] if audio_streams else None
        chat_stream = chat_streams[0] if chat_streams else None
        transfer_stream = transfer_streams[0] if transfer_streams else None

        # Detect the conference selector pseudo-room (default user 'conference',
        # configurable via ConferenceConfig.default_conference_selector). We do
        # not yet know which actual conference room to join: prompt the caller,
        # collect DTMF, then continue into <digits>@<same domain>.
        if _uri_field(session.request_uri.user) == ConferenceConfig.default_conference_selector:
            if audio_stream is None:
                log.info('Session rejected: conference selector requires an audio stream')
                session.reject(488)
                return
            # ACL is validated later, against the actual room URI chosen by
            # the caller. We do not validate against select_conference itself.
            handler = SelectConferenceHandler(self, session, audio_stream, chat_stream)
            handler.start()
            return

        try:
            self.validate_acl(session.request_uri, session.remote_identity.uri)
        except ACLValidationError:
            log.info('Session rejected: unauthorized by access list')
            session.reject(403)
            return

        if transfer_stream is not None:
            try:
                room = self.get_room(session.request_uri)
            except RoomNotFoundError:
                log.info('Session rejected: room not found')
                session.reject(404)
                return
            if transfer_stream.direction == 'sendonly':
                # file transfer 'pull'
                try:
                    file = next(file for file in room.files if file.hash == transfer_stream.file_selector.hash)
                except StopIteration:
                    log.info('Session rejected: requested file not found')
                    session.reject(404)
                    return
                try:
                    transfer_stream.file_selector = file.file_selector
                except EnvironmentError as e:
                    log.info('Session rejected: error opening requested file: %s' % e)
                    session.reject(404)
                    return
            else:
                transfer_stream.handler.save_directory = os.path.join(settings.file_transfer.directory.normalized, room.uri)

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

        if subscribe_request.event != b'conference':
            log.info('Subscription for event %s rejected: only conference event is supported' % subscribe_request.event)
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
                    log.info('Subscription rejected: unauthorized by access list')
                    subscribe_request.reject(403)
                    return
        try:
            room = self.get_room(data.request_uri)
        except RoomNotFoundError:
            try:
                room = self.get_room(to_header.uri)
            except RoomNotFoundError:
                # Callers that came in via the conference selector IVR have
                # their dialog established to <selector>@<host>, but the real
                # room they joined is <digits>@<host>. Try the redirect map.
                room = self._lookup_selector_redirect(data.request_uri, from_header.uri) \
                    or self._lookup_selector_redirect(to_header.uri, from_header.uri)
                if room is None:
                    log.info('Subscription rejected: room not yet created')
                    subscribe_request.reject(480)
                    return
        if not room.started:
            log.info('Subscription rejected: room not started yet')
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

        log.info('Room %s - join request from %s to %s' % ('%s@%s' % (to_header.uri.user, to_header.uri.host), from_header.uri, refer_to_header.uri))

        try:
            self.validate_acl(data.request_uri, from_header.uri)
        except ACLValidationError:
            log.info('Room %s - invite participant request rejected: unauthorized by access list' % data.request_uri)
            refer_request.reject(403)
            return
        referral_handler = IncomingReferralHandler(refer_request, data)
        referral_handler.start()

    def incoming_message(self, message_request, data):
        log.info('SIP MESSAGE is not supported, use MSRP media instead')
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
        log.info('Room %s - outgoing session to %s started' % (room_uri_str, session.remote_identity.uri))
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
            log.info('Room %s - %s removed from conference' % (room_uri, participant_uri))
            room.terminate_sessions(participant_uri)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        room_uri = getattr(session, '_sylk_conference_target_uri', None) or session.request_uri
        room = self.get_room(room_uri, True)
        # The global ConferenceConfig.disable_music_on_hold overrides any
        # per-room setting; an INVITE carrying the configured MoH-disable
        # header also forces MoH off for the room's lifetime.
        if ConferenceConfig.disable_music_on_hold or getattr(session, '_sylk_disable_moh', False):
            room.config.disable_music_on_hold = True
        room.start()
        room.add_session(session)

    @run_in_green_thread
    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        notification.center.remove_observer(self, sender=session)
        if session.direction == 'incoming':
            # A session that came in through the conference selector IVR is
            # actually parked in a different room than its SIP Request-URI
            # indicates.
            if getattr(session, '_sylk_conference_target_uri', None) is not None:
                self.unregister_selector_redirect(session)
            room_uri = getattr(session, '_sylk_conference_target_uri', None) or session.request_uri
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
        log.info('Session from %s failed: %s (%s)' % (session.remote_identity.uri, notification.data.reason, notification.data.failure_reason))


@implementer(IObserver)
class SelectConferenceHandler(object):
    """Conference IVR.

    Plays "please enter the conference number" (from asterisk-core-sounds-en-wav)
    on the audio stream, collects DTMF, then transfers the session over to the
    conference application's normal room-join flow as if the caller had dialed
    <digits>@<same host> in the first place.

    Digit collection rules:
        - '#' terminates the input and submits the collected digits.
        - '*' aborts the call (plays goodbye and hangs up).
        - Inter-digit silence beyond `select_conference_interdigit_timeout`
          submits whatever has been collected so far.
        - No digits at all by `select_conference_initial_timeout` ends the call.
        - An absolute timer (`select_conference_overall_timeout`) caps the IVR.
    """

    def __init__(self, application, session, audio_stream, chat_stream):
        self.application = application
        self.session = session
        self.audio_stream = audio_stream
        self.chat_stream = chat_stream
        self.digits = ''
        self.player = None
        self.play_proc = None
        self.interdigit_timer = None
        self.overall_timer = None
        self.finalized = False
        self.handed_off = False

    # --- lifecycle -----------------------------------------------------------

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)
        self.session.send_ring_indication()
        streams = [s for s in (self.audio_stream, self.chat_stream) if s is not None]
        # Match the small delay used by the regular conference flow so the
        # client has time to set up its audio pipeline before we start playing.
        reactor.callLater(2, self._accept_session, streams)

    def _accept_session(self, streams):
        if self.session is None or self.session.state != 'incoming':
            return
        try:
            # Accept with is_focus=True. select_conference IS a conference — we
            # just don't know which room yet — so the Contact header should
            # advertise ;isfocus from the start. Without this the client tends
            # to send a follow-up re-INVITE, and a late-arriving proposal can
            # race the handoff into the actual room.
            self.session.accept(streams, is_focus=True)
        except IllegalStateError:
            pass

    def _cancel_timers(self):
        if self.interdigit_timer is not None and self.interdigit_timer.active():
            self.interdigit_timer.cancel()
        self.interdigit_timer = None
        if self.overall_timer is not None and self.overall_timer.active():
            self.overall_timer.cancel()
        self.overall_timer = None

    def _cleanup(self):
        self._cancel_timers()
        notification_center = NotificationCenter()
        if self.session is not None:
            notification_center.discard_observer(self, sender=self.session)
        if self.audio_stream is not None:
            notification_center.discard_observer(self, sender=self.audio_stream)
        if self.play_proc is not None:
            try:
                self.play_proc.kill()
            except Exception:
                pass
            self.play_proc = None

    # --- audio prompts -------------------------------------------------------

    def _sounds_path(self, filename):
        return os.path.join(ConferenceConfig.asterisk_sounds_dir.normalized, filename)

    def _play_prompt(self, filename):
        """Play a prompt file on the audio stream. Runs in a green thread."""
        if self.audio_stream is None:
            return
        path = self._sounds_path(filename)
        if not os.path.isfile(path):
            log.warning('select_conference: prompt file not found: %s' % path)
            return
        player = WavePlayer(self.audio_stream.mixer, path, pause_time=0, initial_delay=0, volume=80)
        self.player = player
        self.audio_stream.bridge.add(player)
        try:
            player.play().wait()
        except (ValueError, WavePlayerError) as e:
            log.warning('select_conference: error playing %s: %s' % (path, e))
        except proc.ProcExit:
            pass
        finally:
            try:
                self.audio_stream.bridge.remove(player)
            except Exception:
                pass
            player.stop()
            if self.player is player:
                self.player = None

    def _spawn_prompt(self, filename):
        # Spawn so we don't block the reactor thread; DTMF events keep flowing.
        return proc.spawn(self._play_prompt, filename)

    # --- DTMF / timers -------------------------------------------------------

    def _start_collection(self):
        # Overall safety net regardless of digit activity.
        self.overall_timer = reactor.callLater(
            ConferenceConfig.select_conference_overall_timeout,
            self._on_overall_timeout,
        )
        # No-input timer until the first digit arrives.
        self.interdigit_timer = reactor.callLater(
            ConferenceConfig.select_conference_initial_timeout,
            self._on_interdigit_timeout,
        )

    def _restart_interdigit_timer(self):
        if self.interdigit_timer is not None and self.interdigit_timer.active():
            self.interdigit_timer.cancel()
        self.interdigit_timer = reactor.callLater(
            ConferenceConfig.select_conference_interdigit_timeout,
            self._on_interdigit_timeout,
        )

    def _on_interdigit_timeout(self):
        if self.finalized:
            return
        if not self.digits:
            log.info('select_conference: no input from %s, hanging up' % self.session.remote_identity.uri)
            self._abort('no input')
        else:
            self._finalize()

    def _on_overall_timeout(self):
        if self.finalized:
            return
        log.info('select_conference: overall timeout for %s' % self.session.remote_identity.uri)
        if self.digits:
            self._finalize()
        else:
            self._abort('overall timeout')

    # --- finalize / hand off -------------------------------------------------

    def _finalize(self):
        if self.finalized:
            return
        self.finalized = True
        self._cancel_timers()
        digits = self.digits

        # Sanity-check the collected digits: must be non-empty and digits-only.
        if not digits or not re.match(r'^[0-9]+$', digits):
            log.info('select_conference: invalid input %r' % digits)
            self._play_invalid_and_hangup()
            return

        host = _uri_field(self.session.request_uri.host)
        # Use a lightweight proxy whose .user/.host are guaranteed str. The
        # downstream consumers (get_room, validate_acl, _NH_SIPSessionDidEnd)
        # only ever read .user and .host, so this is sufficient and avoids
        # sipsimple's bytes-typed SIPURI attributes.
        target_uri = _RoomTargetURI(user=digits, host=host)

        # Validate ACL for the *real* room before we hand off.
        try:
            self.application.validate_acl(target_uri, self.session.remote_identity.uri)
        except ACLValidationError:
            log.info('select_conference: %s denied access to %s@%s by ACL' %
                     (self.session.remote_identity.uri, digits, host))
            self._play_invalid_and_hangup()
            return

        log.info('select_conference: %s selected room %s@%s' %
                 (self.session.remote_identity.uri, digits, host))

        # Stop the prompt if it is still running, then hand off ownership of
        # the session to the conference application.
        if self.play_proc is not None:
            try:
                self.play_proc.kill()
            except Exception:
                pass
            self.play_proc = None

        self._handoff(target_uri)

    def _handoff(self, target_uri):
        """Transfer observer ownership and place session into the chosen room."""
        self.handed_off = True
        session = self.session
        # Tag the session so the application's room lookup in
        # _NH_SIPSessionDidEnd routes to the room the caller actually joined,
        # not <selector>@host.
        session._sylk_conference_target_uri = target_uri

        # Register the selector -> real-room redirect so in-dialog SUBSCRIBE
        # requests (which still carry the selector URI) can find the room.
        self.application.register_selector_redirect(session, target_uri)

        notification_center = NotificationCenter()
        notification_center.discard_observer(self, sender=session)
        if self.audio_stream is not None:
            notification_center.discard_observer(self, sender=self.audio_stream)

        # Mirror what _NH_SIPSessionDidStart does for a normal incoming session.
        notification_center.add_observer(self.application, sender=session)
        room = self.application.get_room(target_uri, create=True)
        # Honor the same global override + per-INVITE header as the direct-dial
        # path (see ConferenceApplication._NH_SIPSessionDidStart).
        if ConferenceConfig.disable_music_on_hold or getattr(session, '_sylk_disable_moh', False):
            room.config.disable_music_on_hold = True
        room.start()
        room.add_session(session)

        # Drop references so we can be garbage collected.
        self.session = None
        self.audio_stream = None
        self.chat_stream = None

    def _play_invalid_and_hangup(self):
        # Best-effort: play the "invalid" prompt then hang up.
        @run_in_green_thread
        def run():
            try:
                self._play_prompt(ConferenceConfig.select_conference_invalid_prompt)
            finally:
                self._end_session()
        run()

    def _abort(self, reason):
        if self.finalized:
            return
        self.finalized = True
        self._cancel_timers()
        log.info('select_conference: aborting (%s)' % reason)

        @run_in_green_thread
        def run():
            try:
                self._play_prompt(ConferenceConfig.select_conference_goodbye_prompt)
            finally:
                self._end_session()
        run()

    def _end_session(self):
        if self.session is not None:
            try:
                self.session.end()
            except Exception:
                pass

    # --- notification handlers -----------------------------------------------

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        log.info('select_conference: session %s started, prompting %s' %
                 (session.call_id, session.remote_identity.uri))
        # Observe the audio stream for DTMF events.
        try:
            audio_stream = next(s for s in session.streams if s.type == 'audio')
        except StopIteration:
            log.warning('select_conference: no audio stream after start, aborting')
            self._abort('no audio')
            return
        self.audio_stream = audio_stream
        NotificationCenter().add_observer(self, sender=self.audio_stream)
        self._start_collection()
        self.play_proc = self._spawn_prompt(ConferenceConfig.select_conference_prompt)

    def _NH_SIPSessionDidFail(self, notification):
        log.info('select_conference: session failed: %s' % notification.data.reason)
        self._cleanup()

    def _NH_SIPSessionDidEnd(self, notification):
        if not self.handed_off:
            log.info('select_conference: session ended before selection')
        self._cleanup()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        notification.sender.reject_transfer(403)

    @run_in_twisted_thread
    def _NH_AudioStreamGotDTMF(self, notification):
        # DTMF notifications arrive on the SIP audio thread. We must bounce
        # onto the Twisted thread before touching reactor timers or spawning
        # eventlib greenlets (room.start() -> proc.spawn requires the
        # eventlib hub, which is wired up only in the reactor thread).
        if self.finalized:
            return
        digit = notification.data.digit
        if isinstance(digit, bytes):
            digit = digit.decode()
        log.info('select_conference: got DTMF %r (collected so far: %r)' % (digit, self.digits))

        if digit == '#':
            # Terminator: submit what we have.
            self._finalize()
            return
        if digit == '*':
            # Cancel.
            self._abort('user pressed *')
            return
        if digit in '0123456789':
            self.digits += digit
            if len(self.digits) >= ConferenceConfig.select_conference_max_digits:
                self._finalize()
                return
            self._restart_interdigit_timer()
            return
        # Letters / other DTMF — ignore but reset the silence timer so the
        # caller has time to enter the actual digits.
        self._restart_interdigit_timer()


@implementer(IObserver)
class IncomingReferralHandler(object):

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
            log.info('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
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
            log.info('Room %s - %s removed %s from the room' % (self.room_uri_str, self._refer_headers.get('From').uri, self.refer_to_uri))
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
            log.info('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
            self._refer_request.end(500)
            return
        active_media = set(room.active_media).intersection(('audio', 'chat'))
        if not active_media:
            log.info('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
            self._refer_request.end(500)
            return
        for stream_type in active_media:
            self.streams.append(MediaStreamRegistry.get(stream_type)())
        self.session = Session(account)
        notification_center.add_observer(self, sender=self.session)
        original_from_header = self._refer_headers.get('From')
        if original_from_header.display_name:
            original_identity = "%s <%s@%s>" % (original_from_header.display_name, original_from_header.uri.user, original_from_header.uri.host)
        else:
            original_identity = "%s@%s" % (original_from_header.uri.user, original_from_header.uri.host)
        from_header = FromHeader(SIPURI.new(self.room_uri), 'Conference Call')
        to_header = ToHeader(self.refer_to_uri)
        extra_headers = []
        if ThorNodeConfig.enabled:
            extra_headers.append(Header('Thor-Scope', 'conference-invitation'))
        extra_headers.append(Header('X-Originator-From', str(original_from_header.uri)))
        extra_headers.append(SubjectHeader('Join conference request from %s' % original_identity))
        if self._refer_headers.get('Referred-By', None) is not None:
            extra_headers.append(Header.new(self._refer_headers.get('Referred-By')))
        else:
            extra_headers.append(Header('Referred-By', str(original_from_header.uri)))
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
        log.info('Room %s - %s added %s' % (self.room_uri_str, self._refer_headers.get('From').uri, self.refer_to_uri))
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidFail(self, notification):
        log.info('Room %s - failed to add %s: %s' % (self.room_uri_str, self.refer_to_uri, notification.data.reason))
        notification.center.remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(notification.data.code or 500, notification.data.reason or  notification.data.code)
        self.session = None
        self.streams = []

    def _NH_SIPSessionDidEnd(self, notification):
        # If any stream fails to start we won't get SIPSessionDidFail, we'll get here instead
        log.info('Room %s - failed to add %s' % (self.room_uri_str, self.refer_to_uri))
        notification.center.remove_observer(self, sender=notification.sender)
        if self._refer_request is not None:
            self._refer_request.end(200)
        self.session = None
        self.streams = []

    def _NH_SIPIncomingReferralDidEnd(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self._refer_request = None


