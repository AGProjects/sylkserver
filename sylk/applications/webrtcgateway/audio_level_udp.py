
"""
UDP client for real-time audio-level updates from a remote conference
focus.

Sister of sylk.applications.conference.audio_level_udp — that module
runs the server. This one is the subscriber:

  1. The webrtcgateway watches conference-info NOTIFYs as they arrive
     on the chat sessions it bridges. Each NOTIFY may carry an
     `agp-conf:audio_levels_udp_endpoint` on the audio-bridge
     participant's User element.
  2. When the gateway sees that endpoint (and the matching admin
     token) for a room, it asks AudioLevelUDPClient to subscribe.
  3. The client opens its single shared UDP socket (or reuses it),
     sends a `subscribe` datagram, and starts a heartbeat that
     re-subscribes every audio_level_udp_heartbeat seconds so the
     focus's TTL never expires.
  4. Incoming `audio-levels` datagrams are parsed, looked up against
     the local SylkWebSocketServerFactory.videorooms table by the
     `conference` → `videoconference` URI substitution, and pushed
     as VideoroomConferenceAudioLevelsEvent over every WebSocket
     attached to that videoroom.

One UDP socket serves every room and every gateway-host. Subscriptions
are keyed by (endpoint, room_uri) so two rooms hosted on the same focus
share a single conversation; subscriptions to different focuses each
have their own.
"""

import json
import time

from application.python.types import Singleton
from twisted.internet import reactor
from twisted.internet.protocol import DatagramProtocol
from twisted.internet.task import LoopingCall

from sylk.applications.webrtcgateway.configuration import GeneralConfig
from sylk.applications.webrtcgateway.factory import SylkWebSocketServerFactory
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.models import sylkrtc


__all__ = ('AudioLevelUDPClient',)


HEARTBEAT_INTERVAL = 5   # seconds; re-subscribe at this cadence
SUBSCRIBE_TTL = 60       # seconds; sent to the server in each subscribe
# NOTE on HEARTBEAT_INTERVAL: this also serves as the UDP NAT keepalive
# in deployments where the gateway sits behind a NAT (cloud egress
# gateway, site-to-site VPN, k8s SNAT). Many UDP-tracking NATs use a
# 30s idle timer; setting the heartbeat to 5s keeps the mapping
# pinned with margin to spare. Cost is 1 small datagram every 5s
# per gateway × room — negligible.


def _parse_endpoint(endpoint):
    """Parse 'host:port' string into (host, port) tuple. Returns None
    on malformed input — callers must guard against missing endpoints.
    """
    if not endpoint:
        return None
    if isinstance(endpoint, bytes):
        try:
            endpoint = endpoint.decode('utf-8')
        except Exception:
            return None
    if ':' not in endpoint:
        return None
    host, _, port_str = endpoint.rpartition(':')
    host = host.strip()
    try:
        port = int(port_str.strip())
    except ValueError:
        return None
    if not host or not (0 < port < 65536):
        return None
    return (host, port)


def _videoroom_uri_for(conference_uri):
    """Map a SIP conference room URI (e.g. 'X@conference.host') back to
    the webrtcgateway videoroom URI ('X@videoconference.host'). Inverse
    of the substitution VideoroomChatHandler.start does on the way out.
    """
    if not conference_uri:
        return conference_uri
    user, _, host = conference_uri.partition('@')
    if not host:
        return conference_uri
    if 'conference' in host and 'videoconference' not in host:
        host = host.replace('conference', 'videoconference', 1)
    return '%s@%s' % (user, host)


class _Subscription(object):
    """One active subscription against a remote focus."""

    __slots__ = ('endpoint', 'addr', 'room_uri', 'token', 'last_sent')

    def __init__(self, endpoint, addr, room_uri, token):
        self.endpoint = endpoint       # 'host:port' string from NOTIFY
        self.addr = addr               # (host, port) tuple
        self.room_uri = room_uri       # 'user@host' (SIP conference side)
        self.token = token             # bearer token from NOTIFY
        self.last_sent = 0.0           # unix ts of most recent subscribe

    def key(self):
        return (self.endpoint, self.room_uri.lower())


class _ClientProtocol(DatagramProtocol):

    def __init__(self, client):
        self._client = client

    def datagramReceived(self, datagram, addr):
        self._client._handle_inbound(datagram, addr)


class AudioLevelUDPClient(object, metaclass=Singleton):

    def __init__(self):
        self._protocol = None
        self._listener = None
        # key = (endpoint, room_uri.lower())
        self._subscriptions = {}
        self._heartbeat = None
        # Rolling accumulators for the periodic per-room log line.
        # Mirrors the conference focus's audio_level_log_period output
        # so the gateway can independently confirm what it's receiving.
        # Outer dict: room_uri (lower-cased) → inner dict; inner dict:
        # participant_id → {tx_sum, rx_sum, tx_peak, rx_peak, count}.
        self._log_accumulator = {}
        self._level_logger = None
        # Latest per-room audio-level snapshot, for pull-based consumers
        # such as the admin web UI (which polls instead of holding a
        # WebSocket). Keyed by videoroom_uri (lower-cased, the same key
        # the admin handler uses); value is
        #   {'ts': <focus ts or None>, 'received': <unix seconds>,
        #    'levels': {pid: {'tx','rx','tx_peak','rx_peak'}}}.
        # Replaced wholesale on every datagram so stale pids drop out.
        self.latest_levels = {}

    # ----- lifecycle ------------------------------------------------

    def start(self):
        if self._listener is not None:
            return
        try:
            self._protocol = _ClientProtocol(self)
            # Bind to port 0 — kernel-assigned ephemeral source port.
            # The remote focus echoes back to whatever (src_addr, src_port)
            # our subscribe datagram came from, so we don't need a
            # well-known port here.
            # noinspection PyUnresolvedReferences
            self._listener = reactor.listenUDP(0, self._protocol)
        except Exception as e:
            log.error('audio-level UDP client failed to bind: %s' % e)
            self._listener = None
            self._protocol = None
            return
        self._heartbeat = LoopingCall(self._send_heartbeats)
        self._heartbeat.start(HEARTBEAT_INTERVAL, now=False)
        log.info('audio-level UDP client started (ephemeral source port)')
        # Periodic per-room summary log — mirrors the conference focus's
        # own audio_level_log_period output so the gateway can confirm
        # independently what it's receiving over the wire. Driven from
        # the gateway-side config so the two cadences are independent.
        log_period = float(getattr(GeneralConfig, 'audio_level_log_period', 0) or 0)
        if log_period > 0:
            self._level_logger = LoopingCall(self._log_audio_levels)
            self._level_logger.start(log_period, now=False)

    def stop(self):
        # Send a clean unsubscribe to every focus we registered with.
        for sub in list(self._subscriptions.values()):
            try:
                self._send_datagram(sub.addr, {
                    'type': 'unsubscribe',
                    'room_uri': sub.room_uri,
                    'token': sub.token,
                })
            except Exception:
                pass
        self._subscriptions.clear()
        if self._heartbeat is not None:
            if self._heartbeat.running:
                try:
                    self._heartbeat.stop()
                except Exception:
                    pass
            self._heartbeat = None
        if self._level_logger is not None:
            if self._level_logger.running:
                try:
                    self._level_logger.stop()
                except Exception:
                    pass
            self._level_logger = None
        self._log_accumulator.clear()
        if self._listener is not None:
            try:
                self._listener.stopListening()
            except Exception:
                pass
            self._listener = None
        self._protocol = None

    # ----- subscription management ----------------------------------

    def ensure_subscription(self, endpoint, room_uri, token):
        """Add or refresh a subscription for (endpoint, room_uri).

        Called by VideoroomChatHandler whenever it sees a conference-info
        NOTIFY carrying an audio_levels_udp_endpoint. Idempotent: a
        repeat call with the same parameters just bumps the timestamp
        so we know the gateway still cares about this room. Returns
        True when a new subscribe datagram was sent, False when nothing
        changed.
        """
        if not endpoint or not room_uri:
            return False
        addr = _parse_endpoint(endpoint)
        if addr is None:
            log.warning('audio-level UDP client: bad endpoint %r' % endpoint)
            return False
        # Auto-start on first use so we don't fight with WebRTCGatewayApplication
        # lifecycle ordering.
        if self._listener is None:
            self.start()
            if self._listener is None:
                return False
        key = (endpoint, room_uri.lower())
        existing = self._subscriptions.get(key)
        if existing is None:
            sub = _Subscription(endpoint, addr, room_uri, token or '')
            self._subscriptions[key] = sub
            log.info('audio-level UDP: subscribing to %s for room %s' %
                     (endpoint, room_uri))
        else:
            sub = existing
            # Update token in case it was rotated between NOTIFYs.
            sub.token = token or sub.token
        if self._send_subscribe(sub):
            sub.last_sent = time.time()
            if existing is None:
                log.info('audio-level UDP: subscribe datagram sent to %s:%d for room %s' %
                         (addr[0], addr[1], room_uri))
            return True
        # send_datagram already logs the failure at debug — promote to
        # warning here so a misconfigured peer surfaces in normal logs.
        log.warning('audio-level UDP: failed to send subscribe to %s for room %s' %
                    (endpoint, room_uri))
        return False

    def drop_subscription(self, endpoint, room_uri):
        if not endpoint or not room_uri:
            return
        key = (endpoint, room_uri.lower())
        sub = self._subscriptions.pop(key, None)
        if sub is None:
            return
        try:
            self._send_datagram(sub.addr, {
                'type': 'unsubscribe',
                'room_uri': sub.room_uri,
                'token': sub.token,
            })
        except Exception:
            pass

    # ----- network I/O ----------------------------------------------

    def _send_datagram(self, addr, payload_dict):
        if self._protocol is None or self._protocol.transport is None:
            return False
        try:
            datagram = json.dumps(payload_dict).encode('utf-8')
            self._protocol.transport.write(datagram, addr)
            return True
        except Exception as e:
            log.debug('audio-level UDP client: send failed to %s: %s' % (addr, e))
            return False

    def _send_subscribe(self, sub):
        return self._send_datagram(sub.addr, {
            'type': 'subscribe',
            'room_uri': sub.room_uri,
            'token': sub.token,
            'ttl': SUBSCRIBE_TTL,
        })

    def _send_heartbeats(self):
        # Re-subscribe across the board. The server uses the heartbeat
        # to roll the TTL forward, so a missed beat for HEARTBEAT_INTERVAL
        # + SUBSCRIBE_TTL is enough to drop us cleanly.
        now = time.time()
        for sub in list(self._subscriptions.values()):
            if self._send_subscribe(sub):
                sub.last_sent = now

    # ----- inbound dispatch -----------------------------------------

    def _handle_inbound(self, datagram, addr):
        try:
            message = json.loads(datagram.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get('type')
        if kind == 'subscribed':
            # Ack from the focus — confirms the focus accepted our
            # subscribe datagram. Logged once at INFO so the operator
            # can see the handshake completed; subsequent acks (from
            # heartbeat re-subscribes) are demoted to debug to avoid
            # log spam at the 30s cadence.
            room_uri = message.get('room_uri')
            key = (None, (room_uri or '').lower())
            if room_uri and not getattr(self, '_acked_once', set()).__contains__(room_uri):
                if not hasattr(self, '_acked_once'):
                    self._acked_once = set()
                self._acked_once.add(room_uri)
                log.info('audio-level UDP: subscription confirmed by %s:%d for room %s (ttl=%s)' %
                         (addr[0], addr[1], room_uri, message.get('ttl')))
            else:
                log.debug('audio-level UDP: re-ack from %s for %s' %
                          (addr, room_uri))
            return
        if kind != 'audio-levels':
            return
        room_uri = message.get('room_uri') or ''
        levels = message.get('levels') or {}
        if not room_uri or not isinstance(levels, dict):
            return
        ts = message.get('ts')
        if not isinstance(ts, int):
            ts = None
        self._dispatch_levels(room_uri, levels, ts)

    def _dispatch_levels(self, room_uri, levels, ts):
        videoroom_uri = _videoroom_uri_for(room_uri).lower()
        # SylkWebSocketServerFactory.videorooms is a VideoroomContainer,
        # not a dict — it supports `key in container` / `container[key]`
        # but not `.get()`. Using __getitem__ here would raise KeyError
        # when the room isn't bridged to this gateway; that's the
        # normal case for a focus serving rooms across multiple
        # gateways, so swallow the lookup miss silently.
        try:
            videoroom = SylkWebSocketServerFactory.videorooms[videoroom_uri]
        except KeyError:
            return
        # Log the first audio-levels datagram per room at INFO so the
        # operator can confirm the return path from the conference is
        # actually arriving — otherwise it's invisible whether silence
        # means "no traffic dropped" or "no traffic at all".
        if not hasattr(self, '_seen_levels_for'):
            self._seen_levels_for = set()
        if room_uri not in self._seen_levels_for:
            self._seen_levels_for.add(room_uri)
            log.info('audio-level UDP: first audio-levels datagram received for room %s '
                     '(%d participant entries)' % (room_uri, len(levels) if levels else 0))
        # Feed the per-room accumulator regardless of whether anyone
        # is currently subscribed on a WebSocket. We log mean+peak per
        # participant on the LoopingCall tick (default every 5s).
        room_key = (room_uri or '').lower()
        room_acc = self._log_accumulator.setdefault(room_key, {})
        payload_levels = []
        snapshot = {}
        for pid, v in levels.items():
            if not isinstance(v, dict):
                continue
            try:
                tx = int(v.get('tx') or 0)
                rx = int(v.get('rx') or 0)
                tx_peak = int(v.get('tx_peak') or 0)
                rx_peak = int(v.get('rx_peak') or 0)
            except (TypeError, ValueError):
                continue
            snapshot[str(pid)] = {'tx': tx, 'rx': rx, 'tx_peak': tx_peak, 'rx_peak': rx_peak}
            try:
                payload_levels.append(sylkrtc.VideoroomConferenceAudioLevel(
                    participant_id=str(pid),
                    tx=tx,
                    rx=rx,
                    tx_peak=tx_peak,
                    rx_peak=rx_peak,
                ))
            except Exception:
                continue
            # Accumulate. Note: the values arriving here are themselves
            # the conference's per-250ms mean+peak rollups, so what we
            # average is "mean of means" and what we max is "max of
            # peaks" — both are still meaningful at the 5s scale.
            entry = room_acc.get(pid)
            if entry is None:
                entry = {'tx_sum': 0, 'rx_sum': 0, 'tx_peak': 0, 'rx_peak': 0, 'count': 0}
                room_acc[pid] = entry
            entry['tx_sum'] += tx
            entry['rx_sum'] += rx
            if tx_peak > entry['tx_peak']:
                entry['tx_peak'] = tx_peak
            if rx_peak > entry['rx_peak']:
                entry['rx_peak'] = rx_peak
            entry['count'] += 1
        # Publish the latest snapshot for pull-based consumers (admin UI).
        self.latest_levels[videoroom_uri] = {
            'ts': ts,
            'received': time.time(),
            'levels': snapshot,
        }
        try:
            sessions = list(videoroom)
        except Exception:
            return
        for session in sessions:
            owner = getattr(session, 'owner', None)
            if owner is None:
                continue
            try:
                owner.send(sylkrtc.VideoroomConferenceAudioLevelsEvent(
                    session=session.id,
                    levels=payload_levels,
                    ts=ts,
                ))
            except Exception as e:
                log.debug('audio-level UDP client: send failed for %s: %s' %
                          (session.id, e))

    def _log_audio_levels(self):
        """Emit one summary log line per active room — mirrors the
        conference focus's audio_level_log_period output so the gateway
        side has an independent record of what it's actually receiving.

        Flushes (resets) the accumulator on each call. Rooms with no
        samples in the window produce no log output (silence == silence;
        nothing to report). The room_uri logged here is the SIP-side
        conference URI as it arrived in the UDP datagram — translating
        back to the videoroom URI for display would just reverse what
        the gateway just did to look the room up.
        """
        if not self._log_accumulator:
            return
        accumulator, self._log_accumulator = self._log_accumulator, {}
        log_period = float(getattr(GeneralConfig, 'audio_level_log_period', 0) or 0)
        period_str = ('%g' % log_period).rstrip('.') or '?'
        # One line per participant per room — short, greppable, and the
        # same shape the conference focus and the audio-bridge emit.
        # Format:
        #   Room <local> audio level: "<who>" "<pid>" <N>s mean/peak, n=<samples>, tx=A/B rx=C/D
        for room_uri, room_acc in accumulator.items():
            # Resolve participant_id → short "who" label via the matching
            # Videoroom's cached label map (populated in handler.py from
            # the conference-info NOTIFY). Falls back to "?" when the
            # label isn't (yet) known so the column count stays stable.
            label_map = {}
            bridge_pid = None
            videoroom = None
            try:
                videoroom = SylkWebSocketServerFactory.videorooms[
                    _videoroom_uri_for(room_uri).lower()
                ]
                label_map = getattr(videoroom, 'participant_labels', {}) or {}
                bridge_pid = getattr(videoroom, 'bridge_participant_id', None)
            except KeyError:
                pass
            # Prefer the videoroom's own contextual logger so the line
            # carries the standard "[videoroom <uri>]" prefix every other
            # videoroom log line uses. Fall back to the module logger
            # when the videoroom isn't on this gateway (lookup miss).
            room_log = videoroom.log if videoroom is not None and hasattr(videoroom, 'log') else log
            for pid, entry in room_acc.items():
                if bridge_pid and pid == bridge_pid:
                    continue  # audio-bridge plumbing — not user-facing
                count = entry['count']
                if count <= 0:
                    continue
                avg_tx = entry['tx_sum'] // count
                avg_rx = entry['rx_sum'] // count
                who = label_map.get(pid) or '?'
                # Demoted to .debug — was useful while bringing the
                # audio-level plumbing online but at steady state it
                # produces one INFO line per participant per period
                # for every room. Re-enable by flipping to .info when
                # troubleshooting the audio-bridge → gateway pipeline.
                room_log.debug(
                    'audio level: "%-10.10s" "%s" %ss mean/peak, n=%d, tx=%d/%d rx=%d/%d' %
                    (who, pid, period_str, count,
                     avg_tx, entry['tx_peak'], avg_rx, entry['rx_peak'])
                )
