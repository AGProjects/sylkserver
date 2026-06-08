#!/usr/bin/env python3
"""
sylk-qos-server.py — long-running QoS capture daemon for the WebRTC/Janus host.

Replaces the old SSH-launched, per-call ``qos-server.py``. Instead of opening
an SSH session per call, callers (the dev-host ``qos-probe.py`` pipeline, the
Sylk Mobile client, or SylkServer) talk to this daemon over a small HTTP API:
they REGISTER a call (which starts a capture) and later FETCH everything the
daemon learned about that call, keyed by SIP Call-ID.

Configuration is read the SylkServer way, from <config-dir> (default
/etc/sylkserver, override with --config-dir):
  - config.ini      [Server] trace_dir   -> the trace prefix
  - qos-server.ini  this daemon's own settings (see qos-server.ini.sample)

Everything the daemon learns about a call is persisted under

    <trace_dir>/qos/<call-id>/

so a call can be fully reconstructed after the fact:

    meta.json          registration params, tuples, interface, host, timestamps
    capture.pcap       raw packet capture of the call's 5-tuples (Wireshark)
    samples.ndjson     per-interval cumulative counters timeline (one JSON/line)
    kernel_before.json NIC / UDP / conntrack counters at capture start
    kernel_after.json  ... and at capture end
    summary.json       totals, nic_loss, RTP-leg counts, media-plane verdict
    events.log         human-readable [sylk-qos-server] log lines

HTTP API (all JSON unless noted; auth via Bearer token — see --token):

    GET  /health                      -> {status, version, active, total}
    POST /calls                       -> register + start capture
         body: {call_id, client_ip, client_port, server_port,
                server_ip?, mediaproxy_ip?, rtp_port_min?, rtp_port_max?,
                expected_pps?}
    POST /calls/{call_id}/stop        -> finalize capture
    GET  /calls                       -> [{call_id, status, ...}, ...]
    GET  /calls/{call_id}             -> manifest {meta, summary, artifacts[]}
    GET  /calls/{call_id}/bundle      -> application/gzip tar of the whole dir
    GET  /calls/{call_id}/{artifact}  -> raw file (pcap / samples.ndjson / ...)

Design notes:
  - stdlib only (http.server, subprocess, threading, tarfile) so it runs on a
    bare server with just Python 3 + tcpdump.
  - Capture is best-effort: if tcpdump is missing or unprivileged, the call is
    still registered and the folder/metadata are written, with the failure
    recorded in meta.json (capture_error). The API stays fully functional.
  - Run under systemd (Type=simple) with CAP_NET_RAW — see sylk-qos-server.service.
"""
import argparse
import configparser
import datetime
import gzip
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

VERSION = '1.0'

# SylkServer's default configuration directory (overridable with --config-dir).
DEFAULT_CONFIG_DIR = '/etc/sylkserver'


def load_config(config_dir):
    """Read configuration the SylkServer way.

    Two files in <config_dir> (default /etc/sylkserver), same INI syntax as the
    rest of SylkServer:
      - config.ini      -> [Server] trace_dir  (the log/trace prefix; we write
                           per-call folders under <trace_dir>/qos/)
      - qos-server.ini  -> this daemon's own settings

    Returns a dict of effective settings. Missing files / keys fall back to the
    documented defaults, so a bare host still starts."""
    def parser():
        # interpolation=None: don't choke on '%' in values; strict=False: tolerate
        # the occasional duplicate the hand-edited sylkserver inis may contain.
        return configparser.ConfigParser(interpolation=None, strict=False)

    main_ini = os.path.join(config_dir, 'config.ini')
    qos_ini = os.path.join(config_dir, 'qos-server.ini')

    main = parser()
    try:
        main.read(main_ini)
    except configparser.Error as e:
        raise SystemExit('error reading {}: {}'.format(main_ini, e))
    trace_dir = main.get('Server', 'trace_dir', fallback='/var/log/sylkserver').strip()

    qos = parser()
    try:
        qos.read(qos_ini)
    except configparser.Error as e:
        raise SystemExit('error reading {}: {}'.format(qos_ini, e))

    def get(section, key, fallback):
        return qos.get(section, key, fallback=fallback).strip() if qos.has_option(section, key) else fallback

    def getint(section, key, fallback):
        try:
            return qos.getint(section, key) if qos.has_option(section, key) else fallback
        except ValueError:
            return fallback

    return {
        'config_dir': config_dir,
        'config_files': [p for p in (main_ini, qos_ini) if os.path.isfile(p)],
        'trace_dir': trace_dir,
        # Per-call artifacts go under <trace_dir>/qos/<call-id>/
        'log_dir': os.path.join(trace_dir, 'qos'),
        'listen': get('Server', 'listen', '0.0.0.0:9810'),
        'auth_token': get('Server', 'auth_token', '') or None,
        'interface': get('Server', 'interface', '') or None,
        'expected_pps': getint('Server', 'expected_pps', 50),
        'sample_interval': max(1, getint('Server', 'sample_interval', 5)),
        'max_capture_seconds': max(10, getint('Server', 'max_capture_seconds', 7200)),
        'retention_days': getint('Server', 'retention_days', 7),
        'mediaproxy_ip': get('MediaProxy', 'ip', '') or None,
        'rtp_port_min': getint('MediaProxy', 'rtp_port_min', 0) or None,
        'rtp_port_max': getint('MediaProxy', 'rtp_port_max', 0) or None,
    }

# ---------------------------------------------------------------------------
# Low-level host counters (ported from qos-server.py)
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.datetime.now().astimezone().isoformat()


def read_proc_net_dev(iface):
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                line = line.strip()
                head, _, rest = line.partition(':')
                if head.strip() != iface:
                    continue
                parts = rest.split()
                if len(parts) < 16:
                    return None
                return {
                    'rx_bytes': int(parts[0]), 'rx_packets': int(parts[1]),
                    'rx_errs': int(parts[2]), 'rx_drop': int(parts[3]),
                    'rx_fifo': int(parts[4]), 'rx_frame': int(parts[5]),
                    'tx_bytes': int(parts[8]), 'tx_packets': int(parts[9]),
                    'tx_errs': int(parts[10]), 'tx_drop': int(parts[11]),
                }
    except Exception:
        pass
    return None


def read_udp_counters():
    want = {'InDatagrams', 'NoPorts', 'InErrors', 'OutDatagrams',
            'RcvbufErrors', 'SndbufErrors', 'InCsumErrors'}
    try:
        with open('/proc/net/snmp') as f:
            headers = None
            for line in f:
                if not line.startswith('Udp:'):
                    continue
                if headers is None:
                    headers = line.strip().split()[1:]
                    continue
                values = line.strip().split()[1:]
                return {h: int(v) for h, v in zip(headers, values) if h in want}
    except Exception:
        pass
    return {}


def read_conntrack_count():
    try:
        with open('/proc/sys/net/netfilter/nf_conntrack_count') as f:
            return int(f.read().strip())
    except Exception:
        return None


def primary_interface():
    try:
        out = subprocess.check_output(['ip', '-o', '-4', 'route', 'show', 'default'], text=True)
        toks = out.split()
        for i, t in enumerate(toks):
            if t == 'dev' and i + 1 < len(toks):
                return toks[i + 1]
    except Exception:
        pass
    return 'eth0'


def have_passwordless_sudo():
    try:
        return subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def tcpdump_available():
    return shutil.which('tcpdump') is not None


_SAFE_RE = re.compile(r'[^A-Za-z0-9._@+-]')


def safe_call_id(call_id):
    """Map an arbitrary SIP Call-ID to a stable, filesystem-safe directory name.

    Deterministic: the same Call-ID always yields the same folder, so a later
    GET by the original Call-ID re-derives the path."""
    s = _SAFE_RE.sub('_', call_id or 'unknown')
    return s[:160] or 'unknown'


# ---------------------------------------------------------------------------
# Per-call capture
# ---------------------------------------------------------------------------

class CallCapture(object):
    """Owns one call's capture: tcpdump (pcap + live counters) and the on-disk
    artifact folder. Thread-safe for status reads; finalize() is idempotent."""

    def __init__(self, params, call_dir, defaults):
        self.params = params              # validated registration dict
        self.dir = Path(call_dir)
        self.defaults = defaults
        self.call_id = params['call_id']
        self.iface = params.get('interface') or defaults['interface']
        self.expected_pps = int(params.get('expected_pps') or defaults['expected_pps'])
        self.mediaproxy_ip = params.get('mediaproxy_ip') or defaults.get('mediaproxy_ip')
        self.rtp_port_min = params.get('rtp_port_min') or defaults.get('rtp_port_min')
        self.rtp_port_max = params.get('rtp_port_max') or defaults.get('rtp_port_max')

        self.status = 'registered'
        self.started_at = None
        self.ended_at = None
        self.capture_error = None

        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._pcap_proc = None
        self._text_proc = None
        self._threads = []
        self._events_fh = None

        self.in_count = 0      # phone -> server (WebRTC leg)
        self.out_count = 0     # server -> phone
        self.rtp_out = 0       # Janus -> MediaProxy
        self.rtp_in = 0        # MediaProxy -> Janus

    # -- helpers ----------------------------------------------------------
    def _event(self, msg):
        line = '{} [sylk-qos-server] {}'.format(now_iso(), msg)
        try:
            if self._events_fh:
                self._events_fh.write(line + '\n')
                self._events_fh.flush()
        except Exception:
            pass

    def _build_bpf(self):
        p = self.params
        bpf = ('(src host {cip} and src port {cp} and dst port {sp}) or '
               '(dst host {cip} and dst port {cp} and src port {sp})').format(
            cip=p['client_ip'], cp=p['client_port'], sp=p['server_port'])
        if self.mediaproxy_ip:
            if self.rtp_port_min and self.rtp_port_max:
                bpf = '({}) or (host {} and udp portrange {}-{})'.format(
                    bpf, self.mediaproxy_ip, self.rtp_port_min, self.rtp_port_max)
            else:
                bpf = '({}) or (host {} and udp)'.format(bpf, self.mediaproxy_ip)
        return bpf

    def _sudo(self, cmd):
        if os.geteuid() != 0 and have_passwordless_sudo():
            return ['sudo', '-n'] + cmd
        return cmd

    # -- lifecycle --------------------------------------------------------
    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.status = 'capturing'
        self._events_fh = open(self.dir / 'events.log', 'a')
        bpf = self._build_bpf()

        meta = {
            'call_id': self.call_id,
            'dir': str(self.dir),
            'host': os.uname().nodename,
            'interface': self.iface,
            'bpf': bpf,
            'expected_pps': self.expected_pps,
            'mediaproxy_ip': self.mediaproxy_ip,
            'rtp_port_min': self.rtp_port_min,
            'rtp_port_max': self.rtp_port_max,
            'params': self.params,
            'started_at': now_iso(),
            'daemon_version': VERSION,
        }
        self._write_json('meta.json', meta)
        self._write_json('kernel_before.json', {
            'net_dev': read_proc_net_dev(self.iface),
            'udp': read_udp_counters(),
            'conntrack': read_conntrack_count(),
            'at': now_iso(),
        })
        self._event('start call_id={} iface={} bpf={!r}'.format(self.call_id, self.iface, bpf))

        if not tcpdump_available():
            self.capture_error = 'tcpdump not found in PATH'
            self._event('ERROR ' + self.capture_error)
        else:
            self._spawn_captures(bpf)

        # Watchdog: hard cap so a call that never gets a stop can't capture forever.
        self._threads.append(self._spawn(self._watchdog))
        return meta

    def _spawn(self, target):
        t = threading.Thread(target=target, daemon=True)
        t.start()
        return t

    def _note_error(self, err):
        self.capture_error = (self.capture_error + '; ' + err) if self.capture_error else err
        self._event('ERROR ' + err)

    def _spawn_captures(self, bpf):
        pcap_path = str(self.dir / 'capture.pcap')
        pcap_err = str(self.dir / 'capture.pcap.err')
        pcap_cmd = self._sudo(['tcpdump', '-i', self.iface, '-n', '-U', '-w', pcap_path, bpf])
        text_cmd = self._sudo(['tcpdump', '-i', self.iface, '-n', '-l', '-q', bpf])
        try:
            self._pcap_proc = subprocess.Popen(pcap_cmd, stdout=subprocess.DEVNULL,
                                               stderr=open(pcap_err, 'w'))
        except Exception as e:
            self._note_error('pcap tcpdump spawn failed: {}'.format(e))
        try:
            self._text_proc = subprocess.Popen(text_cmd, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self._threads.append(self._spawn(self._packet_reader))
            self._threads.append(self._spawn(self._sample_reporter))
        except Exception as e:
            self._note_error('text tcpdump spawn failed: {}'.format(e))

        # tcpdump exits within ~100ms on a permission/BPF error. Catch that so
        # a missing capture.pcap is reported, not silently absent.
        def _check():
            time.sleep(0.4)
            if self._pcap_proc is not None and self._pcap_proc.poll() not in (None, 0):
                detail = ''
                try:
                    detail = Path(pcap_err).read_text().strip().splitlines()[-1]
                except Exception:
                    pass
                self._note_error('pcap capture failed (rc={}) {}'.format(self._pcap_proc.returncode, detail))
            if self._text_proc is not None and self._text_proc.poll() not in (None, 0):
                self._note_error('live-counter capture failed (rc={}) — '
                                 'needs root or CAP_NET_RAW'.format(self._text_proc.returncode))
        self._spawn(_check)

    def _packet_reader(self):
        client_src_tag = '{}.{}'.format(self.params['client_ip'], self.params['client_port'])
        mp_ip = self.mediaproxy_ip
        for line in self._text_proc.stdout:
            if self._shutdown.is_set():
                break
            if '{} >'.format(client_src_tag) in line:
                self.in_count += 1
            elif '> {}'.format(client_src_tag) in line:
                self.out_count += 1
            if mp_ip and ' IP ' in line and ' > ' in line:
                try:
                    seg = line.split(' IP ', 1)[1]
                    src, rest = seg.split(' > ', 1)
                    dst = rest.split(':', 1)[0]
                    if dst.strip().rsplit('.', 1)[0] == mp_ip:
                        self.rtp_out += 1
                    elif src.strip().rsplit('.', 1)[0] == mp_ip:
                        self.rtp_in += 1
                except Exception:
                    pass

    def _sample_reporter(self):
        interval = self.defaults['sample_interval']
        samples_path = self.dir / 'samples.ndjson'
        last_in = last_out = 0
        last_t = self.started_at
        with open(samples_path, 'a') as fh:
            while not self._shutdown.is_set():
                for _ in range(interval):
                    if self._shutdown.is_set():
                        return
                    time.sleep(1)
                now = time.time()
                elapsed = max(0.001, now - last_t)
                in_d = self.in_count - last_in
                out_d = self.out_count - last_out
                rec = {
                    't': round(now - self.started_at, 1),
                    'in': self.in_count, 'out': self.out_count,
                    'rtp_out': self.rtp_out, 'rtp_in': self.rtp_in,
                    'in_pps': round(in_d / elapsed, 1),
                    'out_pps': round(out_d / elapsed, 1),
                }
                try:
                    fh.write(json.dumps(rec) + '\n')
                    fh.flush()
                except Exception:
                    pass
                self._event('sample t={t}s in={i} (+{di}) out={o} (+{do}) rtp_out={ro} rtp_in={ri}'.format(
                    t=int(now - self.started_at), i=self.in_count, di=in_d,
                    o=self.out_count, do=out_d, ro=self.rtp_out, ri=self.rtp_in))
                last_in, last_out, last_t = self.in_count, self.out_count, now

    def _watchdog(self):
        deadline = self.started_at + self.defaults['max_capture_seconds']
        while not self._shutdown.is_set():
            if time.time() >= deadline:
                self._event('watchdog: max capture duration reached, finalizing')
                self.finalize(reason='watchdog')
                return
            time.sleep(1)

    def finalize(self, reason='stop'):
        with self._lock:
            if self.status == 'finished':
                return
            self.status = 'finalizing'
        self._shutdown.set()
        for proc in (self._text_proc, self._pcap_proc):
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self.ended_at = time.time()
        duration = self.ended_at - (self.started_at or self.ended_at)
        kb = self._read_json('kernel_before.json') or {}
        ka = {
            'net_dev': read_proc_net_dev(self.iface),
            'udp': read_udp_counters(),
            'conntrack': read_conntrack_count(),
            'at': now_iso(),
        }
        self._write_json('kernel_after.json', ka)
        summary = self._build_summary(duration, kb, ka, reason)
        self._write_json('summary.json', summary)
        self._event('end ' + summary['conclusion'])
        if summary.get('media_plane'):
            self._event('media_plane: ' + summary['media_plane'])
        try:
            if self._events_fh:
                self._events_fh.close()
        except Exception:
            pass
        with self._lock:
            self.status = 'finished'

    def _build_summary(self, duration, kb, ka, reason):
        expected = max(0, int(self.expected_pps * duration))
        nic_loss = 0.0
        if expected > 0 and self.in_count <= expected:
            nic_loss = 100.0 * (1.0 - self.in_count / expected)

        def delta(group, key):
            try:
                return (ka[group][key] - kb['net_dev' if group == 'net_dev' else group][key])
            except Exception:
                return None
        rx_drop_d = None
        udp_inerr_d = None
        try:
            rx_drop_d = ka['net_dev']['rx_drop'] - kb['net_dev']['rx_drop']
        except Exception:
            pass
        try:
            udp_inerr_d = ka['udp'].get('InErrors', 0) - kb['udp'].get('InErrors', 0)
        except Exception:
            pass

        if self.capture_error:
            conclusion = 'capture incomplete: {}'.format(self.capture_error)
        elif self.in_count == 0:
            conclusion = ('NO packets captured for this 5-tuple — verify client-ip/port '
                          '(NAT may rewrite) and interface')
        elif nic_loss < 5.0:
            conclusion = ('NIC received {} of ~{} packets cleanly — loss (if any) is '
                          'DOWNSTREAM of the NIC'.format(self.in_count, expected))
        else:
            missing = max(0, expected - self.in_count)
            conclusion = ('{:.1f}% of expected packets ({}/{}) did not reach the NIC — '
                          'drop is UPSTREAM of this host'.format(nic_loss, missing, expected))

        media_plane = None
        if self.mediaproxy_ip:
            wi, wo, ro, ri = self.in_count, self.out_count, self.rtp_out, self.rtp_in
            if wi > 0 and ro == 0:
                media_plane = 'OUTBOUND BREAK Janus->MediaProxy (phone RTP reaches Janus, not forwarded)'
            elif ri > 0 and wo == 0:
                media_plane = 'INBOUND BREAK Janus->phone (RTP returns from MediaProxy, not relayed to phone)'
            elif wi > 0 and ro > 0 and ri == 0:
                media_plane = 'INBOUND BREAK at/after MediaProxy (forwarded, nothing comes back)'
            elif wi > 0 and wo > 0 and ro > 0 and ri > 0:
                media_plane = 'both legs carrying RTP both ways — media plane intact at this host'
            else:
                media_plane = 'inconclusive — partial counts'

        return {
            'call_id': self.call_id,
            'reason': reason,
            'duration_s': round(duration, 1),
            'expected_packets': expected,
            'in_total': self.in_count,
            'out_total': self.out_count,
            'rtp_out_total': self.rtp_out,
            'rtp_in_total': self.rtp_in,
            'nic_loss_pct': round(nic_loss, 1),
            'kernel_rx_drop_delta': rx_drop_d,
            'kernel_udp_in_err_delta': udp_inerr_d,
            'capture_error': self.capture_error,
            'conclusion': conclusion,
            'media_plane': media_plane,
            'ended_at': now_iso(),
        }

    # -- persistence helpers ---------------------------------------------
    def _write_json(self, name, obj):
        try:
            with open(self.dir / name, 'w') as f:
                json.dump(obj, f, indent=2, default=str)
        except Exception as e:
            self._event('write {} failed: {}'.format(name, e))

    def _read_json(self, name):
        try:
            with open(self.dir / name) as f:
                return json.load(f)
        except Exception:
            return None

    def brief(self):
        return {
            'call_id': self.call_id,
            'status': self.status,
            'started_at': self.started_at and datetime.datetime.fromtimestamp(self.started_at).astimezone().isoformat(),
            'in': self.in_count, 'out': self.out_count,
            'rtp_out': self.rtp_out, 'rtp_in': self.rtp_in,
            'capture_error': self.capture_error,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class Registry(object):
    def __init__(self, log_dir, defaults):
        self.log_dir = Path(log_dir)
        self.defaults = defaults
        self.calls = {}            # folder name -> CallCapture
        self.lock = threading.Lock()

    def call_dir(self, call_id):
        return self.log_dir / safe_call_id(call_id)

    def register(self, params):
        folder = safe_call_id(params['call_id'])
        with self.lock:
            existing = self.calls.get(folder)
            if existing and existing.status in ('capturing', 'registered', 'finalizing'):
                return existing, False
            cap = CallCapture(params, self.call_dir(params['call_id']), self.defaults)
            self.calls[folder] = cap
        cap.start()
        return cap, True

    def stop(self, call_id):
        folder = safe_call_id(call_id)
        cap = self.calls.get(folder)
        if cap is None:
            return None
        cap.finalize(reason='stop')
        return cap

    def get(self, call_id):
        return self.calls.get(safe_call_id(call_id))

    def list_active(self):
        with self.lock:
            return [c.brief() for c in self.calls.values()]

    def prune(self, retention_days):
        if retention_days <= 0:
            return
        cutoff = time.time() - retention_days * 86400
        try:
            for child in self.log_dir.iterdir():
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

# Files allowed for direct artifact download (no path traversal).
_ARTIFACTS = {'meta.json', 'summary.json', 'samples.ndjson', 'events.log',
              'kernel_before.json', 'kernel_after.json', 'capture.pcap'}


def make_handler(registry, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'sylk-qos-server/' + VERSION
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass  # silence default stderr logging; events.log is the record

        # -- auth ---------------------------------------------------------
        def _authorized(self):
            if not token:
                # No token configured: only localhost may talk to us.
                return self.client_address[0] in ('127.0.0.1', '::1')
            got = self.headers.get('Authorization', '')
            if got.startswith('Bearer '):
                got = got[7:].strip()
            else:
                got = parse_qs(urlparse(self.path).query).get('token', [''])[0]
            return got == token

        # -- responses ----------------------------------------------------
        def _json(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, msg):
            self._json(code, {'error': msg})

        def _read_body(self):
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                return None

        # -- routing ------------------------------------------------------
        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/health':
                return self._json(200, {'status': 'ok', 'version': VERSION,
                                        'active': len(registry.list_active())})
            if not self._authorized():
                return self._err(401, 'unauthorized')
            if path == '/calls':
                return self._json(200, registry.list_active())
            parts = [unquote(p) for p in path.strip('/').split('/')]
            if len(parts) >= 2 and parts[0] == 'calls':
                call_id = parts[1]
                if len(parts) == 2:
                    return self._manifest(call_id)
                sub = parts[2]
                if sub == 'bundle':
                    return self._bundle(call_id)
                if sub in _ARTIFACTS:
                    return self._artifact(call_id, sub)
                return self._err(404, 'unknown sub-resource')
            return self._err(404, 'not found')

        def do_POST(self):
            if not self._authorized():
                return self._err(401, 'unauthorized')
            path = urlparse(self.path).path
            parts = [unquote(p) for p in path.strip('/').split('/')]
            if path == '/calls':
                return self._register()
            if len(parts) == 3 and parts[0] == 'calls' and parts[2] == 'stop':
                return self._stop(parts[1])
            return self._err(404, 'not found')

        # -- handlers -----------------------------------------------------
        def _register(self):
            body = self._read_body()
            if body is None:
                return self._err(400, 'invalid JSON body')
            required = ('call_id', 'client_ip', 'client_port', 'server_port')
            missing = [k for k in required if not body.get(k)]
            if missing:
                return self._err(400, 'missing fields: {}'.format(', '.join(missing)))
            try:
                body['client_port'] = int(body['client_port'])
                body['server_port'] = int(body['server_port'])
                if body.get('rtp_port_min'):
                    body['rtp_port_min'] = int(body['rtp_port_min'])
                if body.get('rtp_port_max'):
                    body['rtp_port_max'] = int(body['rtp_port_max'])
            except (TypeError, ValueError):
                return self._err(400, 'ports must be integers')
            cap, created = registry.register(body)
            return self._json(201 if created else 200, {
                'call_id': cap.call_id, 'status': cap.status,
                'dir': str(cap.dir), 'created': created,
                'capture_error': cap.capture_error,
            })

        def _stop(self, call_id):
            cap = registry.stop(call_id)
            if cap is None:
                return self._err(404, 'unknown call_id')
            return self._json(200, {'call_id': cap.call_id, 'status': cap.status,
                                    'summary': cap._read_json('summary.json')})

        def _manifest(self, call_id):
            d = registry.call_dir(call_id)
            if not d.is_dir():
                return self._err(404, 'unknown call_id')
            artifacts = []
            for name in sorted(_ARTIFACTS):
                fp = d / name
                if fp.exists():
                    artifacts.append({'name': name, 'size': fp.stat().st_size})
            cap = registry.get(call_id)
            return self._json(200, {
                'call_id': call_id,
                'status': cap.status if cap else 'finished',
                'meta': _load(d / 'meta.json'),
                'summary': _load(d / 'summary.json'),
                'artifacts': artifacts,
            })

        def _artifact(self, call_id, name):
            fp = registry.call_dir(call_id) / name
            if not fp.is_file():
                return self._err(404, 'no such artifact')
            ctype = 'application/octet-stream' if name.endswith('.pcap') else (
                'application/x-ndjson' if name.endswith('.ndjson') else
                'application/json' if name.endswith('.json') else 'text/plain')
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition', 'attachment; filename="{}"'.format(name))
            self.end_headers()
            self.wfile.write(data)

        def _bundle(self, call_id):
            d = registry.call_dir(call_id)
            if not d.is_dir():
                return self._err(404, 'unknown call_id')
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                tar.add(str(d), arcname=safe_call_id(call_id))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition',
                             'attachment; filename="{}.tar.gz"'.format(safe_call_id(call_id)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config-dir', default=DEFAULT_CONFIG_DIR, metavar='PATH',
                   help='SylkServer configuration directory holding config.ini and '
                        'qos-server.ini (default: {})'.format(DEFAULT_CONFIG_DIR))
    p.add_argument('--version', action='version', version='sylk-qos-server ' + VERSION)
    args = p.parse_args()

    cfg = load_config(args.config_dir)

    host, _, port = cfg['listen'].rpartition(':')
    host = host or '0.0.0.0'
    try:
        port = int(port)
    except ValueError:
        raise SystemExit('invalid [Server] listen value: {!r}'.format(cfg['listen']))

    log_dir = Path(cfg['log_dir'])
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise SystemExit('cannot create trace dir {} (is [Server] trace_dir in '
                         'config.ini writable by this user?): {}'.format(log_dir, e))

    defaults = {
        'interface': cfg['interface'] or primary_interface(),
        'mediaproxy_ip': cfg['mediaproxy_ip'],
        'rtp_port_min': cfg['rtp_port_min'],
        'rtp_port_max': cfg['rtp_port_max'],
        'expected_pps': cfg['expected_pps'],
        'sample_interval': cfg['sample_interval'],
        'max_capture_seconds': cfg['max_capture_seconds'],
    }
    registry = Registry(log_dir, defaults)
    registry.prune(cfg['retention_days'])

    # Background retention sweep, once a day.
    def retention_loop():
        while True:
            time.sleep(86400)
            registry.prune(cfg['retention_days'])
    threading.Thread(target=retention_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), make_handler(registry, cfg['auth_token']))

    def _do_shutdown():
        for cap in list(registry.calls.values()):
            try:
                cap.finalize(reason='daemon-shutdown')
            except Exception:
                pass
        httpd.shutdown()  # unblocks serve_forever() on the main thread

    def shutdown(*_):
        # Must NOT call httpd.shutdown() directly here: this handler runs on the
        # main thread, which is blocked inside serve_forever(); httpd.shutdown()
        # would then wait for a loop that can't run -> deadlock (Ctrl-C hangs).
        # Run the teardown on a separate thread so this handler returns at once,
        # letting serve_forever() resume and observe the stop request.
        threading.Thread(target=_do_shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def logline(msg):
        print('{} [sylk-qos-server] {}'.format(now_iso(), msg), flush=True)

    rtp_range = ('{}-{}'.format(cfg['rtp_port_min'], cfg['rtp_port_max'])
                 if cfg['rtp_port_min'] and cfg['rtp_port_max'] else 'any')
    startup = [
        ('version', VERSION),
        ('config_dir', cfg['config_dir']),
        ('config_files', ', '.join(cfg['config_files']) or '(none found, using defaults)'),
        ('listen', '{}:{}'.format(host, port)),
        ('auth', 'token' if cfg['auth_token'] else 'localhost-only (no auth_token set)'),
        ('trace_dir', cfg['trace_dir']),
        ('log_dir', str(log_dir)),
        ('interface', defaults['interface']),
        ('expected_pps', defaults['expected_pps']),
        ('sample_interval', '{}s'.format(defaults['sample_interval'])),
        ('max_capture_seconds', defaults['max_capture_seconds']),
        ('retention_days', cfg['retention_days']),
        ('mediaproxy_ip', cfg['mediaproxy_ip'] or 'disabled (RTP leg not captured)'),
        ('rtp_port_range', rtp_range),
        ('tcpdump', 'available' if tcpdump_available() else 'MISSING (metadata-only captures)'),
    ]
    logline('starting — effective configuration:')
    width = max(len(k) for k, _ in startup)
    for key, value in startup:
        logline('  {:<{w}} = {}'.format(key, value, w=width))
    logline('ready, accepting requests')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown()


if __name__ == '__main__':
    main()
