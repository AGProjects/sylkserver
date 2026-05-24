
"""
UDP fanout for real-time audio-level updates.

Runs as a UDP **server**: subscribers (typically a remote webrtcgateway,
running on a different host) send a `subscribe` datagram, the server
remembers the (source_addr, room_uri) tuple with an expiry, and then
each time the conference fires a ConferenceRoomAudioLevels notification
the server sends a `levels` datagram to every live subscription whose
room URI matches. Subscribers refresh their interest with a periodic
heartbeat; entries that stop heartbeating are dropped after their TTL.

The endpoint of this server is published inside every conference-info
NOTIFY (on the audio-bridge participant's User element, as
`agp-conf:audio_levels_udp_endpoint`), so the webrtcgateway discovers
where to subscribe purely by reading the NOTIFY it already receives —
no static config on either side beyond the listen address and the
shared token.

Wire protocol (JSON-encoded datagrams):

  client → server   subscribe / heartbeat
      {"type":"subscribe",   "room_uri":"…", "token":"…", "ttl":60}

  client → server   explicit teardown (optional — TTL handles it too)
      {"type":"unsubscribe", "room_uri":"…", "token":"…"}

  server → client   level updates (one datagram per notify tick per sub)
      {"type":"audio-levels",
       "room_uri":"…",
       "levels": {"<pid>":{"tx":…,"rx":…,"tx_peak":…,"rx_peak":…}, …},
       "ts": <ms-since-epoch>}

  server → client   ack of subscribe (optional, fires once per subscribe)
      {"type":"subscribed", "room_uri":"…", "ttl":60}

All datagrams are authenticated by the shared `token`. The token check
is mandatory on inbound packets; outbound packets do not carry the
token. Mismatched tokens are dropped silently — UDP packets can be
spoofed, the token is the only line of defence.
"""

import json
import time

from application.python.types import Singleton
from twisted.internet import reactor
from twisted.internet.protocol import DatagramProtocol

from sylk.applications.conference.configuration import ConferenceConfig
from sylk.applications.conference.logger import log


__all__ = ('LevelUDPServer',)


DEFAULT_SUBSCRIPTION_TTL = 60  # seconds; subscribers refresh with heartbeat
MAX_SUBSCRIPTION_TTL = 300


def _canonical_room_uri(value):
    """Canonicalise a room URI for subscription keying.

    Subscribers may include the SIP scheme (sip:user@host) — the focus
    side calls send_levels with the bare AoR (user@host) it reads from
    Room.uri. Strip any scheme prefix, drop URI parameters, lowercase,
    so both sides converge on the same key.
    """
    if not value:
        return ''
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8')
        except Exception:
            return ''
    value = value.strip()
    # Drop ;params (e.g. sip:foo@host;transport=udp;app=…).
    if ';' in value:
        value = value.split(';', 1)[0]
    # Strip scheme (sip:, sips:, tel:, im: …).
    if ':' in value and '@' in value and value.index(':') < value.index('@'):
        value = value.split(':', 1)[1]
    return value.lower()


class _Subscription(object):
    __slots__ = ('addr', 'room_uri', 'expires_at')

    def __init__(self, addr, room_uri, expires_at):
        self.addr = addr            # (host, port)
        self.room_uri = room_uri    # 'user@host'
        self.expires_at = expires_at


class _ServerProtocol(DatagramProtocol):

    def __init__(self, server):
        self._server = server

    def datagramReceived(self, datagram, addr):
        self._server._handle_datagram(datagram, addr)


class LevelUDPServer(object, metaclass=Singleton):
    """Process-wide UDP server. One instance shared by every Room — the
    UDP listener is a global resource bound to the configured host:port,
    and the subscription table is keyed by (room_uri, addr) so a single
    socket serves the whole conference focus.
    """

    def __init__(self):
        self._protocol = None
        self._listener = None
        # (room_uri, addr) -> _Subscription
        self._subscriptions = {}

    # ----- lifecycle ------------------------------------------------

    def start(self):
        if self._listener is not None:
            return
        addr = ConferenceConfig.audio_level_udp_listen
        if not addr:
            log.info('audio-level UDP server disabled (audio_level_udp_listen unset)')
            return
        host, port = addr
        try:
            self._protocol = _ServerProtocol(self)
            # noinspection PyUnresolvedReferences
            self._listener = reactor.listenUDP(port, self._protocol, interface=host)
            log.info('audio-level UDP server listening on %s:%d' % (host, port))
        except Exception as e:
            log.error('audio-level UDP server failed to start on %s:%d: %s' % (host, port, e))
            self._listener = None
            self._protocol = None

    def stop(self):
        if self._listener is not None:
            try:
                self._listener.stopListening()
            except Exception:
                pass
            self._listener = None
        self._protocol = None
        self._subscriptions.clear()

    @property
    def endpoint(self):
        """Public host:port string for publishing in conference-info NOTIFY.

        Resolution order:

          1. ConferenceConfig.audio_level_udp_advertised_endpoint, if set
             explicitly by the operator (use this when the focus is
             behind NAT or container hostname games).
          2. The actual bound host:port — but with `0.0.0.0` / `::` /
             empty rewritten to the first private IPv4 of a non-virtual
             interface (same picker that ConferenceApplication.admin_url
             uses for http_management_interface — keeps the admin URL
             and the UDP endpoint in sync with each other).
          3. None when the server is not listening yet.
        """
        advertised = ConferenceConfig.audio_level_udp_advertised_endpoint
        if advertised:
            return advertised
        if self._listener is None:
            return None
        bound = self._listener.getHost()
        host = bound.host
        if host in ('0.0.0.0', '::', ''):
            # Lazy import to avoid a circular dependency at module load
            # time — configuration.py imports nothing from this module.
            from sylk.applications.conference.configuration import pick_default_admin_ip
            host = pick_default_admin_ip()
        return '%s:%d' % (host, bound.port)

    # ----- inbound (subscribe / unsubscribe) ------------------------

    def _expected_token(self):
        return (ConferenceConfig.audio_level_udp_token
                or ConferenceConfig.http_management_auth_secret or '')

    def _handle_datagram(self, datagram, addr):
        try:
            message = json.loads(datagram.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            log.debug('audio-level UDP server: bad payload from %s:%d' % (addr[0], addr[1]))
            return
        if not isinstance(message, dict):
            return
        kind = message.get('type')
        if kind not in ('subscribe', 'unsubscribe'):
            return
        expected = self._expected_token()
        if expected and message.get('token') != expected:
            # Spoofable transport; bad token = drop on the floor. Logged
            # at warning so a misconfigured peer surfaces in normal logs
            # — otherwise the failure is invisible to both sides.
            log.warning('audio-level UDP server: %s datagram from %s:%d rejected (bad token)' %
                        (kind, addr[0], addr[1]))
            return
        room_uri = _canonical_room_uri(message.get('room_uri'))
        if not room_uri:
            return
        if kind == 'subscribe':
            ttl = message.get('ttl', DEFAULT_SUBSCRIPTION_TTL)
            try:
                ttl = int(ttl)
            except (TypeError, ValueError):
                ttl = DEFAULT_SUBSCRIPTION_TTL
            if ttl <= 0:
                ttl = DEFAULT_SUBSCRIPTION_TTL
            if ttl > MAX_SUBSCRIPTION_TTL:
                ttl = MAX_SUBSCRIPTION_TTL
            expires_at = time.time() + ttl
            key = (room_uri, addr)
            is_new = key not in self._subscriptions
            self._subscriptions[key] = _Subscription(addr, room_uri, expires_at)
            # Log new registrations at INFO; refreshes (heartbeats) at debug
            # so the 30s cadence doesn't flood the log.
            if is_new:
                log.info('audio-level UDP server: subscriber %s:%d registered for room %s (ttl=%ds)' %
                         (addr[0], addr[1], room_uri, ttl))
            else:
                log.debug('audio-level UDP server: subscriber %s:%d refreshed room %s' %
                          (addr[0], addr[1], room_uri))
            # Ack so the subscriber sees its registration succeeded.
            self._send(addr, {
                'type': 'subscribed',
                'room_uri': room_uri,
                'ttl': ttl,
            })
        else:  # unsubscribe
            dropped = self._subscriptions.pop((room_uri, addr), None)
            if dropped is not None:
                log.info('audio-level UDP server: subscriber %s:%d unsubscribed from room %s' %
                         (addr[0], addr[1], room_uri))

    # ----- outbound (called by Room._emit_level_notification) -------

    def send_levels(self, room_uri, levels, ts=None):
        """Push a levels snapshot to every live subscriber for the given room URI.

        Best-effort: errors per subscription are swallowed; expired
        subscriptions are GC'd lazily as we iterate. Returns the count
        of datagrams sent (0 when there are no live subscribers for
        that room — the common case for rooms that no webrtcgateway
        is bridged to right now).
        """
        if self._protocol is None or self._protocol.transport is None:
            return 0
        if not self._subscriptions:
            return 0
        key_room = _canonical_room_uri(room_uri)
        if not key_room:
            return 0
        now = time.time()
        payload = {
            'type': 'audio-levels',
            'room_uri': room_uri,
            'levels': levels,
            'ts': ts if isinstance(ts, int) else int(now * 1000),
        }
        sent = 0
        expired = []
        for key, sub in self._subscriptions.items():
            if sub.room_uri != key_room:
                continue
            if sub.expires_at <= now:
                expired.append(key)
                continue
            if self._send(sub.addr, payload):
                sent += 1
        for key in expired:
            self._subscriptions.pop(key, None)
        return sent

    def _send(self, addr, payload_dict):
        try:
            datagram = json.dumps(payload_dict).encode('utf-8')
        except (TypeError, ValueError) as e:
            log.warning('audio-level UDP server: payload not serialisable: %s' % e)
            return False
        try:
            self._protocol.transport.write(datagram, addr)
            return True
        except Exception:
            return False
