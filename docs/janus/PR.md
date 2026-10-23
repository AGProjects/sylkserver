**Title:** NoSIP: decide SDES-SRTP per media line, fix crash on mixed SRTP/RTP peers

### Problem

The NoSIP plugin decides whether to SRTP-decrypt incoming packets with the session-wide `has_srtp_remote` flag, and whether to SRTP-encrypt outgoing packets with `has_srtp_local`. The SRTP contexts, though, are per media line (`audio_srtp_in`/`video_srtp_in`, `audio_srtp_out`/`video_srtp_out`).

When the peer uses SDES-SRTP on one media line and plain RTP on another, `has_srtp_remote` is TRUE but the context of the plain line was never created. This happens when an optional SDES offer (`"srtp": "sdes_optional"`) is answered with `a=crypto` on the audio line only. The first RTP or RTCP packet the peer sends on that line goes to `srtp_unprotect()` with a NULL context, and Janus crashes with SIGSEGV, taking down every session on the server.

We hit this in production with a SIP peer that answers SRTP audio and plain RTP video:

```
[NoSIP-0x7f4ab4007800] Got SIP peer video SSRC: 878203724
janus.service: Main process exited, code=dumped, status=11/SEGV
```

Outgoing packets on the plain line were also SRTP-protected, which the peer cannot decode.

The SIP plugin doesn't have this problem because it keeps per-media flags (`has_srtp_local_audio`/`_video`, `has_srtp_remote_audio`/`_video`).

### Fix

- **Per-line decision:** SDES-SRTP is used on a media line only when the SRTP contexts of that line exist in both directions (`janus_nosip_srtp_active()`). This covers incoming RTP and RTCP, outgoing RTP and RTCP, and the PLI sent to the peer.
- **No crypto on plain lines:** when the peer already uses SDES on other lines, no `a=crypto` is added to a line where it doesn't (`janus_nosip_srtp_plain_line()`). That applies to answers to such an offer and to later re-offers, so we don't advertise keys for a line that is plain RTP.

Lines where both sides use SDES behave exactly as before.

### Reproduction

`repro_nosip_mixed_srtp.py` drives the NoSIP plugin over the HTTP API:
1. `generate` with a browser offer (audio + video) and `sdes_optional`.
2. `process` an answer with `a=crypto` on audio only.
3. Send plain RTP to the video port.

Results on master `10bd072`, built with `--enable-plugin-nosip --enable-rest`:

```
== unpatched
plain offer: audio port 30000, video port 30002
answer processed (SRTP audio, RTP video)
Janus died                         (Segmentation fault, exit status 139)
== patched
plain offer: audio port 30000, video port 30002
answer processed (SRTP audio, RTP video)
Janus is alive
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01YQg7tP7KC57FB25JMcccJ4
