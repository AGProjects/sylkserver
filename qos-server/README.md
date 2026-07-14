# sylk-qos-server — QoS capture server

Long-running daemon on the WebRTC/Janus host. Clients drive it over a small
HTTP API: they **register** a call (which starts a packet capture) and later
**fetch** everything learned about that call, keyed by SIP Call-ID. No SSH.

Everything per call is persisted under `<trace_dir>/qos/<call-id>/` so a call
can be reconstructed afterwards:

```
<trace_dir>/qos/<call-id>/
  meta.json          registration params, 5-tuples, interface, host, timestamps
  capture.pcap       raw packet capture of the call's tuples (open in Wireshark)
  samples.ndjson     per-interval counter timeline (one JSON object per line)
  kernel_before.json NIC / UDP / conntrack counters at start
  kernel_after.json  ... and at end
  summary.json       totals, nic_loss, RTP-leg counts, media-plane verdict
  events.log         human-readable [sylk-qos-server] lines
```

`trace_dir` is taken from SylkServer's `config.ini` `[Server] trace_dir`
(default `/var/log/sylkserver`), so by default captures land in
`/var/log/sylkserver/qos/`.

## Files

```
qos-server/
  sylk-qos-server.py        <- the daemon (stdlib only: http.server + tcpdump)
  sylk-qos-server.service   <- systemd unit
  qos-server.ini.sample     <- config sample -> install as /etc/sylkserver/qos-server.ini
  qos-server.py             <- LEGACY per-call SSH script (superseded; kept for reference)
```

## Configuration

Read the SylkServer way from `--config-dir` (default `/etc/sylkserver`),
same INI syntax as the other SylkServer files:

- `config.ini` — only `[Server] trace_dir` is used (the trace prefix).
- `qos-server.ini` — this daemon's settings; see `qos-server.ini.sample`
  (`listen`, `auth_token`, `interface`, `expected_pps`, `sample_interval`,
  `max_capture_seconds`, `retention_days`, and `[MediaProxy]` for the optional
  Janus↔MediaProxy RTP leg).

Override the config directory with `--config-dir /path`.

## Deploy

```
sudo install -d /usr/share/sylkserver/qos-server
sudo install sylk-qos-server.py /usr/share/sylkserver/qos-server/
sudo install -m 0644 qos-server.ini.sample /etc/sylkserver/qos-server.ini
# edit /etc/sylkserver/qos-server.ini: set [Server] auth_token (and optionally [MediaProxy] ip)
sudo install -m 0644 sylk-qos-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sylk-qos-server
curl -s localhost:9810/health
```

The unit grants only `CAP_NET_RAW`/`CAP_NET_ADMIN` (so tcpdump works without
full root) and runs as the `sylkserver` user so it can write under `trace_dir`.
Old call folders are pruned after `retention_days`.

## Automatic capture

A call starting in Janus *is* the trigger — the daemon starts a capture for the
call's 5-tuple and finalizes it when the call ends. Two modes (`[Janus] mode`,
default `events`):

### `events` — Janus pushes (default, preferred)

Janus' sample event handler POSTs events to the daemon's `/janus-events`
endpoint; no polling. Per handle it assembles the SIP **Call-ID** (from plugin
events) and the **selected ICE pair** (from WebRTC events) and starts/stops the
capture. If an event lacks the 5-tuple, a single `handle_info` admin lookup
fills the gap (admin access optional but recommended).

Enable it on the Janus side (two steps — event broadcasting is **off by
default**):

1. In `janus.jcfg`, in the `events: { }` section, set `broadcast = true` (and
   make sure `libjanus_sampleevh.so` isn't in its `disable` list).
2. Copy `janus.eventhandler.sampleevh.jcfg.sample` to
   `/etc/janus/janus.eventhandler.sampleevh.jcfg`, set `enabled = true` and
   `backend = "http://<this-host>:9810/janus-events"`.

Then set `mode = events` in `qos-server.ini`.

```
janus-events: listening for Janus event pushes at http://127.0.0.1:9810/janus-events ...
janus-events: call STARTED call_id=abc@ex.com 86.1.2.3:42744 <-> server:9002 -> capturing
janus-events: call ENDED call_id=abc@ex.com -> capture finalized
```

The `/janus-events` endpoint is unauthenticated (the Janus event handler can't
send a bearer token) — keep it on localhost or behind a proxy.

### `poll` — daemon polls the admin API

The daemon polls the **Admin API** (`list_sessions` → `list_handles` →
`handle_info`) every `poll_interval` seconds, detecting SIP calls with a
Call-ID + established ICE pair. Admin URL/secret are auto-detected from
`[Janus] config_dir` (the `/etc/janus` files) or set explicitly. At startup it
connects once and logs the result:

```
janus-monitor: CONNECTED to http://127.0.0.1:7088/admin OK — 2 active session(s) (polling every 2s for call start/stop)
janus-monitor: call STARTED call_id=abc@ex.com 86.1.2.3:42744 <-> server:9001 -> capturing
janus-monitor: call ENDED call_id=abc@ex.com -> capture finalized
```

Both need the Janus admin HTTP transport (`admin_http` in
`janus.transport.http.jcfg`) and an `admin_secret` (`janus.jcfg`) — `poll`
requires it, `events` uses it only for gap-fill. `mode = off` (or
`monitor = false`) disables auto-capture; calls can still be registered
manually via the HTTP API below.

## HTTP API

All endpoints except `/health` require `Authorization: Bearer <token>` (or
`?token=`). Without `auth_token` configured the daemon answers localhost only.

```
GET  /health                      {status, version, active}
POST /calls                       register + start capture
     {call_id, client_ip, client_port, server_port,
      server_ip?, mediaproxy_ip?, expected_pps?}
POST /calls/{call_id}/stop        finalize -> {summary}
GET  /calls                       active/recent calls
GET  /calls/{call_id}             manifest {meta, summary, artifacts[]}
GET  /calls/{call_id}/bundle      whole folder as .tar.gz
GET  /calls/{call_id}/{artifact}  capture.pcap | samples.ndjson | summary.json | ...
```

The dev-host pipeline (`sylk-mobile/qos/qos-test.sh`) registers and fetches
automatically when pointed at the daemon:

```
QOS_DAEMON_URL=http://janus.example.com:9810 QOS_DAEMON_TOKEN=secret \
QOS_FETCH_DIR=./bundles ./qos-test.sh
```

Sylk Mobile fetches by Call-ID over the same API (`accountInfo.js`:
`getQosCapture` / `getQosBundle`). `curl` example:

```
curl -H "Authorization: Bearer secret" http://janus.example.com:9810/calls/$CALLID
curl -H "Authorization: Bearer secret" http://janus.example.com:9810/calls/$CALLID/bundle -o call.tar.gz
```

## Loss localization & the MediaProxy-side peer probe

Every finalized summary now contains a `loss_analysis` section: hop-by-hop
packet counts per direction plus plain-language `findings` that attribute loss
to a specific hop:

```
far end <-> MediaProxy <-> Janus <-> phone
            [peer NIC]    [this NIC]   (client report)
```

- **inside this host** — packets arrived at the NIC from MediaProxy but never
  left toward the client (or vice versa): Janus/kernel drop. Cross-checked
  against kernel counters (UDP `RcvbufErrors`/`SndbufErrors`, NIC rx/tx drops),
  which are now part of the verdict, not just raw numbers.
- **on the wire MediaProxy↔Janus** — needs the peer probe (below).
- **at/beyond MediaProxy** — deficit between what MediaProxy receives and what
  it sends back (audio symmetry).
- **Janus→phone last hop** — the summary marks this hop as needing the
  client-side count; the Sylk Mobile report fills it in and renders the whole
  table (Loss localization section).

To split "lost between the hosts" from "lost inside/beyond MediaProxy", run a
second sylk-qos-server **on the MediaProxy host** (same deploy, but set
`[Janus] mode = off` there — it never talks to Janus) and point this daemon at
it:

```
[MediaProxy]
ip = <mediaproxy-ip>
probe_url = https://<mediaproxy-host>:9810
probe_token = <the peer's [Server] auth_token>
```

For every call, the daemon registers the same Janus↔MediaProxy RTP tuple on
the peer (with `probe: true` so the peer never probes further and `with_rtcp`
so both tallies count RTP+RTCP alike). At call end the peer's counts and
kernel counters are merged into this call's summary as the `mediaproxy_host`
leg and reconciled per hop. Failures are soft: an unreachable peer only adds
`mediaproxy_probe_error` to the summary.

The SIP-leg counters are also now split RTP vs RTCP (`rtcp_janus_to_mediaproxy`,
`rtcp_mediaproxy_to_janus` in the `sip` leg).

## Relationship to the SylkServer media-plane render

This daemon captures at the NIC. SylkServer separately logs a `[media-plane]`
block per call from Janus' own counters (Admin API `handle_info`). The Janus
admin URL/secret are auto-detected from `/etc/janus` (`janus.jcfg` +
`janus.transport.http.jcfg`); see `webrtcgateway.ini.sample` `[Janus]`. Read
together with the MediaProxy stats in the fetched media trace, they localize a
one-way break to a single hop.
