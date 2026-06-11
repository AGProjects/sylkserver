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
                server_ip?, mediaproxy_ip?, expected_pps?}
    POST /calls/{call_id}/stop        -> finalize capture
    GET  /calls                       -> [{call_id, status, ...}, ...] (in progress)
    GET  /finalized                   -> [{call_id, status, ...}, ...] (recently finished)
                                         text/html -> recent finalized calls page
    GET  /calls/{call_id}             -> manifest {meta, summary}
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
import ssl
import subprocess
import tarfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs, quote

VERSION = '0.2.29'  # bumped by bump-version.sh on each build

# SylkServer's default configuration directory (overridable with --config-dir).
DEFAULT_CONFIG_DIR = '/etc/sylkserver'
# Janus' own config dir, used to read its RTP port range so the Janus<->
# MediaProxy capture matches the Janus server. Override via [Janus] config_dir.
DEFAULT_JANUS_CONFIG_DIR = '/etc/janus'


def _jcfg_text(path):
    text = open(path).read()
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'(?m)//.*$', '', text)
    text = re.sub(r'(?m)#.*$', '', text)
    return text


def _jcfg_value(text, key):
    m = re.search(r'(?m)^\s*' + re.escape(key) + r'\s*[:=]\s*(?:"([^"]*)"|([^\s;,]+))', text)
    if not m:
        return None
    return (m.group(1) if m.group(1) is not None else m.group(2)).strip()


def read_janus_admin(janus_dir):
    """Auto-detect the Janus Admin API URL + secret from Janus' own config:
    admin_secret from janus.jcfg, admin port/base from janus.transport.http.jcfg.
    Logs what it reads. Returns (url, secret, url_file, secret_file) — the *_file
    entries name the file a value came from (None if not found)."""
    secret = url = secret_file = url_file = None

    janus_cfg = os.path.join(janus_dir, 'janus.jcfg')
    if os.path.isfile(janus_cfg):
        try:
            secret = _jcfg_value(_jcfg_text(janus_cfg), 'admin_secret')
            if secret:
                secret_file = janus_cfg
            log_line('janus config: read {} (admin_secret: {})'.format(
                janus_cfg, 'found' if secret else 'not set / commented out'))
        except Exception as e:
            log_line('janus config: failed to read {}: {}'.format(janus_cfg, e))
    else:
        log_line('janus config: {} not found'.format(janus_cfg))

    http_cfg = os.path.join(janus_dir, 'janus.transport.http.jcfg')
    if os.path.isfile(http_cfg):
        try:
            text = _jcfg_text(http_cfg)
            enabled = _jcfg_value(text, 'admin_http')
            port = _jcfg_value(text, 'admin_port') or '7088'
            base = _jcfg_value(text, 'admin_base_path') or '/admin'
            if not base.startswith('/'):
                base = '/' + base
            if enabled is not None and str(enabled).lower() not in ('true', 'yes', '1'):
                log_line('janus config: read {} but admin_http={} (admin API DISABLED '
                         'in Janus)'.format(http_cfg, enabled))
            else:
                url = 'http://127.0.0.1:{}{}'.format(port, base)
                url_file = http_cfg
                log_line('janus config: read {} (admin endpoint: {}, admin_http={})'.format(
                    http_cfg, url, enabled))
        except Exception as e:
            log_line('janus config: failed to read {}: {}'.format(http_cfg, e))
    else:
        log_line('janus config: {} not found'.format(http_cfg))

    return url, secret, url_file, secret_file


def janus_admin_request(admin_url, admin_secret, body, timeout=4):
    req = dict(body)
    req['transaction'] = uuid.uuid4().hex
    if admin_secret:
        req['admin_secret'] = admin_secret
    data = json.dumps(req).encode()
    r = urllib.request.Request(admin_url, data=data,
                               headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def janus_handle_info(admin_url, admin_secret, session_id, handle_id, timeout=4):
    data = janus_admin_request(admin_url, admin_secret,
                               {'janus': 'handle_info', 'session_id': session_id, 'handle_id': handle_id}, timeout)
    return data.get('info', {}) or {}


_IPPORT_RE = re.compile(r'(\d{1,3}(?:\.\d{1,3}){3}):(\d+)')


def find_call_id(info):
    """Find the SIP Call-ID anywhere in a Janus handle_info dict."""
    result = [None]

    def walk(node):
        if result[0] or not isinstance(node, (dict, list)):
            return
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in items:
            if result[0]:
                return
            if isinstance(key, str) and key.lower().replace('-', '_') == 'call_id' \
                    and isinstance(value, str) and value:
                result[0] = value
                return
            walk(value)

    walk(info)
    return result[0]


def find_selected_pair(info):
    """Extract the established ICE 5-tuple from handle_info.

    Janus reports a component's chosen pair as a 'selected-pair' string like
    '10.0.0.1:9000 [host,udp] <-> 86.1.2.3:42744 [prflx,udp]' (local <-> remote).
    Returns (local_ip, local_port, remote_ip, remote_port) — i.e.
    (server_ip, server_port, client_ip, client_port) — or None."""
    pairs = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.lower().replace('_', '-') == 'selected-pair' \
                        and isinstance(value, str):
                    pairs.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(info)
    for s in pairs:
        sides = s.split('<->')
        if len(sides) != 2:
            continue
        lm = _IPPORT_RE.search(sides[0])
        rm = _IPPORT_RE.search(sides[1])
        if lm and rm:
            return (lm.group(1), int(lm.group(2)), rm.group(1), int(rm.group(2)))
    return None


def _collect_sdps(info):
    """Return all SDP strings found anywhere in a handle_info dict."""
    out = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and ('m=audio' in node or 'm=video' in node):
            out.append(node)

    walk(info)
    return out


def find_media_types(info):
    """List the media types in the call ('audio', 'video') from the SDP m= lines
    in handle_info. Empty if none found."""
    types = []
    for sdp in _collect_sdps(info):
        for line in sdp.splitlines():
            line = line.strip()
            if line.startswith('m=audio') and 'audio' not in types:
                types.append('audio')
            elif line.startswith('m=video') and 'video' not in types:
                types.append('video')
    return types


_XSYLK_RE = re.compile(r'X-Sylk-Session-Id:\s*([^\s\\"]+)', re.IGNORECASE)


def find_sylk_session_id(ev):
    """Extract the X-Sylk-Session-Id header (the Sylk client's session id) from
    a SIP message carried in a Janus event. Returns it or None."""
    try:
        m = _XSYLK_RE.search(json.dumps(ev))
        return m.group(1) if m else None
    except Exception:
        return None


def find_sip_media_all(info):
    """From a Janus SIP-plugin handle_info / event, return ALL SIP-side media
    streams (audio + video + ...), each as {'media','remote_ip','remote_port'}.

    The WebRTC PeerConnection SDP uses an *SAVPF* profile with ICE/DTLS and
    BUNDLEs every track onto ONE port; the SIP-side SDP is plain RTP/AVP and
    carries a SEPARATE port pair per m= line. So a video call has (at least) an
    audio and a video stream on the SIP side and we must watch every one of
    them — not just the first."""
    for sdp in _collect_sdps(info):
        if 'SAVPF' in sdp or 'a=candidate' in sdp or 'a=fingerprint' in sdp:
            continue  # WebRTC PeerConnection SDP, not the SIP side
        sess_ip = None
        streams = []
        cur = None
        for raw in sdp.splitlines():
            line = raw.strip()
            if line.startswith('m='):
                parts = line.split()
                media = parts[0][2:] if parts else ''
                port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                cur = {'media': media, 'remote_ip': None, 'remote_port': port}
                streams.append(cur)
            elif line.startswith('c='):
                toks = line.split()
                ip = toks[2] if len(toks) >= 3 else None
                if cur is None:
                    sess_ip = ip            # session-level c= (before any m=)
                else:
                    cur['remote_ip'] = ip   # media-level c= overrides
        for s in streams:
            if s['remote_ip'] is None:
                s['remote_ip'] = sess_ip
        streams = [s for s in streams
                   if s['remote_ip'] and s['remote_port'] and s['remote_ip'] not in ('0.0.0.0',)]
        if streams:
            return streams
    return []


def find_sip_media(info):
    """Single-endpoint convenience wrapper around find_sip_media_all: the
    'primary' SIP media endpoint (audio if present, else the first stream).
    Returns {'remote_ip','remote_port'} or None."""
    streams = find_sip_media_all(info)
    for s in streams:
        if s['media'] == 'audio':
            return {'remote_ip': s['remote_ip'], 'remote_port': s['remote_port']}
    if streams:
        return {'remote_ip': streams[0]['remote_ip'], 'remote_port': streams[0]['remote_port']}
    return None


_UA_KEYS = ('User-Agent', 'Server')


def find_user_agent(ev):
    """Best-effort SIP User-Agent / Server header from a Janus event. Janus
    sometimes surfaces SIP headers (esp. on incoming messages) either as a JSON
    object ("User-Agent":"...") or embedded raw ("User-Agent: ..."). Returns the
    string or None."""
    try:
        s = ev if isinstance(ev, str) else json.dumps(ev)
    except Exception:
        return None
    for name in _UA_KEYS:
        m = re.search(r'"' + name + r'"\s*:\s*"([^"]+)"', s, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(name + r':\s*([^\r\n"\\,}]+)', s, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


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
    # Reuse SylkServer's webrtcgateway TLS: [WebServer] in config.ini holds the
    # HTTPS certificate (a PEM with the private key concatenated), optional
    # chain, and the hostname (the cert CN).
    tls_certificate = main.get('WebServer', 'certificate', fallback='').strip()
    tls_certificate_chain = main.get('WebServer', 'certificate_chain', fallback='').strip()
    tls_hostname = main.get('WebServer', 'hostname', fallback='').strip()

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

    def getbool(section, key, fallback):
        try:
            return qos.getboolean(section, key) if qos.has_option(section, key) else fallback
        except ValueError:
            return fallback

    janus_dir = get('Janus', 'config_dir', DEFAULT_JANUS_CONFIG_DIR)
    # Auto-capture mode: 'events' (Janus pushes via event handler — preferred),
    # 'poll' (we poll the admin API), or 'off'. `monitor = false` forces off
    # (back-compat).
    janus_mode = get('Janus', 'mode', 'events').lower()
    if not getbool('Janus', 'monitor', True):
        janus_mode = 'off'
    if janus_mode not in ('off', 'poll', 'events'):
        janus_mode = 'events'
    janus_admin_url = get('Janus', 'admin_url', '')
    janus_admin_secret = get('Janus', 'admin_secret', '')
    janus_admin_url_source = 'qos-server.ini' if janus_admin_url else None
    janus_admin_secret_source = 'qos-server.ini' if janus_admin_secret else None
    # poll mode needs the admin API; events mode uses it only to fill 5-tuple
    # gaps, but auto-detect it anyway so it's available.
    if janus_mode in ('poll', 'events') and (not janus_admin_url or not janus_admin_secret):
        d_url, d_secret, d_url_file, d_secret_file = read_janus_admin(janus_dir)
        if not janus_admin_url:
            janus_admin_url = d_url or 'http://127.0.0.1:7088/admin'
            janus_admin_url_source = d_url_file or 'built-in default'
        if not janus_admin_secret:
            janus_admin_secret = d_secret or ''
            janus_admin_secret_source = d_secret_file or 'not found'
    janus_poll_interval = max(1, getint('Janus', 'poll_interval', 2))

    return {
        'config_dir': config_dir,
        'config_files': [p for p in (main_ini, qos_ini) if os.path.isfile(p)],
        'trace_dir': trace_dir,
        'tls_certificate': tls_certificate or None,
        'tls_certificate_chain': tls_certificate_chain or None,
        'tls_hostname': tls_hostname or None,
        # Per-call artifacts go under <trace_dir>/qos/<call-id>/
        'log_dir': os.path.join(trace_dir, 'qos'),
        'listen': get('Server', 'listen', '0.0.0.0:9810'),
        # Plain-HTTP loopback listener so the co-located Janus can push events
        # over http://127.0.0.1 (the HTTPS cert CN won't match 127.0.0.1). 0 = off.
        'local_http_port': getint('Server', 'local_http_port', 9809),
        'auth_token': get('Server', 'auth_token', '') or None,
        'interface': get('Server', 'interface', '') or None,
        'expected_pps': getint('Server', 'expected_pps', 50),
        'sample_interval': max(1, getint('Server', 'sample_interval', 5)),
        'max_capture_seconds': max(10, getint('Server', 'max_capture_seconds', 7200)),
        'retention_days': getint('Server', 'retention_days', 7),
        # pcap retention: 'no' (never write pcap, just count — default),
        # 'problem' (keep only for calls with a media problem), 'yes' (always).
        'keep_pcap': (get('Server', 'keep_pcap', 'no').lower()
                      if get('Server', 'keep_pcap', 'no').lower() in ('no', 'problem', 'yes')
                      else 'no'),
        # Optional secondary leg: capture all UDP to/from the MediaProxy relay
        # host. The per-call RTP PORTS are negotiated in the SIP SDP (not visible
        # here) and are not needed — filtering by host catches the leg whatever
        # ports the call uses. The WebRTC leg's exact 5-tuple comes per-call from
        # Janus ICE, never from config.
        'mediaproxy_ip': get('MediaProxy', 'ip', '') or None,
        'janus_config_dir': janus_dir,
        'janus_mode': janus_mode,
        'janus_admin_url': janus_admin_url,
        'janus_admin_url_source': janus_admin_url_source,
        'janus_admin_secret': janus_admin_secret or None,
        'janus_admin_secret_source': janus_admin_secret_source,
        'janus_poll_interval': janus_poll_interval,
    }

# ---------------------------------------------------------------------------
# Low-level host counters (ported from qos-server.py)
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.datetime.now().astimezone().isoformat()


def log_line(msg):
    print('{} [sylk-qos-server] {}'.format(now_iso(), msg), flush=True)


def _h(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def default_route():
    """Return (host_ip, gateway_ip, iface) for the default route, or (None, ...)."""
    src = gw = iface = None
    try:
        out = subprocess.check_output(['ip', '-o', '-4', 'route', 'get', '1.1.1.1'], text=True)
        toks = out.split()
        for i, t in enumerate(toks):
            if t == 'via' and i + 1 < len(toks):
                gw = toks[i + 1]
            elif t == 'dev' and i + 1 < len(toks):
                iface = toks[i + 1]
            elif t == 'src' and i + 1 < len(toks):
                src = toks[i + 1]
    except Exception:
        pass
    return src, gw, iface


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
        # SIP Call-ID — in events/poll mode this equals call_id (we key on it);
        # kept as an explicit field in the saved JSON.
        self.sip_call_id = params.get('sip_call_id') or params['call_id']
        self.sylk_session_id = params.get('sylk_session_id')  # X-Sylk-Session-Id
        self.iface = params.get('interface') or defaults['interface']
        self.expected_pps = int(params.get('expected_pps') or defaults['expected_pps'])
        self.mediaproxy_ip = params.get('mediaproxy_ip') or defaults.get('mediaproxy_ip')
        self.sip_remote_port = params.get('sip_remote_port')
        # All SIP-side streams (audio + video): [{media, remote_ip, remote_port}].
        self.sip_streams = params.get('sip_streams') or []
        self.sip_local_ip = params.get('sip_local_ip') or params.get('server_ip')
        self.sip_local_port = params.get('sip_local_port')
        self.media_types = params.get('media_types') or []
        self.user_agent_local = params.get('user_agent_local')    # near (Janus/Sylk INVITE)
        self.user_agent_remote = params.get('user_agent_remote')  # far (remote answer)
        self.is_video = 'video' in [str(m).lower() for m in self.media_types]
        self.keep_pcap = params.get('keep_pcap') or defaults.get('keep_pcap') or 'no'

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
        self._flow_seen = set()  # flows we've already logged "media flowing" for

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
        # WebRTC leg: the exact per-call 5-tuple from Janus ICE (client <-> Janus).
        bpf = ('(src host {cip} and src port {cp} and dst port {sp}) or '
               '(dst host {cip} and dst port {cp} and src port {sp})').format(
            cip=p['client_ip'], cp=p['client_port'], sp=p['server_port'])
        # Downstream (SIP) leg: Janus <-> MediaProxy. We learn each MediaProxy
        # RTP port from the SIP answer SDP and filter to those exact host+ports
        # (and +1 for each RTCP). A video call has one port pair PER stream
        # (audio + video), so we watch ALL of them — host-only would also catch
        # OTHER calls relayed by the same MediaProxy and inflate the counts.
        if self.mediaproxy_ip:
            ports = []
            for s in self.sip_streams:
                p = s.get('remote_port')
                if p:
                    ports.extend([p, p + 1])           # RTP + RTCP
            if not ports and self.sip_remote_port:
                ports = [self.sip_remote_port, self.sip_remote_port + 1]
            if ports:
                seen = []
                for p in ports:
                    if p not in seen:
                        seen.append(p)
                port_expr = ' or '.join('port {}'.format(p) for p in seen)
                bpf = '({}) or (host {} and udp and ({}))'.format(bpf, self.mediaproxy_ip, port_expr)
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
            'sip_call_id': self.sip_call_id,
            'sylk_session_id': self.sylk_session_id,
            'dir': str(self.dir),
            'host': os.uname().nodename,
            'interface': self.iface,
            'bpf': bpf,
            'expected_pps': self.expected_pps,
            'mediaproxy_ip': self.mediaproxy_ip,
            'sip_remote_port': self.sip_remote_port,
            'sip_streams': self.sip_streams,
            'media_types': self.media_types,
            'user_agent_local': self.user_agent_local,
            'user_agent_remote': self.user_agent_remote,
            'keep_pcap': self.keep_pcap,
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
        mt = ', '.join(self.media_types) if self.media_types else '?'
        if self.media_types:
            log_line('call {} media: {}'.format(self.call_id, mt))
        log_line('call {} leg WebRTC [{}]: client {}:{} <-> Janus :{}'.format(
            self.call_id, mt, self.params.get('client_ip'), self.params.get('client_port'),
            self.params.get('server_port')))
        if self.mediaproxy_ip:
            janus_side = '{}:{}'.format(self.sip_local_ip or '?', self.sip_local_port or '?')
            mp_side = '{}:{}'.format(self.mediaproxy_ip, self.sip_remote_port or '?')
            log_line('call {} leg downstream (SIP) [{}]: Janus {} <-> MediaProxy {}'.format(
                self.call_id, mt, janus_side, mp_side))

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
        text_cmd = self._sudo(['tcpdump', '-i', self.iface, '-n', '-l', '-q', bpf])
        # The pcap (-w) is ONLY for opening in Wireshark; packet COUNTING is done
        # by the text tcpdump below, independently. So skip the pcap entirely
        # when keep_pcap == 'no' (just count). For 'problem' we still record it
        # and delete it at finalize unless the call had a media problem.
        if self.keep_pcap != 'no':
            pcap_cmd = self._sudo(['tcpdump', '-i', self.iface, '-n', '-U', '-w', pcap_path, bpf])
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

    def _flow_started(self, name, desc):
        """Log the first packet on a given RTP flow direction, once."""
        if name in self._flow_seen:
            return
        self._flow_seen.add(name)
        elapsed = (time.time() - self.started_at) if self.started_at else 0
        mt = '/'.join(self.media_types) if self.media_types else 'media'
        msg = 'media flowing [{}]: {} (first packet at +{:.1f}s)'.format(mt, desc, elapsed)
        self._event(msg)
        log_line('call {} {}'.format(self.call_id, msg))

    def _packet_reader(self):
        client_src_tag = '{}.{}'.format(self.params['client_ip'], self.params['client_port'])
        mp_ip = self.mediaproxy_ip
        for line in self._text_proc.stdout:
            if self._shutdown.is_set():
                break
            if '{} >'.format(client_src_tag) in line:
                self.in_count += 1
                if self.in_count == 1:
                    self._flow_started('in', 'client->server (phone RTP reaching Janus)')
            elif '> {}'.format(client_src_tag) in line:
                self.out_count += 1
                if self.out_count == 1:
                    self._flow_started('out', 'server->client (Janus RTP to phone)')
            if mp_ip and ' IP ' in line and ' > ' in line:
                try:
                    seg = line.split(' IP ', 1)[1]
                    src, rest = seg.split(' > ', 1)
                    dst = rest.split(':', 1)[0]
                    if dst.strip().rsplit('.', 1)[0] == mp_ip:
                        self.rtp_out += 1
                        if self.rtp_out == 1:
                            self._flow_started('rtp_out', 'Janus->MediaProxy')
                    elif src.strip().rsplit('.', 1)[0] == mp_ip:
                        self.rtp_in += 1
                        if self.rtp_in == 1:
                            self._flow_started('rtp_in', 'MediaProxy->Janus')
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

    def _maybe_drop_pcap(self, summary):
        """Apply the keep_pcap policy at call end:
          no       -> no pcap was written (nothing to do)
          problem  -> keep the pcap only if the call had a media problem
          yes      -> always keep
        The packet counts in summary.json don't need the pcap, so dropping it
        for healthy calls reclaims the (large) capture file."""
        pcap = self.dir / 'capture.pcap'
        try:
            if not pcap.exists():
                return
            keep = (self.keep_pcap == 'yes') or \
                   (self.keep_pcap == 'problem' and summary.get('media_ok') is not True)
            if keep:
                self._event('pcap kept ({} bytes, keep_pcap={}, media_ok={})'.format(
                    pcap.stat().st_size, self.keep_pcap, summary.get('media_ok')))
            else:
                size = pcap.stat().st_size
                pcap.unlink()
                try:
                    (self.dir / 'capture.pcap.err').unlink()
                except Exception:
                    pass
                self._event('pcap removed ({} bytes freed; call OK, keep_pcap=problem)'.format(size))
        except Exception as e:
            self._event('pcap retention error: {}'.format(e))

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
        self._maybe_drop_pcap(summary)
        self._event('end ' + summary['conclusion'])
        if summary.get('media_plane'):
            self._event('media_plane: ' + summary['media_plane'])
        # End-of-call verdict: did media flow both ways?
        self._event('EVALUATION: ' + summary['evaluation_text'])
        log_line('call {} ENDED — {} (WebRTC client->server={} server->client={}{}) dur={:.0f}s — saved in {}'.format(
            self.call_id, summary['evaluation_text'], self.in_count, self.out_count,
            '; Janus->MP={} MP->Janus={}'.format(self.rtp_out, self.rtp_in) if self.mediaproxy_ip else '',
            duration, self.dir))
        try:
            if self._events_fh:
                self._events_fh.close()
        except Exception:
            pass
        with self._lock:
            self.status = 'finished'

    def _build_summary(self, duration, kb, ka, reason):
        # NIC-loss heuristic is an AUDIO-ONLY model (constant ~expected_pps).
        # Video adds a large, bursty, variable-rate stream, so a duration x pps
        # "expected" is meaningless on a video call — don't compute it there.
        expected = 0 if self.is_video else max(0, int(self.expected_pps * duration))
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
        elif self.is_video:
            # No fixed packet-rate expectation for video; report raw delivery.
            conclusion = ('video call — {} packets received at the NIC (no fixed '
                          'per-packet expectation for video; see per-leg counts)'.format(self.in_count))
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

        # Did media flow both ways on the WebRTC (client<->Janus) leg?
        # A direction must carry a MEANINGFUL number of packets, not just >0 — a
        # one-way call often still has a trickle (comfort noise / a few initial
        # packets) on the dead side. "Meaningful" = at least FLOOR packets AND at
        # least RATIO of the busier direction (audio is ~symmetric in pps).
        flow_floor = 16          # ~0.3s of 50pps audio
        flow_ratio = 0.2
        busier = max(self.in_count, self.out_count)
        cs = self.in_count >= flow_floor and self.in_count >= flow_ratio * busier
        sc = self.out_count >= flow_floor and self.out_count >= flow_ratio * busier
        if self.capture_error:
            evaluation, media_ok = 'unknown', None
            evaluation_text = 'EVALUATION UNKNOWN — capture incomplete ({})'.format(self.capture_error)
        elif cs and sc:
            evaluation, media_ok = 'ok', True
            evaluation_text = 'OK — media flowing both ways'
        elif cs and not sc:
            evaluation, media_ok = 'one-way-client-to-server', False
            evaluation_text = ('CALL NOT OK — ONE-WAY: client->server only '
                               '(server->client={} pkts, ~nothing reaching the phone)'.format(self.out_count))
        elif sc and not cs:
            evaluation, media_ok = 'one-way-server-to-client', False
            evaluation_text = ('CALL NOT OK — ONE-WAY: server->client only '
                               '(client->server={} pkts)'.format(self.in_count))
        else:
            evaluation, media_ok = 'no-media', False
            evaluation_text = 'CALL NOT OK — NO media in either direction'

        # Loss grade (only when media flows both ways): >10% = bad (degraded),
        # >30% = broken.
        #
        # We grade on intra-call DIRECTION SYMMETRY, not on a duration x pps
        # baseline. Audio is ~symmetric in packet rate, so over the same window
        # both directions should carry a similar number of packets; the deficit
        # of the lighter direction vs the busier one is the loss the relay path
        # actually shows. A duration baseline punishes SHORT calls unfairly,
        # because ICE+DTLS setup eats 1-2s before any RTP flows (a 7s call only
        # carries ~5s of media but gets graded as if it should carry 7s).
        #
        # We also REFUSE to grade calls that are too short / too small to be
        # meaningful — there just aren't enough packets to call a stream broken.
        #
        # Video is exempt: its two directions are NOT symmetric in packet rate
        # (resolution/bitrate/keyframes differ per sender, and one side may send
        # video while the other sends only audio), so the symmetry deficit is
        # not loss. We confirm both directions FLOW for video, but don't grade %.
        min_grade_seconds = 5.0
        min_grade_packets = 250
        loss_pct = None
        if evaluation == 'ok' and self.is_video:
            evaluation_text = 'OK — media flowing both ways'
        elif evaluation == 'ok':
            busier = max(self.in_count, self.out_count)
            lighter = min(self.in_count, self.out_count)
            if busier > 0:
                loss_pct = round(100.0 * (busier - lighter) / busier, 1)
            gradable = duration >= min_grade_seconds and busier >= min_grade_packets
            if not gradable:
                evaluation_text = 'OK — media flowing both ways'
            elif loss_pct is not None and loss_pct > 30:
                evaluation, media_ok = 'broken', False
                evaluation_text = 'CALL BROKEN — {:.0f}% packet loss (media stream broken)'.format(loss_pct)
            elif loss_pct is not None and loss_pct > 10:
                evaluation, media_ok = 'bad', False
                evaluation_text = 'CALL BAD — {:.0f}% packet loss (degraded)'.format(loss_pct)
            elif loss_pct:
                # only mention loss when there actually is some
                evaluation_text = 'OK — media flowing both ways ({:.0f}% loss)'.format(loss_pct)
            else:
                evaluation_text = 'OK — media flowing both ways'

        # Packet counts tagged per leg. user_agent_local is the SIP identity
        # Janus/Sylk presents (our outgoing INVITE); user_agent_remote is the
        # far party (from its answer). The phone's own app UA is added by the
        # client when it reconciles (the daemon can't see it — it's a WebRTC,
        # not a SIP, endpoint).
        legs = {
            'webrtc': {
                'description': 'Sylk client <-> Janus',
                'client': '{}:{}'.format(self.params.get('client_ip'), self.params.get('client_port')),
                'janus_ip': self.params.get('server_ip'),
                'janus_port': self.params.get('server_port'),
                'janus': '{}:{}'.format(self.params.get('server_ip') or '?', self.params.get('server_port') or '?'),
                'janus_user_agent': self.user_agent_local,
                'packets_client_to_server': self.in_count,
                'packets_server_to_client': self.out_count,
            },
        }
        if self.mediaproxy_ip:
            legs['sip'] = {
                'description': 'Janus <-> MediaProxy',
                'janus': '{}:{}'.format(self.sip_local_ip or '?', self.sip_local_port or '?'),
                'mediaproxy': '{}:{}'.format(self.mediaproxy_ip, self.sip_remote_port or '?'),
                'janus_user_agent': self.user_agent_local,
                'remote_user_agent': self.user_agent_remote,
                'streams': self.sip_streams,
                'packets_janus_to_mediaproxy': self.rtp_out,
                'packets_mediaproxy_to_janus': self.rtp_in,
            }

        return {
            'call_id': self.call_id,
            'sip_call_id': self.sip_call_id,
            'sylk_session_id': self.sylk_session_id,
            'reason': reason,
            'duration_s': round(duration, 1),
            'media_types': self.media_types,
            'is_video': self.is_video,
            'user_agents': {
                'local': self.user_agent_local,    # Janus/Sylk SIP side
                'remote': self.user_agent_remote,  # far party
            },
            'expected_packets': expected,
            'legs': legs,
            # flat totals kept for back-compat:
            'in_total': self.in_count,
            'out_total': self.out_count,
            'rtp_out_total': self.rtp_out,
            'rtp_in_total': self.rtp_in,
            'mediaproxy_ip': self.mediaproxy_ip,
            'nic_loss_pct': round(nic_loss, 1),
            'kernel_rx_drop_delta': rx_drop_d,
            'kernel_udp_in_err_delta': udp_inerr_d,
            'capture_error': self.capture_error,
            'evaluation': evaluation,
            'media_ok': media_ok,
            'loss_pct': loss_pct,
            'evaluation_text': evaluation_text,
            'conclusion': conclusion,
            'media_plane': media_plane,
            'ended_at': now_iso(),
        }

    # -- persistence helpers ---------------------------------------------
    def _write_json(self, name, obj):
        try:
            with open(self.dir / name, 'w', encoding='utf-8') as f:
                # ensure_ascii=False keeps the em-dash (—) etc. as real UTF-8
                # in summary.json rather than "—" escapes.
                json.dump(obj, f, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            self._event('write {} failed: {}'.format(name, e))

    def _read_json(self, name):
        try:
            with open(self.dir / name) as f:
                return json.load(f)
        except Exception:
            return None

    def _live_eval(self):
        if self.capture_error:
            return 'unknown'
        cs, sc = self.in_count > 0, self.out_count > 0
        if cs and sc:
            return 'ok (both ways)'
        if cs:
            return 'one-way client->server'
        if sc:
            return 'one-way server->client'
        return 'no media yet'

    def brief(self):
        if self.started_at:
            end = self.ended_at or time.time()
            duration = round(end - self.started_at, 1)
        else:
            duration = 0
        return {
            'call_id': self.call_id,
            'status': self.status,
            'started_at': self.started_at and datetime.datetime.fromtimestamp(self.started_at).astimezone().isoformat(),
            'duration_s': duration,
            'media_types': self.media_types,
            'client': '{}:{}'.format(self.params.get('client_ip'), self.params.get('client_port')),
            'server_port': self.params.get('server_port'),
            'mediaproxy': self.mediaproxy_ip,
            'webrtc_in': self.in_count, 'webrtc_out': self.out_count,
            'rtp_out': self.rtp_out, 'rtp_in': self.rtp_in,
            'flows': sorted(self._flow_seen),
            'evaluation': self._live_eval(),
            'capture_error': self.capture_error,
            'dir': str(self.dir),
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

    def new_call_dir(self, call_id):
        """Fresh per-call folder: <log_dir>/<YYYYMMDD>/<YYYYMMDD-HHMMSS>-<call-id>/."""
        now = datetime.datetime.now()
        return (self.log_dir / now.strftime('%Y%m%d')
                / '{}-{}'.format(now.strftime('%Y%m%d-%H%M%S'), safe_call_id(call_id)))

    def find_call_dir(self, call_id):
        """Locate an existing call folder by Call-ID (active first, else newest
        matching folder across the per-day directories)."""
        cap = self.calls.get(safe_call_id(call_id))
        if cap is not None and Path(cap.dir).is_dir():
            return Path(cap.dir)
        safe = safe_call_id(call_id)
        try:
            matches = sorted(self.log_dir.glob('*/*-' + safe),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            matches = []
        return matches[0] if matches else (self.log_dir / '__missing__' / safe)

    def resolve_dir(self, ident):
        """Locate a call folder by SIP Call-ID (our key) OR Sylk session id
        (X-Sylk-Session-Id). Returns a Path or None."""
        # 1. by Call-ID (the folder key)
        d = self.find_call_dir(ident)
        if d.is_dir():
            return d
        # 2. active captures, by session id / sip_call_id
        for cap in list(self.calls.values()):
            if ident in (getattr(cap, 'sylk_session_id', None),
                         getattr(cap, 'sip_call_id', None)):
                p = Path(cap.dir)
                if p.is_dir():
                    return p
        # 3. scan on-disk meta.json (newest first)
        try:
            metas = sorted(self.log_dir.glob('*/*/meta.json'),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            metas = []
        for meta_path in metas:
            try:
                m = json.load(open(meta_path))
            except Exception:
                continue
            if ident in (m.get('call_id'), m.get('sip_call_id'), m.get('sylk_session_id')):
                return meta_path.parent
        return None

    def register(self, params):
        folder = safe_call_id(params['call_id'])
        with self.lock:
            existing = self.calls.get(folder)
            if existing and existing.status in ('capturing', 'registered', 'finalizing'):
                return existing, False
            cap = CallCapture(params, self.new_call_dir(params['call_id']), self.defaults)
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
        """Calls currently IN PROGRESS (not finished)."""
        with self.lock:
            return [c.brief() for c in self.calls.values()
                    if c.status in ('registered', 'capturing', 'finalizing')]

    def list_finalized(self, limit=100):
        """Most-recently FINISHED calls, newest first, read from disk.

        A finished call has a summary.json written by finalize(); we read it
        (plus meta.json for the registration params) and shape each one like
        brief() so the finalized page can reuse the dashboard's row renderer."""
        try:
            summaries = sorted(self.log_dir.glob('*/*/summary.json'),
                               key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            summaries = []
        out = []
        for sp in summaries[:max(0, int(limit))]:
            try:
                s = json.load(open(sp))
            except Exception:
                continue
            try:
                m = json.load(open(sp.parent / 'meta.json'))
            except Exception:
                m = {}
            params = m.get('params', {}) if isinstance(m, dict) else {}
            webrtc = (s.get('legs') or {}).get('webrtc', {})
            client = webrtc.get('client') or '{}:{}'.format(
                params.get('client_ip'), params.get('client_port'))
            out.append({
                'call_id': s.get('call_id'),
                'status': 'finished',
                'started_at': m.get('started_at'),
                'ended_at': s.get('ended_at'),
                'duration_s': s.get('duration_s'),
                'media_types': s.get('media_types') or [],
                'client': client,
                'server_port': webrtc.get('janus_port') or params.get('server_port'),
                'mediaproxy': s.get('mediaproxy_ip'),
                'webrtc_in': s.get('in_total'), 'webrtc_out': s.get('out_total'),
                'rtp_out': s.get('rtp_out_total'), 'rtp_in': s.get('rtp_in_total'),
                'flows': [],
                'evaluation': s.get('evaluation'),
                'evaluation_text': s.get('evaluation_text'),
                'capture_error': s.get('capture_error'),
            })
        return out

    def prune(self, retention_days):
        if retention_days <= 0:
            return
        cutoff = time.time() - retention_days * 86400
        try:
            for day in self.log_dir.iterdir():
                if not day.is_dir():
                    continue
                for call in day.iterdir():
                    try:
                        if call.is_dir() and call.stat().st_mtime < cutoff:
                            shutil.rmtree(call, ignore_errors=True)
                    except Exception:
                        pass
                try:  # drop the per-day folder once it's empty
                    if day.is_dir() and not any(day.iterdir()):
                        day.rmdir()
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Janus Admin API monitor — auto start/stop captures on call start/end
# ---------------------------------------------------------------------------

class JanusMonitor(object):
    """Polls the Janus Admin API and drives captures automatically.

    Every poll it walks list_sessions -> list_handles -> handle_info, finds SIP
    plugin handles that have an ESTABLISHED call (a SIP Call-ID plus a selected
    ICE pair, i.e. media is flowing), and:
      - registers + starts a capture when a call first appears, and
      - stops it when the call is gone from Janus.
    So a capture begins the moment a call comes up in Janus and ends when it
    tears down — no external trigger needed.

    Uses the Admin HTTP API (request/response) on a short poll interval; Janus
    has no push channel on the admin API."""

    def __init__(self, registry, admin_url, admin_secret, poll_interval, verbose=False):
        self.registry = registry
        self.admin_url = admin_url
        self.admin_secret = admin_secret
        self.poll_interval = max(1, poll_interval)
        self.verbose = verbose
        self._stop = threading.Event()
        self._thread = None
        self._active = {}  # call_id -> params (the calls we currently capture)

    # -- admin transport --------------------------------------------------
    def _admin(self, body, timeout=4):
        return janus_admin_request(self.admin_url, self.admin_secret, body, timeout)

    def check(self):
        """One-shot connectivity + auth probe. Returns (ok, detail)."""
        try:
            data = self._admin({'janus': 'list_sessions'})
        except Exception as e:
            return False, 'connect failed: {}'.format(e)
        if data.get('janus') == 'success':
            return True, '{} active session(s)'.format(len(data.get('sessions', []) or []))
        err = data.get('error', {}) or {}
        return False, 'admin error {}: {}'.format(err.get('code'), err.get('reason'))

    # -- discovery --------------------------------------------------------
    def _scan(self):
        """Return {call_id: params} for every established SIP call in Janus."""
        active = {}
        sessions = self._admin({'janus': 'list_sessions'}).get('sessions', []) or []
        for sid in sessions:
            try:
                handles = self._admin({'janus': 'list_handles', 'session_id': sid}).get('handles', []) or []
            except Exception:
                continue
            for hid in handles:
                try:
                    info = self._admin({'janus': 'handle_info', 'session_id': sid,
                                        'handle_id': hid}).get('info', {}) or {}
                except Exception:
                    continue
                plugin = str(info.get('plugin', ''))
                if 'sip' not in plugin.lower():
                    continue
                call_id = find_call_id(info)
                pair = find_selected_pair(info)
                if self.verbose:
                    log_line('janus-monitor: session={} handle={} plugin={} call_id={} pair={}'.format(
                        sid, hid, plugin, call_id, pair))
                    log_line('janus-monitor: handle_info {}'.format(json.dumps(info)))
                if not call_id or not pair:
                    continue  # not an established media call yet
                local_ip, local_port, remote_ip, remote_port = pair
                params = {
                    'call_id': call_id,
                    'client_ip': remote_ip, 'client_port': remote_port,
                    'server_ip': local_ip, 'server_port': local_port,
                    'janus_session': sid, 'janus_handle': hid,
                    'media_types': find_media_types(info),
                }
                sip = find_sip_media(info)
                if sip:
                    params['mediaproxy_ip'] = sip['remote_ip']
                    params['sip_remote_port'] = sip['remote_port']
                active[call_id] = params
        return active

    def _poll_once(self):
        active = self._scan()
        for call_id, params in active.items():
            if call_id not in self._active:
                cap, created = self.registry.register(params)
                self._active[call_id] = params
                log_line('janus-monitor: call STARTED call_id={} {}:{} <-> server:{} -> capturing{}'.format(
                    call_id, params['client_ip'], params['client_port'], params['server_port'],
                    '' if created else ' (already registered)'))
        for call_id in list(self._active):
            if call_id not in active:
                cap = self.registry.stop(call_id)
                del self._active[call_id]
                log_line('janus-monitor: call ENDED call_id={} -> capture finalized in {}'.format(
                    call_id, cap.dir if cap else '?'))

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log_line('janus-monitor: poll error: {}'.format(e))
            self._stop.wait(self.poll_interval)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


class JanusEventReceiver(object):
    """Push-based call detection via the Janus Event Handler API.

    Janus' sample event handler (janus.eventhandler.sampleevh) POSTs events to
    our /janus-events endpoint; feed() consumes them. Per handle we accumulate
    the SIP Call-ID (from plugin events) and the selected ICE pair (from WebRTC
    events); once both are known a capture starts, and a hangup/detached event
    stops it. If an 'up' event arrives but the Call-ID or pair isn't in the
    event payload, we do a single handle_info admin lookup to fill the gap
    (so admin access is optional but recommended).

    No polling — captures react to Janus events as they happen."""

    def __init__(self, registry, admin_url=None, admin_secret=None, verbose=False):
        self.registry = registry
        self.admin_url = admin_url
        self.admin_secret = admin_secret
        self.verbose = verbose
        self.handles = {}   # handle_id -> state dict
        self.lock = threading.Lock()

    # In verbose mode only these event types are dumped (16 = WebRTC,
    # 64 = plugin/SIP); the rest (media stats, session, transport, ...) are
    # processed but not logged, to keep the output focused.
    _VERBOSE_TYPES = (16, 64)
    # SIP plugin events that aren't tied to a call (registration churn) — skip.
    _SKIP_PLUGIN_EVENTS = {'registering', 'registered', 'unregistering',
                           'unregistered', 'registration_failed'}

    def _log_event_verbose(self, ev):
        t = ev.get('type')
        hid = ev.get('handle_id')
        e = ev.get('event')
        if t == 64 and isinstance(e, dict):
            data = e.get('data')
            evname = data.get('event') if isinstance(data, dict) else None
            if evname in self._SKIP_PLUGIN_EVENTS:
                return
            log_line('janus-events: SIP plugin event (handle={}):\n{}'.format(
                hid, json.dumps(data, indent=2)))
        elif t == 16:
            log_line('janus-events: WebRTC event (handle={}): {}'.format(hid, json.dumps(e)))

    def feed(self, payload):
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if self.verbose and isinstance(ev, dict) and ev.get('type') in self._VERBOSE_TYPES:
                self._log_event_verbose(ev)
            try:
                self._handle_event(ev)
            except Exception as e:
                log_line('janus-events: error processing event: {}'.format(e))

    def _handle_event(self, ev):
        if not isinstance(ev, dict):
            return
        hid = ev.get('handle_id')
        sid = ev.get('session_id')
        if not hid:
            return  # session/core/transport events without a handle: ignore
        with self.lock:
            st = self.handles.setdefault(hid, {
                'call_id': None, 'pair': None, 'capturing': False,
                'looked_up': False, 'session': sid,
                'sip_streams': [], 'ua_local': None, 'ua_remote': None})
            if self._is_down(ev):
                self._stop(hid)
                return
            cid = find_call_id(ev)
            if cid:
                st['call_id'] = cid
            pair = find_selected_pair(ev)
            if pair:
                st['pair'] = pair
            # The Janus SIP plugin often carries the negotiated SIP SDP in its
            # plugin events (accepted/progress) — try to learn the downstream
            # (Janus<->MediaProxy) leg and media types straight from the event.
            mt = find_media_types(ev)
            if mt:
                st['media_types'] = mt
            # Downstream (Janus<->MediaProxy) endpoint: take it ONLY from
            # INCOMING SIP (the remote answer, e.g. the 200 OK). Our own
            # outgoing offer (sip-out/calling) carries the Janus host's own IP,
            # which must never be used as the capture target.
            e = ev.get('event')
            evname = (e.get('data', {}).get('event')
                      if isinstance(e, dict) and isinstance(e.get('data'), dict) else None)
            if evname == 'sip-in':
                # remote answer -> the MediaProxy endpoint(s) Janus sends RTP to.
                # Capture EVERY stream (audio + video) so the BPF watches all of
                # this call's port pairs, not just the first.
                streams = find_sip_media_all(ev)
                if streams:
                    st['sip_streams'] = streams
                    prim = next((s for s in streams if s['media'] == 'audio'), streams[0])
                    st['mediaproxy_ip'] = prim['remote_ip']
                    st['sip_remote_port'] = prim['remote_port']
                # remote party's SIP User-Agent / Server header (the far leg)
                ua = find_user_agent(ev)
                if ua:
                    st['ua_remote'] = ua
            elif evname in ('calling', 'sip-out'):
                # our outgoing offer -> Janus' own SIP-side RTP IP:port
                loc = find_sip_media(ev)
                if loc:
                    st['sip_local_ip'] = loc['remote_ip']
                    st['sip_local_port'] = loc['remote_port']
                # the INVITE carries X-Sylk-Session-Id (the Sylk client session id)
                ssid = find_sylk_session_id(ev)
                if ssid:
                    st['sylk_session_id'] = ssid
                # our own (Janus/Sylk) outgoing SIP User-Agent (the near leg)
                ua = find_user_agent(ev)
                if ua:
                    st['ua_local'] = ua
            # On the first 'up' signal, do one handle_info lookup (if admin is
            # reachable) to fill any gaps AND learn the media types + the
            # downstream SIP leg (MediaProxy endpoint).
            if self._is_up_hint(ev) and self.admin_url and not st['looked_up']:
                st['looked_up'] = True
                try:
                    info = janus_handle_info(self.admin_url, self.admin_secret, sid, hid)
                    if self.verbose:
                        log_line('janus-events: handle_info handle={} {}'.format(hid, json.dumps(info)))
                    st['call_id'] = st['call_id'] or find_call_id(info)
                    st['pair'] = st['pair'] or find_selected_pair(info)
                    st['media_types'] = find_media_types(info)
                    sip = find_sip_media(info)
                    if sip:
                        st['mediaproxy_ip'] = sip['remote_ip']
                        st['sip_remote_port'] = sip['remote_port']
                except Exception as e:
                    log_line('janus-events: handle_info lookup failed for handle {}: {}'.format(hid, e))
            if st['call_id'] and st['pair'] and not st['capturing']:
                self._start(hid, st)

    def _start(self, hid, st):
        local_ip, local_port, remote_ip, remote_port = st['pair']
        mp = st.get('mediaproxy_ip')
        if mp and mp == local_ip:
            mp = None  # that was our own SIP offer address, not the remote MediaProxy
        params = {
            'call_id': st['call_id'],
            'client_ip': remote_ip, 'client_port': remote_port,
            'server_ip': local_ip, 'server_port': local_port,
            'janus_session': st['session'], 'janus_handle': hid,
            'media_types': st.get('media_types') or [],
            'mediaproxy_ip': mp,
            'sip_remote_port': st.get('sip_remote_port') if mp else None,
            'sip_streams': st.get('sip_streams') or [],
            'sip_local_ip': st.get('sip_local_ip'),
            'sip_local_port': st.get('sip_local_port'),
            'sylk_session_id': st.get('sylk_session_id'),
            'user_agent_local': st.get('ua_local'),
            'user_agent_remote': st.get('ua_remote'),
        }
        self.registry.register(params)
        st['capturing'] = True
        log_line('janus-events: call STARTED call_id={} {}:{} <-> server:{} -> capturing'.format(
            st['call_id'], remote_ip, remote_port, local_port))

    def _stop(self, hid):
        st = self.handles.pop(hid, None)
        if st and st.get('capturing') and st.get('call_id'):
            cap = self.registry.stop(st['call_id'])
            log_line('janus-events: call ENDED call_id={} -> capture finalized in {}'.format(
                st['call_id'], cap.dir if cap else '?'))

    @staticmethod
    def _is_down(ev):
        e = ev.get('event', {})
        if isinstance(e, dict):
            if str(e.get('connection', '')).lower() in ('hangup', 'down'):
                return True
            if e.get('name') == 'detached':
                return True
            data = e.get('data', {})
            if isinstance(data, dict) and str(data.get('event', '')).lower() in (
                    'hangup', 'hangingup', 'bye', 'missed_call', 'registration_failed'):
                return True
        # type 2 = handle event; a 'detached' name means the handle is gone.
        if ev.get('type') == 2 and isinstance(e, dict) and e.get('name') == 'detached':
            return True
        return False

    @staticmethod
    def _is_up_hint(ev):
        t = ev.get('type')
        if t in (16, 32):  # 16 = WebRTC (ice/dtls/selected-pair/connection), 32 = media
            return True
        e = ev.get('event', {})
        if isinstance(e, dict):
            data = e.get('data', {})
            if isinstance(data, dict) and str(data.get('event', '')).lower() in (
                    'accepted', 'accepting', 'calling', 'incomingcall', 'updated'):
                return True
        return False


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

# Files allowed for direct artifact download (no path traversal).
_ARTIFACTS = {'meta.json', 'summary.json', 'samples.ndjson', 'events.log',
              'kernel_before.json', 'kernel_after.json', 'capture.pcap'}


# Static dashboard page. The JS polls /calls (in-progress calls only) and
# rewrites just the table body — no full-page reload, so it doesn't flash.
DASHBOARD_HTML = r'''<!doctype html><html><head><meta charset="utf-8">
<title>sylk-qos-server</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:1.2rem;color:#1b1b1b}
 h1{font-size:1.1rem;margin:0 0 .2rem}
 .sub{color:#666;font-size:.8rem;margin-bottom:.8rem}
 table{border-collapse:collapse;width:100%;font-size:.82rem}
 th,td{border:1px solid #ddd;padding:.32rem .5rem;text-align:left;vertical-align:top}
 th{background:#f4f4f4}
 .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.78rem}
 tr.ok{background:#eafbea} tr.bad{background:#fdecec} tr.neutral{background:#fffceb}
 td.ev{font-weight:600} .muted{color:#999}
 a{color:#0a58ca;text-decoration:none} a:hover{text-decoration:underline}
 #dot{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;background:#3c3;margin-right:.35rem;vertical-align:middle}
 #dot.stale{background:#c33}
</style></head><body>
<h1>sylk-qos-server __VERSION__ — calls in progress</h1>
<div class="sub"><span id="dot"></span><span id="count">loading…</span> &nbsp; ▲ packets in &nbsp; ▼ packets out &nbsp; live &nbsp; · &nbsp; <a id="finlink" href="/finalized">recent finalized calls &rarr;</a></div>
<table><thead><tr>
<th>Call-ID</th><th>Status</th><th>Dur</th><th>Media</th>
<th>WebRTC leg (client &#8644; Janus)</th><th>Downstream (Janus &#8644; MediaProxy)</th>
<th>Flows seen</th><th>Evaluation</th><th>Data</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
var token = new URLSearchParams(location.search).get('token') || '';
var q = token ? ('?token=' + encodeURIComponent(token)) : '';
document.getElementById('finlink').href = '/finalized' + q;
function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML;}
function rowHtml(c){
 var ev = c.evaluation || '';
 var cls = ev.indexOf('ok')===0 ? 'ok' : ((ev.indexOf('one-way')>=0 || ev.indexOf('no media')===0) ? 'bad' : 'neutral');
 var mp = c.mediaproxy ? (esc(c.mediaproxy)+' &nbsp; ▲'+c.rtp_out+' ▼'+c.rtp_in) : '<span class="muted">n/a</span>';
 var media = (c.media_types && c.media_types.length) ? c.media_types.join(', ') : '?';
 var flows = (c.flows && c.flows.length) ? c.flows.join(', ') : '—';
 var cid = encodeURIComponent(c.call_id);
 return '<tr class="'+cls+'">'
  +'<td class="mono">'+esc(c.call_id)+'</td><td>'+esc(c.status)+'</td><td>'+esc(c.duration_s)+'s</td>'
  +'<td>'+esc(media)+'</td>'
  +'<td class="mono">'+esc(c.client)+' ⇄ :'+esc(c.server_port)+' &nbsp; ▲'+c.webrtc_in+' ▼'+c.webrtc_out+'</td>'
  +'<td class="mono">'+mp+'</td>'
  +'<td>'+esc(flows)+'</td><td class="ev">'+esc(ev)+'</td>'
  +'<td class="mono"><a href="/call/'+cid+'">json</a> &nbsp;<a href="/call/'+cid+'/tar">tar</a></td></tr>';
}
function render(calls){
 calls.sort(function(a,b){return (b.started_at||'').localeCompare(a.started_at||'');});
 var tb = document.getElementById('rows');
 tb.innerHTML = calls.length ? calls.map(rowHtml).join('') : '<tr><td colspan="9" class="muted">no calls in progress</td></tr>';
 document.getElementById('count').textContent = calls.length + ' call(s) in progress';
 document.getElementById('dot').className = '';
}
function refresh(){
 fetch('/calls'+q, {headers:{'Accept':'application/json'}})
  .then(function(r){ if(!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(function(){ document.getElementById('dot').className='stale'; });
}
refresh(); setInterval(refresh, 2000);
</script>
</body></html>'''


# Recent finalized calls. The JS fetches /finalized (JSON) and renders the
# last N finished calls read from disk. Same look as the live dashboard.
FINALIZED_HTML = r'''<!doctype html><html><head><meta charset="utf-8">
<title>sylk-qos-server — finalized calls</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:1.2rem;color:#1b1b1b}
 h1{font-size:1.1rem;margin:0 0 .2rem}
 .sub{color:#666;font-size:.8rem;margin-bottom:.8rem}
 table{border-collapse:collapse;width:100%;font-size:.82rem}
 th,td{border:1px solid #ddd;padding:.32rem .5rem;text-align:left;vertical-align:top}
 th{background:#f4f4f4}
 .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.78rem}
 tr.ok{background:#eafbea} tr.bad{background:#fdecec} tr.neutral{background:#fffceb}
 td.ev{font-weight:600} .muted{color:#999}
 a{color:#0a58ca;text-decoration:none} a:hover{text-decoration:underline}
 #dot{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;background:#3c3;margin-right:.35rem;vertical-align:middle}
 #dot.stale{background:#c33}
</style></head><body>
<h1>sylk-qos-server __VERSION__ — recent finalized calls</h1>
<div class="sub"><span id="dot"></span><span id="count">loading…</span> &nbsp; · &nbsp; <a id="backlink" href="/">&larr; calls in progress</a></div>
<table><thead><tr>
<th>Call-ID</th><th>Status</th><th>Ended</th><th>Dur</th><th>Media</th>
<th>WebRTC leg (client &#8644; Janus)</th><th>Downstream (Janus &#8644; MediaProxy)</th>
<th>Evaluation</th><th>Data</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
var token = new URLSearchParams(location.search).get('token') || '';
var q = token ? ('?token=' + encodeURIComponent(token)) : '';
document.getElementById('backlink').href = '/' + q;
function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML;}
function evClass(ev){ ev = ev || '';
 if(ev.indexOf('ok')===0) return 'ok';
 if(ev.indexOf('one-way')>=0 || ev.indexOf('no-media')===0 || ev.indexOf('broken')===0 || ev.indexOf('bad')===0) return 'bad';
 return 'neutral'; }
function rowHtml(c){
 var cls = evClass(c.evaluation);
 var mp = c.mediaproxy ? (esc(c.mediaproxy)+' &nbsp; ▲'+c.rtp_out+' ▼'+c.rtp_in) : '<span class="muted">n/a</span>';
 var media = (c.media_types && c.media_types.length) ? c.media_types.join(', ') : '?';
 var cid = encodeURIComponent(c.call_id);
 var ended = c.ended_at ? esc(c.ended_at).replace('T',' ').replace(/\..*$/,'') : '—';
 return '<tr class="'+cls+'">'
  +'<td class="mono">'+esc(c.call_id)+'</td><td>'+esc(c.status)+'</td><td class="mono">'+ended+'</td>'
  +'<td>'+esc(c.duration_s)+'s</td><td>'+esc(media)+'</td>'
  +'<td class="mono">'+esc(c.client)+' ⇄ :'+esc(c.server_port)+' &nbsp; ▲'+c.webrtc_in+' ▼'+c.webrtc_out+'</td>'
  +'<td class="mono">'+mp+'</td>'
  +'<td class="ev">'+esc(c.evaluation_text || c.evaluation)+'</td>'
  +'<td class="mono"><a href="/call/'+cid+'">json</a> &nbsp;<a href="/call/'+cid+'/tar">tar</a></td></tr>';
}
function render(calls){
 var tb = document.getElementById('rows');
 tb.innerHTML = calls.length ? calls.map(rowHtml).join('') : '<tr><td colspan="9" class="muted">no finalized calls on disk</td></tr>';
 document.getElementById('count').textContent = calls.length + ' finalized call(s)';
 document.getElementById('dot').className = '';
}
function refresh(){
 fetch('/finalized'+q, {headers:{'Accept':'application/json'}})
  .then(function(r){ if(!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(function(){ document.getElementById('dot').className='stale'; });
}
refresh(); setInterval(refresh, 10000);
</script>
</body></html>'''


def make_handler(registry, token, event_receiver=None):
    state = {'first_push': True}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'sylk-qos-server/' + VERSION
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass  # silence default stderr logging; we log via log_request below

        def log_request(self, code='-', size='-'):
            # Log every served HTTP request (client, method, path, status) so
            # it's visible who fetched what and when — e.g. a web/app client
            # pulling a call's qos summary. Skip the Janus event-push sink:
            # Janus POSTs to it constantly and those lines are pure noise.
            try:
                path = urlparse(self.path).path
            except Exception:
                path = self.path
            # Skip noisy poll endpoints: the Janus event-push sink, and the
            # dashboard's active-call listing (GET /calls with no call id, which
            # the dashboard JS polls continuously). Per-call fetches
            # (/calls/<id>, /call/<id>/...) are still logged.
            if path == '/janus-events' or path.rstrip('/') in ('/calls', '/call'):
                return
            try:
                code = code.value if hasattr(code, 'value') else code
            except Exception:
                pass
            log_line('http {} "{} {}" -> {}'.format(
                self.client_address[0], self.command, self.path, code))

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
            # ensure_ascii=False so the em-dash (—) and other non-ASCII stay as
            # real UTF-8 characters instead of "—" escapes in the output.
            pretty = json.dumps(obj, indent=2, default=str, ensure_ascii=False)
            # Browsers (Accept: text/html) get readable, syntax-friendly HTML;
            # API clients (curl, qos-probe) get plain JSON.
            if 'text/html' in self.headers.get('Accept', ''):
                tq = parse_qs(urlparse(self.path).query).get('token', [''])[0]
                back = '/?token=' + tq if tq else '/'
                body = ('<!doctype html><meta charset="utf-8"><title>sylk-qos-server</title>'
                        '<body style="margin:1rem;font-family:ui-monospace,Menlo,Consolas,monospace">'
                        '<a href="{}">&larr; dashboard</a>'
                        '<pre style="font-size:13px;white-space:pre-wrap;word-break:break-word">{}</pre>'
                        '</body>'.format(_h(back), _h(pretty))).encode()
                ctype = 'text/html; charset=utf-8'
            else:
                body = pretty.encode()
                ctype = 'application/json'
            self.send_response(code)
            self.send_header('Content-Type', ctype)
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
            parts = [unquote(p) for p in path.strip('/').split('/')]
            # Single-call endpoints. The call id / session id is a long random
            # capability, so these do NOT require the token. /call/{id} and
            # /calls/{id} both resolve by SIP Call-ID or Sylk session id.
            if len(parts) >= 2 and parts[0] in ('call', 'calls'):
                ident = parts[1]
                client = self.client_address[0]
                if len(parts) == 2:
                    log_line('qos client {} requested call data (manifest) for {}'.format(client, ident))
                    return self._manifest(ident)         # meta + summary + artifact list
                sub = parts[2]
                if sub in ('bundle', 'tar', 'archive'):
                    log_line('qos client {} requested bundle for {}'.format(client, ident))
                    return self._bundle(ident)           # whole call folder as .tar.gz
                if sub in ('summary', 'summary.json'):
                    log_line('qos client {} requested summary for {}'.format(client, ident))
                    return self._artifact(ident, 'summary.json')   # just the summary JSON
                if sub in _ARTIFACTS:
                    log_line('qos client {} requested {} for {}'.format(client, sub, ident))
                    return self._artifact(ident, sub)    # any raw artifact (pcap, ndjson, ...)
                return self._err(404, 'unknown sub-resource')
            # Listing and dashboard expose all calls -> require the token.
            if not self._authorized():
                return self._err(401, 'unauthorized')
            if path in ('/', '/ui', '/dashboard'):
                return self._dashboard()
            if path == '/calls':
                return self._json(200, registry.list_active())
            if path in ('/finalized', '/recent'):
                # Browsers get the page; the page's fetch (Accept: json) and API
                # clients get the JSON list of recently finished calls.
                if 'text/html' in self.headers.get('Accept', ''):
                    return self._finalized_page()
                try:
                    limit = int(parse_qs(urlparse(self.path).query).get('limit', ['100'])[0])
                except (TypeError, ValueError):
                    limit = 100
                return self._json(200, registry.list_finalized(limit))
            return self._err(404, 'not found')

        def do_POST(self):
            path = urlparse(self.path).path
            # Janus event-handler push sink. NOT behind the bearer token — the
            # Janus sample event handler can't send one; keep this endpoint on
            # localhost / firewalled. Only present when event mode is enabled.
            if path == '/janus-events' and event_receiver is not None:
                if state['first_push']:
                    state['first_push'] = False
                    log_line('janus-events: first push received from {} — Janus is '
                             'connected to our event socket'.format(self.client_address[0]))
                body = self._read_body()
                if body is None:
                    return self._err(400, 'invalid JSON body')
                event_receiver.feed(body)
                return self._json(200, {'ok': True})
            if not self._authorized():
                return self._err(401, 'unauthorized')
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

        def _html(self, code, html):
            body = html.encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dashboard(self):
            # Static page; the JS fetches /calls and updates the table in place
            # (no full-page reload / flashing).
            return self._html(200, DASHBOARD_HTML.replace('__VERSION__', _h(VERSION)))

        def _finalized_page(self):
            # Static page; the JS fetches /finalized (JSON) and lists the most
            # recently finished calls read from disk.
            return self._html(200, FINALIZED_HTML.replace('__VERSION__', _h(VERSION)))

        def _manifest(self, ident):
            d = registry.resolve_dir(ident)
            if not d or not d.is_dir():
                return self._err(404, 'unknown call id / session id')
            cap = registry.get(ident)
            summary = _load(d / 'summary.json')
            return self._json(200, {
                'call_id': ident,
                'status': cap.status if cap else ('finished' if summary else 'unknown'),
                'meta': _load(d / 'meta.json'),
                'summary': summary,
            })

        def _artifact(self, ident, name):
            d = registry.resolve_dir(ident)
            fp = (d / name) if d else None
            if not fp or not fp.is_file():
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

        def _bundle(self, ident):
            d = registry.resolve_dir(ident)
            if not d or not d.is_dir():
                return self._err(404, 'unknown call id / session id')
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                tar.add(str(d), arcname=d.name)
            data = buf.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition',
                             'attachment; filename="{}.tar.gz"'.format(d.name))
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
    p.add_argument('-v', '--verbose', action='count', default=0,
                   help='verbose logging — logs every event/handle received from Janus')
    args = p.parse_args()
    verbose = args.verbose > 0

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
        'expected_pps': cfg['expected_pps'],
        'sample_interval': cfg['sample_interval'],
        'max_capture_seconds': cfg['max_capture_seconds'],
        'keep_pcap': cfg['keep_pcap'],
    }
    registry = Registry(log_dir, defaults)
    registry.prune(cfg['retention_days'])

    # Background retention sweep, once a day.
    def retention_loop():
        while True:
            time.sleep(86400)
            registry.prune(cfg['retention_days'])
    threading.Thread(target=retention_loop, daemon=True).start()

    # Auto-capture: either Janus pushes events to us ('events') or we poll the
    # admin API ('poll').
    monitor = None
    event_receiver = None
    if cfg['janus_mode'] == 'poll':
        monitor = JanusMonitor(registry, cfg['janus_admin_url'],
                               cfg['janus_admin_secret'], cfg['janus_poll_interval'],
                               verbose=verbose)
    elif cfg['janus_mode'] == 'events':
        event_receiver = JanusEventReceiver(registry, cfg['janus_admin_url'] or None,
                                            cfg['janus_admin_secret'], verbose=verbose)

    httpd = ThreadingHTTPServer((host, port), make_handler(registry, cfg['auth_token'], event_receiver))

    # HTTPS using SylkServer's webrtcgateway TLS (config.ini [WebServer]).
    scheme = 'http'
    if cfg['tls_certificate']:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            if cfg['tls_certificate_chain']:
                # chain file holds cert+chain; the private key is in the cert file
                ctx.load_cert_chain(certfile=cfg['tls_certificate_chain'],
                                    keyfile=cfg['tls_certificate'])
            else:
                # cert file holds the certificate AND the private key
                ctx.load_cert_chain(certfile=cfg['tls_certificate'])
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = 'https'
        except Exception as e:
            log_line('TLS: failed to load certificate {} ({}) — falling back to HTTP'.format(
                cfg['tls_certificate'], e))

    # Plain-HTTP loopback listener for the co-located Janus (internal comms):
    # the public server is TLS for the internet, but Janus on this same host
    # pushes events over http://127.0.0.1 (no cert-CN mismatch, never leaves
    # the box). Skipped if it would collide with the main HTTP loopback bind.
    local_httpd = None
    lp = cfg['local_http_port']
    if lp and not (scheme == 'http' and host in ('127.0.0.1', '0.0.0.0', '::', '::1') and lp == port):
        try:
            local_httpd = ThreadingHTTPServer(('127.0.0.1', lp),
                                              make_handler(registry, cfg['auth_token'], event_receiver))
            threading.Thread(target=local_httpd.serve_forever, daemon=True).start()
        except Exception as e:
            log_line('local HTTP (for Janus on localhost) failed to bind 127.0.0.1:{}: {}'.format(lp, e))
            local_httpd = None

    def _do_shutdown():
        if monitor is not None:
            monitor.stop()
        if local_httpd is not None:
            try:
                local_httpd.shutdown()
            except Exception:
                pass
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

    logline = log_line

    startup = [
        ('version', VERSION),
        ('config_dir', cfg['config_dir']),
        ('config_files', ', '.join(cfg['config_files']) or '(none found, using defaults)'),
        ('listen', '{}:{} ({})'.format(host, port, scheme.upper())),
        ('tls', '{} (cert {}{})'.format(scheme == 'https' and 'on' or 'off',
                                        cfg['tls_certificate'] or '-',
                                        ', hostname ' + cfg['tls_hostname'] if cfg['tls_hostname'] else '')),
        ('auth', 'token' if cfg['auth_token'] else 'localhost-only (no auth_token set)'),
        ('trace_dir', cfg['trace_dir']),
        ('log_dir', str(log_dir)),
        ('interface', defaults['interface']),
        ('expected_pps', defaults['expected_pps']),
        ('sample_interval', '{}s'.format(defaults['sample_interval'])),
        ('max_capture_seconds', defaults['max_capture_seconds']),
        ('retention_days', cfg['retention_days']),
        ('keep_pcap', cfg['keep_pcap'] + {'no': ' (never write pcap — just count)',
                                          'problem': ' (keep pcap only for calls with a media problem)',
                                          'yes': ' (always keep pcap)'}[cfg['keep_pcap']]),
        ('janus_config_dir', cfg['janus_config_dir']),
        ('janus_mode', cfg['janus_mode'] + (' (push)' if cfg['janus_mode'] == 'events'
                                            else ' (admin polling)' if cfg['janus_mode'] == 'poll' else '')),
        ('janus_admin_url', ('{} (from {})'.format(cfg['janus_admin_url'], cfg['janus_admin_url_source'])
                             if cfg['janus_mode'] != 'off' else '-')),
        ('janus_admin_secret', ('set (from {})'.format(cfg['janus_admin_secret_source'])
                                if cfg['janus_admin_secret'] else 'NOT SET ({})'.format(
                                    cfg['janus_admin_secret_source'] or 'n/a'))
                               if cfg['janus_mode'] != 'off' else '-'),
        ('janus_events_backend', ('http://127.0.0.1:{}/janus-events  <- set this as `backend` in '
                                  'janus.eventhandler.sampleevh.jcfg'.format(local_httpd.server_address[1])
                                  if local_httpd else 'off (events mode disabled)')),
        ('tcpdump', 'available' if tcpdump_available() else 'MISSING (metadata-only captures)'),
    ]
    logline('starting — effective configuration:')
    width = max(len(k) for k, _ in startup)
    for key, value in startup:
        logline('  {:<{w}} = {}'.format(key, value, w=width))
    if verbose:
        logline('verbose mode ON — logging every event/handle received from Janus')

    _src_ip, _gw_ip, _ = default_route()
    # Prefer the cert hostname for HTTPS (so it matches the cert CN).
    _hosthint = (cfg['tls_hostname'] if scheme == 'https' and cfg['tls_hostname']
                 else (_src_ip or ('127.0.0.1' if host in ('0.0.0.0', '::') else host)))
    _tok = '?token=YOUR_TOKEN' if cfg['auth_token'] else ''
    logline('web dashboard: {}://{}:{}/{}  (default gateway {})  — live table '
            'of monitored calls; also GET /calls for JSON'.format(
                scheme, _hosthint, port, _tok, _gw_ip or '?'))

    # Poll mode: connect to the Janus Admin API, report success/failure, start.
    if monitor is not None:
        ok, detail = monitor.check()
        if ok:
            logline('janus-monitor: CONNECTED to {} OK — {} (polling every {}s for '
                    'call start/stop)'.format(cfg['janus_admin_url'], detail, cfg['janus_poll_interval']))
            monitor.start()
        else:
            logline('janus-monitor: could NOT connect to Janus admin {} — {}. '
                    'Auto-capture disabled; calls can still be registered via the '
                    'HTTP API. (check admin_http in janus.transport.http.jcfg and '
                    'admin_secret)'.format(cfg['janus_admin_url'], detail))

    # Events mode: wait for Janus to push events to /janus-events. Janus is
    # co-located, so it should post to the plain-HTTP loopback endpoint.
    if event_receiver is not None:
        if local_httpd is not None:
            events_url = 'http://127.0.0.1:{}/janus-events'.format(local_httpd.server_address[1])
        else:
            events_url = '{}://{}:{}/janus-events'.format(
                scheme, '127.0.0.1' if host in ('0.0.0.0', '::') else host, port)
        logline('janus-events: listening for Janus event pushes — point '
                'janus.eventhandler.sampleevh.jcfg `backend` at {} (events: '
                'webrtc, plugins; see janus.eventhandler.sampleevh.jcfg.sample)'.format(events_url))
        if cfg['janus_admin_url'] and cfg['janus_admin_secret']:
            try:
                janus_admin_request(cfg['janus_admin_url'], cfg['janus_admin_secret'], {'janus': 'ping'})
                logline('janus-events: admin API {} reachable (used to fill 5-tuple '
                        'gaps)'.format(cfg['janus_admin_url']))
            except Exception as e:
                logline('janus-events: admin API {} not reachable ({}) — relying purely '
                        'on event payloads for the 5-tuple'.format(cfg['janus_admin_url'], e))

    logline('ready, accepting requests')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown()


if __name__ == '__main__':
    main()
