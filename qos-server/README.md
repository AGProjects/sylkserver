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

## HTTP API

All endpoints except `/health` require `Authorization: Bearer <token>` (or
`?token=`). Without `auth_token` configured the daemon answers localhost only.

```
GET  /health                      {status, version, active}
POST /calls                       register + start capture
     {call_id, client_ip, client_port, server_port,
      server_ip?, mediaproxy_ip?, rtp_port_min?, rtp_port_max?, expected_pps?}
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

## Relationship to the SylkServer media-plane render

This daemon captures at the NIC. SylkServer separately logs a `[media-plane]`
block per call from Janus' own counters (Admin API `handle_info`). The Janus
admin URL/secret are auto-detected from `/etc/janus` (`janus.jcfg` +
`janus.transport.http.jcfg`); see `webrtcgateway.ini.sample` `[Janus]`. Read
together with the MediaProxy stats in the fetched media trace, they localize a
one-way break to a single hop.
