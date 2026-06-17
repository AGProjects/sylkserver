#!/usr/bin/env python3
"""
audio_level_monitor.py — standalone subscriber for the conference focus's
audio-level UDP stream.

Use it to verify the UDP path end-to-end without sylkserver in the way,
or as a console VU meter for diagnosing who's talking in a conference
without having to read the focus's own log.

Wire protocol is the one implemented by
sylk.applications.conference.audio_level_udp:

  client -> server   {"type":"subscribe", "room_uri":..., "token":..., "ttl":60}
  client <- server   {"type":"subscribed", ...}                    (one ack)
  client <- server   {"type":"audio-levels", "room_uri":...,
                      "levels":{"<pid>":{"tx":...,"rx":...,
                                          "tx_peak":...,"rx_peak":...}, ...},
                      "ts":<ms-epoch>}                              (every 250ms)
  client -> server   {"type":"unsubscribe", "room_uri":..., "token":...}

Usage:

    python3 audio_level_monitor.py \\
        --endpoint 10.208.117.121:11000 \\
        --room     733591@conference.sip2sip.info \\
        --token    el2Lwc3FwRkVWYhlblUdgqoYlaJ10cUotIVlT0w9rkk

Add --raw to dump every datagram as one JSON line (good for piping to jq).
Add --quiet to suppress everything but the meter.
Press Ctrl+C to unsubscribe cleanly and exit.
"""

import argparse
import json
import os
import select
import signal
import socket
import sys
import time


HEARTBEAT_INTERVAL = 5      # seconds; pins the NAT mapping
SUBSCRIBE_TTL = 60          # seconds; what we ask the focus to remember us for
RECV_BUFSIZE = 65535        # one UDP datagram fits


# ----- terminal helpers ------------------------------------------------

def _supports_color():
    if not sys.stdout.isatty():
        return False
    if os.environ.get('NO_COLOR'):
        return False
    return os.environ.get('TERM', '') not in ('', 'dumb')


COLOR = _supports_color()


def _c(code, text):
    if not COLOR:
        return text
    return '\x1b[{}m{}\x1b[0m'.format(code, text)


def _bar(value, width=24, max_value=255):
    """Render a 0..255 level as a width-character bar."""
    if value < 0:
        value = 0
    if value > max_value:
        value = max_value
    filled = int(round(value / max_value * width))
    blocks = '█' * filled
    empty = '░' * (width - filled)
    if COLOR:
        # Green up to ~40%, yellow up to ~75%, red above.
        ratio = value / max_value
        color = 32 if ratio < 0.4 else 33 if ratio < 0.75 else 31
        return '\x1b[{}m{}\x1b[0m{}'.format(color, blocks, empty)
    return blocks + empty


# ----- subscriber ------------------------------------------------------

class LevelMonitor(object):
    def __init__(self, host, port, room_uri, token, raw=False, quiet=False):
        self.addr = (host, port)
        self.room_uri = room_uri
        self.token = token or ''
        self.raw = raw
        self.quiet = quiet
        self.sock = None
        self._stop = False
        self._first_ack = False
        self._first_levels = False
        self._datagrams_received = 0
        self._last_print_line_count = 0

    # ---- lifecycle -----

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # Bind to port 0 — kernel picks an ephemeral source port.
        self.sock.bind(('0.0.0.0', 0))
        local = self.sock.getsockname()
        if not self.quiet:
            print(_c('1;36', 'audio-level monitor:'),
                  'bound on {}:{}, subscribing to {}:{} for room {}'.format(
                      local[0], local[1], self.addr[0], self.addr[1], self.room_uri))

    def close(self):
        if self.sock is None:
            return
        try:
            self._send({'type': 'unsubscribe',
                        'room_uri': self.room_uri,
                        'token': self.token})
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    # ---- protocol -----

    def _send(self, payload):
        if self.sock is None:
            return False
        datagram = json.dumps(payload).encode('utf-8')
        try:
            self.sock.sendto(datagram, self.addr)
            return True
        except OSError as e:
            print(_c('31', 'send failed: {}'.format(e)), file=sys.stderr)
            return False

    def _subscribe(self):
        return self._send({
            'type': 'subscribe',
            'room_uri': self.room_uri,
            'token': self.token,
            'ttl': SUBSCRIBE_TTL,
        })

    # ---- main loop -----

    def stop(self):
        self._stop = True

    def run(self):
        self.open()
        if not self._subscribe():
            return 1
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
        last_recv = time.monotonic()
        try:
            while not self._stop:
                # Wake at most every 500 ms so we can fire heartbeats
                # and notice no-data conditions even when the focus
                # has gone quiet.
                timeout = max(0.0, next_heartbeat - time.monotonic())
                if timeout > 0.5:
                    timeout = 0.5
                r, _, _ = select.select([self.sock], [], [], timeout)
                now = time.monotonic()
                if r:
                    try:
                        datagram, addr = self.sock.recvfrom(RECV_BUFSIZE)
                    except OSError:
                        continue
                    last_recv = now
                    self._handle_datagram(datagram, addr)
                if now >= next_heartbeat:
                    self._subscribe()
                    next_heartbeat = now + HEARTBEAT_INTERVAL
                # No-data warning: if the conference is supposed to be
                # pushing levels but we haven't seen anything for ~3
                # heartbeats, the operator should know — this is the
                # NAT / firewall failure mode.
                if (not self.raw and not self.quiet
                        and self._datagrams_received == 0
                        and now - last_recv > 3 * HEARTBEAT_INTERVAL):
                    self._render_status('no data yet — check NAT / firewall / token')
                    last_recv = now  # rate-limit the warning to once per window
        finally:
            self.close()
        return 0

    # ---- inbound parsing -----

    def _handle_datagram(self, datagram, addr):
        try:
            message = json.loads(datagram.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            print(_c('31', 'bad payload from {}:{}'.format(addr[0], addr[1])),
                  file=sys.stderr)
            return
        if not isinstance(message, dict):
            return
        if self.raw:
            sys.stdout.write(json.dumps(message) + '\n')
            sys.stdout.flush()
            return
        kind = message.get('type')
        if kind == 'subscribed':
            if not self._first_ack and not self.quiet:
                self._first_ack = True
                print(_c('32', 'subscription confirmed by {}:{} ttl={}s'.format(
                    addr[0], addr[1], message.get('ttl'))))
        elif kind == 'audio-levels':
            self._datagrams_received += 1
            if not self._first_levels and not self.quiet:
                self._first_levels = True
                print(_c('32', 'first audio-levels datagram received '
                              '({} entries)'.format(len(message.get('levels') or {}))))
            self._render(message)

    # ---- rendering -----

    def _render_status(self, msg):
        line = _c('33', msg)
        self._erase_previous()
        sys.stdout.write(line + '\n')
        sys.stdout.flush()
        self._last_print_line_count = 1

    def _erase_previous(self):
        # Move cursor up and clear, so the meter updates in place
        # instead of scrolling. Skipped when not interactive.
        if not COLOR or self._last_print_line_count == 0:
            return
        sys.stdout.write('\x1b[{}A'.format(self._last_print_line_count))
        sys.stdout.write('\x1b[J')

    def _render(self, message):
        levels = message.get('levels') or {}
        ts = message.get('ts')
        lines = []
        header = 'room={}  ts={}  n={}  ({} datagrams received)'.format(
            message.get('room_uri', '?'),
            ts,
            len(levels),
            self._datagrams_received,
        )
        lines.append(_c('1', header))
        # Sort by participant_id for stable layout — otherwise dict
        # iteration order makes the meter jump around.
        for pid in sorted(levels.keys()):
            entry = levels[pid] or {}
            tx = int(entry.get('tx') or 0)
            rx = int(entry.get('rx') or 0)
            tx_peak = int(entry.get('tx_peak') or 0)
            rx_peak = int(entry.get('rx_peak') or 0)
            lines.append('  {pid:>14}  rx {rx_bar} {rx:3d}/{rxp:3d}   '
                         'tx {tx_bar} {tx:3d}/{txp:3d}'.format(
                             pid=pid,
                             rx_bar=_bar(rx_peak),
                             rx=rx, rxp=rx_peak,
                             tx_bar=_bar(tx_peak),
                             tx=tx, txp=tx_peak))
        self._erase_previous()
        sys.stdout.write('\n'.join(lines) + '\n')
        sys.stdout.flush()
        self._last_print_line_count = len(lines)


# ----- CLI -------------------------------------------------------------

def parse_endpoint(value):
    if ':' not in value:
        raise argparse.ArgumentTypeError('endpoint must be host:port')
    host, _, port = value.rpartition(':')
    host = host.strip()
    try:
        port = int(port.strip())
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer')
    if not host:
        raise argparse.ArgumentTypeError('host is empty')
    if not (0 < port < 65536):
        raise argparse.ArgumentTypeError('port out of range')
    return host, port


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument('--endpoint', required=True, type=parse_endpoint,
                   help='conference UDP server endpoint, e.g. 10.208.117.121:11000 '
                        '(value of agp-conf:audio_levels_udp_endpoint in the NOTIFY)')
    p.add_argument('--room', required=True,
                   help='conference room URI, e.g. 733591@conference.sip2sip.info')
    p.add_argument('--token', required=True,
                   help='shared secret (the per-room admin_endpoint_token from the '
                        'conference-info NOTIFY, or the focus\'s audio_level_udp_token)')
    p.add_argument('--raw', action='store_true',
                   help='print every datagram as a JSON line — good for | jq .')
    p.add_argument('--quiet', action='store_true',
                   help='suppress chatter; only render the meter')
    args = p.parse_args(argv)

    host, port = args.endpoint
    mon = LevelMonitor(host, port, args.room, args.token,
                       raw=args.raw, quiet=args.quiet)

    def _on_signal(sig, frame):
        mon.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        return mon.run()
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
