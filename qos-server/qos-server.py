#!/usr/bin/env python3
"""
qos-server.py — long-running server-side QoS diagnostic.

Launched over SSH by qos-probe.py when a call starts. Runs for the
lifetime of the call (no fixed duration); the SSH session closing on
call end fires SIGHUP, which triggers a final summary and conclusion.

For the lifetime of the process:
  - Snapshots /proc/net/dev (NIC counters), /proc/net/snmp UDP counters,
    and nf_conntrack_count at start.
  - Starts tcpdump in line-streaming mode (no pcap), filtered to the
    call's exact 5-tuple. Counts client→server and server→client
    packets in real time.
  - Every --sample-interval seconds (default 5 s) emits a single
    `[qos-server] sample t=Ns ...` line with running totals and pps.
  - On SIGHUP / SIGTERM / SIGINT / stdin EOF: emits a final summary +
    conclusion line (`[qos-server] conclusion: ...`) and exits.

Output (all lines prefixed with `[qos-server]`):

    [qos-server] start client=... server_port=... iface=eth0
    [qos-server] before nic.rx_packets=... nic.rx_drop=... udp.InErrors=...
    [qos-server] tcpdump iface=eth0 filter='...'
    [qos-server] sample t=5s in=247 (+247, 49pps) out=12 (+12, 2pps)
    [qos-server] sample t=10s in=494 (+247, 49pps) out=24 (+12, 2pps)
    ...
    [qos-server] end duration=27.3s in_total=1340 out_total=66 expected~1365 nic_loss=1.8%
    [qos-server] kernel_delta rx_drop=+0 udp_in_err=+0 udp_rcvbuf=+0 conntrack=+0
    [qos-server] conclusion: NIC received 1340 of ~1365 packets cleanly; loss (if any) is downstream of NIC

Prerequisites on the server:
  - tcpdump (CAP_NET_RAW, or passwordless sudo for tcpdump)
  - Python 3
"""
import argparse
import datetime
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def now_str():
    return datetime.datetime.now().strftime('%H:%M:%S')


def log(msg):
    sys.stdout.write(f"{now_str()} [qos-server] {msg}\n")
    sys.stdout.flush()


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
                    'rx_bytes':   int(parts[0]),
                    'rx_packets': int(parts[1]),
                    'rx_errs':    int(parts[2]),
                    'rx_drop':    int(parts[3]),
                    'rx_fifo':    int(parts[4]),
                    'rx_frame':   int(parts[5]),
                    'tx_bytes':   int(parts[8]),
                    'tx_packets': int(parts[9]),
                    'tx_errs':    int(parts[10]),
                    'tx_drop':    int(parts[11]),
                }
    except Exception as e:
        log(f"read_proc_net_dev({iface}) failed: {e}")
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
    except Exception as e:
        log(f"read_udp_counters failed: {e}")
    return {}


def read_conntrack_count():
    try:
        with open('/proc/sys/net/netfilter/nf_conntrack_count') as f:
            return int(f.read().strip())
    except Exception:
        return None


def primary_interface():
    try:
        out = subprocess.check_output(['ip', '-o', '-4', 'route', 'show', 'default'],
                                      text=True)
        toks = out.split()
        for i, t in enumerate(toks):
            if t == 'dev' and i + 1 < len(toks):
                return toks[i + 1]
    except Exception:
        pass
    return 'eth0'


def have_passwordless_sudo():
    try:
        r = subprocess.run(['sudo', '-n', 'true'], capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--client-ip', required=True)
    p.add_argument('--client-port', type=int, required=True)
    p.add_argument('--server-port', type=int, required=True)
    p.add_argument('--interface', default=None,
                   help='NIC to capture on (default: default-route interface)')
    p.add_argument('--expected-pps', type=int, default=50,
                   help='Expected client-side packets/sec (default: 50, Opus 20ms)')
    p.add_argument('--sample-interval', type=int, default=5,
                   help='Emit a sample line every N seconds (default: 5)')
    # Optional second leg: the plain-RTP hop between this Janus host and
    # MediaProxy. When --mediaproxy-ip is given, we ALSO count packets to/from
    # MediaProxy at this NIC, so the summary can localize a break to the
    # WebRTC leg (phone<->Janus) vs the RTP leg (Janus<->MediaProxy) — i.e.
    # which side of MediaProxy a one-way call dies on.
    p.add_argument('--mediaproxy-ip', default=None,
                   help='MediaProxy IP; also count the Janus<->MediaProxy RTP leg seen at this NIC')
    p.add_argument('--rtp-port-min', type=int, default=None,
                   help='Lower bound of the RTP port range toward MediaProxy (narrows the capture)')
    p.add_argument('--rtp-port-max', type=int, default=None,
                   help='Upper bound of the RTP port range toward MediaProxy')
    # Accepted but ignored — retained for compatibility with older qos-probe.py
    p.add_argument('--duration', type=int, default=0, help=argparse.SUPPRESS)
    args = p.parse_args()

    iface = args.interface or primary_interface()

    log(f"start client={args.client_ip}:{args.client_port} "
        f"server_port={args.server_port} iface={iface} "
        f"sample_interval={args.sample_interval}s")

    # Before snapshots
    dev_before = read_proc_net_dev(iface)
    udp_before = read_udp_counters()
    ct_before = read_conntrack_count()
    log(f"before "
        f"nic.rx_packets={dev_before['rx_packets'] if dev_before else '?'} "
        f"nic.rx_drop={dev_before['rx_drop'] if dev_before else '?'} "
        f"udp.InErrors={udp_before.get('InErrors', '?')} "
        f"udp.RcvbufErrors={udp_before.get('RcvbufErrors', '?')} "
        f"conntrack={ct_before}")

    bpf = (f'(src host {args.client_ip} and src port {args.client_port} and '
           f'dst port {args.server_port}) or '
           f'(dst host {args.client_ip} and dst port {args.client_port} and '
           f'src port {args.server_port})')

    # Add the Janus<->MediaProxy RTP leg to the same capture when asked.
    if args.mediaproxy_ip:
        if args.rtp_port_min and args.rtp_port_max:
            mp_clause = (f'(host {args.mediaproxy_ip} and udp portrange '
                         f'{args.rtp_port_min}-{args.rtp_port_max})')
        else:
            mp_clause = f'(host {args.mediaproxy_ip} and udp)'
        bpf = f'({bpf}) or {mp_clause}'

    base_cmd = ['tcpdump', '-i', iface, '-n', '-l', '-q', bpf]
    if os.geteuid() != 0 and have_passwordless_sudo():
        cmd = ['sudo', '-n'] + base_cmd
    else:
        cmd = base_cmd

    log(f"tcpdump iface={iface} filter='{bpf}'")

    try:
        tcpdump_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        log("ERROR: tcpdump not found in PATH")
        sys.exit(1)
    except PermissionError:
        log("ERROR: tcpdump permission denied — needs root or CAP_NET_RAW")
        sys.exit(1)

    in_count = [0]
    out_count = [0]
    # Janus<->MediaProxy RTP leg counters (only used when --mediaproxy-ip set):
    #   rtp_out = Janus -> MediaProxy   (RTP we forward toward the SIP side)
    #   rtp_in  = MediaProxy -> Janus   (RTP coming back from the SIP side)
    rtp_out_count = [0]
    rtp_in_count = [0]
    shutdown_event = threading.Event()
    start_time = time.time()

    # tcpdump -q output line example:
    # 14:32:45.123456 IP 86.127.76.54.42751 > 174.142.205.47.42043: UDP, length 160
    client_src_tag = f"{args.client_ip}.{args.client_port}"
    mp_ip = args.mediaproxy_ip

    def packet_reader():
        for line in tcpdump_proc.stdout:
            if shutdown_event.is_set():
                break
            # WebRTC leg: look for the client IP+port as src or dst.
            if f"{client_src_tag} >" in line:
                in_count[0] += 1
            elif f"> {client_src_tag}" in line:
                out_count[0] += 1
            # RTP leg: classify by MediaProxy IP as src/dst host.
            if mp_ip and ' IP ' in line and ' > ' in line:
                try:
                    seg = line.split(' IP ', 1)[1]
                    src, rest = seg.split(' > ', 1)
                    dst = rest.split(':', 1)[0]
                    src_host = src.strip().rsplit('.', 1)[0]
                    dst_host = dst.strip().rsplit('.', 1)[0]
                    if dst_host == mp_ip:
                        rtp_out_count[0] += 1   # Janus -> MediaProxy
                    elif src_host == mp_ip:
                        rtp_in_count[0] += 1    # MediaProxy -> Janus
                except Exception:
                    pass

    threading.Thread(target=packet_reader, daemon=True).start()

    def sample_reporter():
        last_in = 0
        last_out = 0
        last_t = start_time
        while not shutdown_event.is_set():
            # Sleep in 1-second chunks so we react quickly to shutdown.
            for _ in range(args.sample_interval):
                if shutdown_event.is_set():
                    return
                time.sleep(1)
            now = time.time()
            elapsed = now - last_t
            in_d = in_count[0] - last_in
            out_d = out_count[0] - last_out
            in_pps = in_d / elapsed if elapsed > 0 else 0
            out_pps = out_d / elapsed if elapsed > 0 else 0
            rtp_tail = ''
            if mp_ip:
                rtp_tail = (f" rtp_out={rtp_out_count[0]} rtp_in={rtp_in_count[0]}")
            log(f"sample t={int(now - start_time)}s "
                f"in={in_count[0]} (+{in_d}, {in_pps:.0f}pps) "
                f"out={out_count[0]} (+{out_d}, {out_pps:.0f}pps)" + rtp_tail)
            last_in = in_count[0]
            last_out = out_count[0]
            last_t = now

    threading.Thread(target=sample_reporter, daemon=True).start()

    def stdin_watcher():
        # When the SSH session closes (call ended), our stdin gets EOF.
        # Treat that as a shutdown signal so we emit the final summary.
        try:
            while sys.stdin.readable():
                if not sys.stdin.read(1):
                    break
        except Exception:
            pass
        if not shutdown_event.is_set():
            os.kill(os.getpid(), signal.SIGHUP)

    threading.Thread(target=stdin_watcher, daemon=True).start()

    def emit_final_summary():
        try:
            tcpdump_proc.terminate()
            tcpdump_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try: tcpdump_proc.kill()
            except Exception: pass
        except Exception:
            pass

        dev_after = read_proc_net_dev(iface)
        udp_after = read_udp_counters()
        ct_after = read_conntrack_count()

        duration = time.time() - start_time
        expected = max(0, int(args.expected_pps * duration))
        nic_loss_pct = 0.0
        if expected > 0 and in_count[0] <= expected:
            nic_loss_pct = 100.0 * (1.0 - in_count[0] / expected)

        log(f"end duration={duration:.1f}s "
            f"in_total={in_count[0]} out_total={out_count[0]} "
            f"expected~{expected} nic_loss={nic_loss_pct:.1f}%")

        rx_drop_delta = ((dev_after['rx_drop'] - dev_before['rx_drop'])
                         if dev_before and dev_after else None)
        udp_inerr_delta = ((udp_after.get('InErrors', 0) - udp_before.get('InErrors', 0))
                           if udp_before and udp_after else None)
        udp_rcvbuf_delta = ((udp_after.get('RcvbufErrors', 0) - udp_before.get('RcvbufErrors', 0))
                            if udp_before and udp_after else None)
        ct_delta = ((ct_after or 0) - (ct_before or 0)
                    if ct_before is not None and ct_after is not None else None)

        log(f"kernel_delta "
            f"rx_drop={'+'+str(rx_drop_delta) if rx_drop_delta is not None else '?'} "
            f"udp_in_err={'+'+str(udp_inerr_delta) if udp_inerr_delta is not None else '?'} "
            f"udp_rcvbuf={'+'+str(udp_rcvbuf_delta) if udp_rcvbuf_delta is not None else '?'} "
            f"conntrack={ct_delta:+d}" if ct_delta is not None else f"conntrack=?")

        kernel_dropped = sum(d for d in (rx_drop_delta, udp_inerr_delta, udp_rcvbuf_delta)
                             if d is not None)
        if in_count[0] == 0:
            conclusion = ("NO packets captured for this 5-tuple — verify "
                          "client-ip/port (NAT may rewrite) and interface")
        elif nic_loss_pct < 5.0 and (kernel_dropped is None or kernel_dropped < 5):
            conclusion = (f"NIC received {in_count[0]} of ~{expected} packets "
                          "cleanly — loss (if any) is DOWNSTREAM of the NIC "
                          "(Janus, kernel→userspace, or server→client path)")
        elif kernel_dropped and kernel_dropped > 5:
            conclusion = (f"NIC received packets but kernel discarded "
                          f"{kernel_dropped} (rp_filter, IPset, conntrack, "
                          "socket overflow)")
        else:
            missing = max(0, expected - in_count[0])
            conclusion = (f"{nic_loss_pct:.1f}% of expected packets "
                          f"({missing}/{expected}) did not reach the NIC — "
                          "drop is UPSTREAM of this host (network path, DC "
                          "firewall, hypervisor)")
        log(f"conclusion: {conclusion}")

        # Media-plane localization across both legs seen at this NIC:
        #   phone --in--> Janus --rtp_out--> MediaProxy --rtp_in--> Janus --out--> phone
        # The first leg whose count collapses to ~0 in a given direction is
        # where that direction breaks, relative to MediaProxy.
        if mp_ip:
            log(f"rtp_leg janus->mediaproxy={rtp_out_count[0]} "
                f"mediaproxy->janus={rtp_in_count[0]} (mediaproxy={mp_ip})")
            wi, wo = in_count[0], out_count[0]
            ro, ri = rtp_out_count[0], rtp_in_count[0]
            if wi > 0 and ro == 0:
                mp_conc = ("OUTBOUND BREAK Janus->MediaProxy — phone RTP reaches "
                           "Janus but Janus is not forwarding it to MediaProxy "
                           "(SIP-leg SDP/port, srtp, or Janus relay)")
            elif ri > 0 and wo == 0:
                mp_conc = ("INBOUND BREAK Janus->phone — RTP returns from "
                           "MediaProxy to Janus but Janus is not relaying it to "
                           "the phone (DTLS/SRTP to phone, transceiver)")
            elif wi > 0 and ro > 0 and ri == 0:
                mp_conc = ("INBOUND BREAK at/after MediaProxy — Janus forwards to "
                           "MediaProxy but nothing comes back (far leg, far phone, "
                           "or MediaProxy not relaying)")
            elif wi > 0 and wo > 0 and ro > 0 and ri > 0:
                mp_conc = "both legs carrying RTP in both directions — media plane intact at this host"
            else:
                mp_conc = ("inconclusive — partial counts; compare against the "
                           "phone's [qos] VERDICT and the far leg's capture")
            log(f"media_plane: {mp_conc}")

    def shutdown_handler(*_):
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        try:
            emit_final_summary()
        finally:
            # Give SSH a moment to drain our final lines before exit.
            sys.stdout.flush()
            time.sleep(0.3)
            os._exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGHUP, shutdown_handler)

    # Block forever — the threads do the work; shutdown_handler exits.
    while not shutdown_event.is_set():
        time.sleep(1)


if __name__ == '__main__':
    main()
