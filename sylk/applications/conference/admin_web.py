
"""
Administrative HTTP API for the conference application.

Provides a Klein-based JSON/SSE interface for moderators or external
tooling to inspect rooms and act on participants:

    GET    /rooms                                                   list rooms
    GET    /rooms/<room_uri>                                        room detail
    GET    /rooms/<room_uri>/audio-levels                           snapshot
    GET    /rooms/<room_uri>/audio-levels/stream                    SSE @ N Hz
    POST   /rooms/<room_uri>/participants/<participant>/mute        {"muted":bool}
    DELETE /rooms/<room_uri>/participants/<participant>             kick

Where `<participant>` is either the integer audio stream id (as returned
by the snapshot endpoint and used in the SIP conference info payload) or
the participant's AoR / full SIP URI (URL-encoded).

Authentication is bearer-style: when http_management_auth_secret is set,
clients must send an Authorization header whose value equals the secret.

This handler is modelled on the AdminWebHandler used by the
webrtcgateway application — it listens on its own port
(ConferenceConfig.http_management_interface) so the admin surface does
not share the public conference web port.
"""

import json
import urllib.parse

from application.notification import IObserver, NotificationCenter
from application.python.types import Singleton
from klein import Klein
from sipsimple.core import SIPCoreError, SIPURI
from twisted.internet import reactor
from twisted.web.server import Site, NOT_DONE_YET
from zope.interface import implementer

from sylk.applications.conference.configuration import ConferenceConfig
from sylk.applications.conference.logger import log


__all__ = 'AdminWebHandler', 'AuthError'


class AuthError(Exception):
    pass


def _safe_dumps(payload):
    """Serialize to bytes — Klein wants bytes from sync responses."""
    return json.dumps(payload).encode('utf-8')


@implementer(IObserver)
class AdminWebHandler(object, metaclass=Singleton):
    app = Klein()

    def __init__(self, application):
        self.application = application       # ConferenceApplication
        self.listener = None
        # room_uri (str) -> set(twisted.web.http.Request)
        self._level_subscribers = {}
        NotificationCenter().add_observer(self, name='ConferenceRoomAudioLevels')

    # ----- lifecycle -------------------------------------------------

    def start(self):
        addr = ConferenceConfig.http_management_interface
        if not addr:
            log.info('Conference admin API disabled (http_management_interface unset)')
            return
        host, port = addr
        try:
            # noinspection PyUnresolvedReferences
            self.listener = reactor.listenTCP(port, Site(self.app.resource()), interface=host)
        except Exception as e:
            log.error('Conference admin API failed to start on %s:%d: %s' % (host, port, e))
            self.listener = None
            return
        log.info('Conference admin API listening on http://%s:%d' % (host, port))

    def stop(self):
        try:
            NotificationCenter().remove_observer(self, name='ConferenceRoomAudioLevels')
        except KeyError:
            pass
        if self.listener is not None:
            self.listener.stopListening()
            self.listener = None
        # Close any in-flight SSE responses.
        for subs in list(self._level_subscribers.values()):
            for req in list(subs):
                try:
                    req.finish()
                except Exception:
                    pass
        self._level_subscribers.clear()

    # ----- notifications ---------------------------------------------

    def handle_notification(self, notification):
        if notification.name != 'ConferenceRoomAudioLevels':
            return
        room = notification.sender
        subs = self._level_subscribers.get(room.uri)
        if not subs:
            return
        payload = {
            'uri': room.uri,
            'levels': notification.data.levels,
        }
        line = ('data: ' + json.dumps(payload) + '\n\n').encode('utf-8')
        for req in list(subs):
            try:
                req.write(line)
            except Exception:
                subs.discard(req)

    # ----- auth ------------------------------------------------------

    @staticmethod
    def _extract_bearer(request):
        """Return the credential portion of an Authorization header value,
        accepting both `Bearer <token>` and the bare `<secret>` style used
        by the existing webrtcgateway admin handler. Returns None if no
        Authorization header was sent.
        """
        hdrs = request.requestHeaders.getRawHeaders('Authorization', default=None)
        if not hdrs:
            return None
        value = hdrs[0].strip()
        if value.lower().startswith('bearer '):
            return value[7:].strip()
        return value

    @staticmethod
    def _extract_room_token(request):
        """Pick the per-room token out of a request. We accept it in three
        places, in order of preference: an `X-Room-Token` header, the
        `?token=` query string, and the Authorization header (Bearer or
        bare). Whichever turns up first wins.
        """
        hdr = request.requestHeaders.getRawHeaders('X-Room-Token', default=None)
        if hdr:
            return hdr[0].strip()
        args = request.args.get(b'token') if request.args else None
        if args:
            value = args[0]
            if isinstance(value, bytes):
                value = value.decode('utf-8', errors='replace')
            return value.strip()
        return AdminWebHandler._extract_bearer(request)

    def _check_auth(self, request, room=None):
        """Authenticate `request`. Two paths are accepted:

        1. The global `http_management_auth_secret`, supplied via the
           Authorization header (bare or as `Bearer <secret>`). Works on
           every endpoint regardless of whether `room` is given.

        2. The per-room random token (`room.auth_token`), supplied via the
           Authorization header, the `X-Room-Token` header, or the
           `?token=` query parameter. Only accepted when `room` is not
           None, i.e. on endpoints scoped to a specific room — the token
           must match that exact room.

        Raises AuthError when neither check passes. When the global secret
        is unset and no `room` is provided, access is allowed (preserves
        the existing unsecured-by-default behaviour for the listing
        endpoints; deployments concerned about that should set the global
        secret).
        """
        secret = ConferenceConfig.http_management_auth_secret
        supplied = self._extract_room_token(request)
        if secret:
            if supplied and supplied == secret:
                return
        else:
            if room is None:
                return
        if room is not None:
            token = getattr(room, 'auth_token', None)
            if supplied and token and supplied == token:
                return
        raise AuthError()

    @app.handle_errors(AuthError)
    def auth_error(self, request, failure):
        request.setResponseCode(403)
        request.setHeader('Content-Type', 'application/json')
        return _safe_dumps({'error': 'authentication required'})

    # ----- helpers ---------------------------------------------------

    @staticmethod
    def _decode(value):
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        return urllib.parse.unquote(value)

    def _get_room(self, room_uri):
        return self.application._rooms.get(self._decode(room_uri))

    @staticmethod
    def _coerce_participant(value):
        """Decode the URL path segment and return it as-is.

        The room's _find_audio_session() interprets a plain identifier
        (no '@', no scheme) as the canonical participant_id token. AoR /
        full URI / Contact URI forms are also accepted. Numeric strings
        are NOT auto-cast to int — `id(audio_stream)` is internal-only
        and the token alphabet (token_urlsafe) is disjoint from a clean
        decimal integer in practice anyway.
        """
        return AdminWebHandler._decode(value)

    # ----- endpoints -------------------------------------------------

    @app.route('/', methods=['GET'])
    def index(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        return _safe_dumps({
            'service': 'sylkserver-conference-admin',
            'endpoints': [
                'GET    /rooms',
                'GET    /rooms/<room_uri>',
                'GET    /rooms/<room_uri>/audio-levels',
                'GET    /rooms/<room_uri>/audio-levels/stream',
                'POST   /rooms/<room_uri>/participants/<participant>/mute',
                'DELETE /rooms/<room_uri>/participants/<participant>',
            ],
            'participant_id_help': (
                'In /participants/<participant>, <participant> is the per-session '
                'participant_id token (preferred, published as <agp-conf:participant_id> '
                'in the conference-info NOTIFY payload). The full Contact URI, SIP URI '
                'or AoR (user@host) are also accepted; with multiple devices behind one '
                'AoR, only participant_id uniquely identifies a single device.'
            ),
        })

    @app.route('/rooms', methods=['GET'])
    def list_rooms(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        rooms = []
        for room in self.application._rooms.values():
            rooms.append({
                'uri': room.uri,
                'started': bool(room.started),
                'participants': len(room.sessions),
                'subject': getattr(room, 'subject', '') or '',
            })
        return _safe_dumps({'rooms': rooms})

    @app.route('/rooms/<string:room_uri>', methods=['GET'])
    def room_detail(self, request, room_uri):
        room = self._get_room(room_uri)
        self._check_auth(request, room=room)
        request.setHeader('Content-Type', 'application/json')
        if room is None:
            request.setResponseCode(404)
            return _safe_dumps({'error': 'no such room', 'uri': self._decode(room_uri)})
        return _safe_dumps({
            'uri': room.uri,
            'started': bool(room.started),
            'subject': getattr(room, 'subject', '') or '',
            'participants': room.get_participants(),
        })

    @app.route('/rooms/<string:room_uri>/audio-levels', methods=['GET'])
    def audio_levels_snapshot(self, request, room_uri):
        room = self._get_room(room_uri)
        self._check_auth(request, room=room)
        request.setHeader('Content-Type', 'application/json')
        if room is None:
            request.setResponseCode(404)
            return _safe_dumps({'error': 'no such room', 'uri': self._decode(room_uri)})
        return _safe_dumps({
            'uri': room.uri,
            'levels': room.audio_levels,
            'participants': room.get_participants(),
        })

    @app.route('/rooms/<string:room_uri>/audio-levels/stream', methods=['GET'])
    def audio_levels_stream(self, request, room_uri):
        """Server-Sent Events stream of {stream_id: {tx, rx}} updates.

        Each event is a single `data:` line carrying a JSON object with
        the room URI and the latest levels snapshot. The cadence matches
        ConferenceConfig.audio_level_sample_period.
        """
        room = self._get_room(room_uri)
        self._check_auth(request, room=room)
        if room is None:
            request.setResponseCode(404)
            request.setHeader('Content-Type', 'application/json')
            return _safe_dumps({'error': 'no such room', 'uri': self._decode(room_uri)})

        request.setHeader('Content-Type', 'text/event-stream')
        request.setHeader('Cache-Control', 'no-cache')
        request.setHeader('Connection', 'keep-alive')
        request.setHeader('X-Accel-Buffering', 'no')

        # Send the current snapshot up front so the client doesn't have
        # to wait a full sample period before seeing anything.
        try:
            initial = {'uri': room.uri, 'levels': room.audio_levels}
            request.write(('data: ' + json.dumps(initial) + '\n\n').encode('utf-8'))
        except Exception:
            pass

        subs = self._level_subscribers.setdefault(room.uri, set())
        subs.add(request)

        def _drop(_):
            subs.discard(request)
            if not subs:
                self._level_subscribers.pop(room.uri, None)

        request.notifyFinish().addBoth(_drop)
        return NOT_DONE_YET

    @app.route('/rooms/<string:room_uri>/participants/<string:participant>/mute',
               methods=['POST'])
    def mute_participant(self, request, room_uri, participant):
        room = self._get_room(room_uri)
        self._check_auth(request, room=room)
        request.setHeader('Content-Type', 'application/json')
        if room is None:
            request.setResponseCode(404)
            return _safe_dumps({'error': 'no such room', 'uri': self._decode(room_uri)})
        try:
            raw = request.content.read() or b'{}'
            body = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            request.setResponseCode(400)
            return _safe_dumps({'error': 'invalid JSON body'})
        if not isinstance(body, dict):
            request.setResponseCode(400)
            return _safe_dumps({'error': 'body must be a JSON object'})
        muted = bool(body.get('muted', True))
        ident = self._coerce_participant(participant)
        if not room.set_participant_muted(ident, muted):
            request.setResponseCode(404)
            return _safe_dumps({
                'error': 'no such participant',
                'participant': self._decode(participant),
            })
        return _safe_dumps({'success': True, 'muted': muted})

    @app.route('/rooms/<string:room_uri>/participants/<string:participant>',
               methods=['DELETE'])
    def kick_participant(self, request, room_uri, participant):
        room = self._get_room(room_uri)
        self._check_auth(request, room=room)
        request.setHeader('Content-Type', 'application/json')
        if room is None:
            request.setResponseCode(404)
            return _safe_dumps({'error': 'no such room', 'uri': self._decode(room_uri)})
        ident = self._coerce_participant(participant)

        # First try the room's own lookup — handles participant_id, Contact
        # URI, AoR and stream id uniformly. This is what lets us target a
        # specific device when two share the same AoR.
        target_session, _ = room._find_audio_session(ident)
        if target_session is not None:
            target_uri = target_session.remote_identity.uri
        elif isinstance(ident, str) and ('@' in ident or ident.lower().startswith(('sip:', 'sips:'))):
            # Fallback: caller gave a URI that doesn't match any current
            # session (maybe it's a partially-joined refer). Hand it to
            # terminate_sessions which compares by AoR anyway.
            value = ident if ident.lower().startswith(('sip:', 'sips:')) else 'sip:' + ident
            try:
                target_uri = SIPURI.parse(value)
            except SIPCoreError:
                request.setResponseCode(400)
                return _safe_dumps({'error': 'invalid participant identifier',
                                    'participant': self._decode(participant)})
        else:
            request.setResponseCode(404)
            return _safe_dumps({
                'error': 'no such participant',
                'participant': self._decode(participant),
            })

        room.terminate_sessions(target_uri)
        log.info('Room %s - participant %s kicked by admin API' % (room.uri, target_uri))
        return _safe_dumps({'success': True})
