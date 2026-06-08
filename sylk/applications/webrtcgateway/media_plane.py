
"""
media_plane.py — end-of-call media-plane break locator for the WebRTC gateway.

A Sylk WebRTC↔WebRTC call's media plane is:

    phone A  ⇄  Janus(A) + SIP plugin  ⇄  MediaProxy  ⇄  Janus(B) + SIP plugin  ⇄  phone B

Each ConnectionHandler in SylkServer owns ONE of those Janus SIP legs (the one
facing its own WebRTC client). This module renders, at call end, a per-leg
"[media-plane]" block that shows — authoritatively, from Janus' own counters —
whether RTP is flowing in each direction across that leg, and where (relative
to MediaProxy) a one-way / no-media break sits.

Two such blocks (one logged by each phone's ConnectionHandler) plus the
client-side qos-stats render and the fetched MediaProxy trace together cover
the whole chain.

Inputs, all best-effort (any may be missing — the renderer degrades):
  - handle_info     : parsed Janus Admin API `handle_info.info` dict for the leg
  - media_plane data: receiving flags (media events), slowlink lost counts,
                      negotiated SDP directions, established timestamp

Nothing here raises: a diagnostic must never break a teardown path.
"""

import time

__all__ = ('parse_sdp_directions', 'extract_handle_counters', 'extract_handle_sdps', 'render')


_DIRECTIONS = ('sendrecv', 'sendonly', 'recvonly', 'inactive')


def parse_sdp_directions(sdp):
    """Return {media_type: {'direction','port','addr'}} from an SDP blob.

    Honours session-level direction attributes, per-media overrides, and
    treats a zero port as inactive (medium disabled). Keeps the first
    m-line of each media type. Never raises."""
    result = {}
    if not sdp:
        return result
    try:
        session_dir = 'sendrecv'
        current = None
        session_level = True
        for raw in sdp.splitlines():
            line = raw.strip()
            if line.startswith('m='):
                session_level = False
                parts = line[2:].split()
                mtype = parts[0] if parts else 'application'
                port = None
                if len(parts) > 1 and parts[1].isdigit():
                    port = int(parts[1])
                current = {'type': mtype, 'port': port, 'addr': None, 'direction': None}
                result.setdefault(mtype, current)
            elif line.startswith('c=') and current is not None:
                toks = line.split()
                if len(toks) >= 3:
                    current['addr'] = toks[2]
            elif line.startswith('a='):
                attr = line[2:].strip().lower()
                if attr in _DIRECTIONS:
                    if session_level:
                        session_dir = attr
                    elif current is not None and current['direction'] is None:
                        current['direction'] = attr
        for media in result.values():
            if media['direction'] is None:
                media['direction'] = session_dir
            if media['port'] == 0:
                media['direction'] = 'inactive'
    except Exception:
        pass
    return result


def extract_handle_counters(info):
    """Walk a Janus handle_info dict and aggregate RTP counters per medium.

    Returns {medium: {'in': {packets,bytes,nacks}, 'out': {...}}}.

    Robust across Janus versions: it looks for any nested ``in_stats`` /
    ``out_stats`` dicts (they live under streams[].components[] in 1.x and
    under streams[] in older builds) and attributes them to the nearest
    enclosing ``type`` / ``media`` label. Never raises."""
    result = {}
    if not isinstance(info, dict):
        return result

    def blank():
        return {'packets': 0, 'bytes': 0, 'nacks': 0}

    def walk(node, medium):
        try:
            if isinstance(node, dict):
                medium = node.get('type') or node.get('media') or medium
                for key, direction in (('in_stats', 'in'), ('out_stats', 'out')):
                    stats = node.get(key)
                    if isinstance(stats, dict) and ('packets' in stats or 'bytes' in stats):
                        slot = result.setdefault(medium or 'audio', {}).setdefault(direction, blank())
                        slot['packets'] += int(stats.get('packets', 0) or 0)
                        slot['bytes'] += int(stats.get('bytes', 0) or 0)
                        slot['nacks'] += int(stats.get('nacks', 0) or 0)
                for value in node.values():
                    walk(value, medium)
            elif isinstance(node, list):
                for value in node:
                    walk(value, medium)
        except Exception:
            pass

    walk(info, None)
    return result


def extract_handle_sdps(info):
    """Return {'local': {...}, 'remote': {...}} SDP directions from handle_info.

    These are the SIP-side SDPs Janus negotiated toward MediaProxy/the peer —
    the half of the leg the WebRTC client never sees. Never raises."""
    out = {}
    if not isinstance(info, dict):
        return out
    sdps = info.get('sdps') or info.get('sdp') or {}
    if isinstance(sdps, dict):
        for side in ('local', 'remote'):
            blob = sdps.get(side)
            if blob:
                out[side] = parse_sdp_directions(blob)
    return out


def _fmt_count(counters, medium, direction):
    slot = counters.get(medium, {}).get(direction)
    if not slot:
        return None
    return slot


def _dir_glyph(ok):
    if ok is True:
        return '✓'
    if ok is False:
        return '✗'
    return '?'


def render(session_info, handle_info=None):
    """Build the multi-line [media-plane] diagram for one SIP leg.

    Returns a string (possibly multi-line). The caller logs it. The renderer
    pulls everything it can from handle_info + the media_plane dict that the
    event handlers accumulated on session_info, and never raises."""
    try:
        return _render(session_info, handle_info)
    except Exception as e:  # diagnostics must never break teardown
        return '[media-plane] render error: {!s}'.format(e)


def _render(session_info, handle_info):
    mp = getattr(session_info, 'media_plane', None) or {}
    counters = extract_handle_counters(handle_info) if handle_info else {}
    sip_sdp = extract_handle_sdps(handle_info) if handle_info else mp.get('sip_sdp', {})
    webrtc_sdp = mp.get('webrtc_sdp', {})
    receiving = mp.get('media_receiving', {})
    slowlink = mp.get('slowlink', {})

    call_id = getattr(session_info, 'call_id', None) or '?'
    direction = getattr(session_info, 'direction', '?')
    account = getattr(getattr(session_info, 'account', None), 'id', '?')
    established = mp.get('established_at')
    dur = '{:.0f}'.format(time.time() - established) if established else '?'

    # Which media are in play: union of everything we have a signal for.
    media_types = set(counters) | set(receiving)
    for side in webrtc_sdp.values():
        media_types |= set(side)
    media_types &= {'audio', 'video'}
    if not media_types:
        media_types = {'audio'}

    lines = []
    lines.append('[media-plane] call={call_id} leg={direction} account={account} dur={dur}s'.format(
        call_id=call_id, direction=direction, account=account, dur=dur))
    if handle_info is None:
        lines.append('  (Janus handle_info unavailable — counters from events/SDP only;'
                     ' set [Janus] admin_url/admin_secret in webrtcgateway.ini)')

    for medium in sorted(media_types):
        in_c = _fmt_count(counters, medium, 'in')
        out_c = _fmt_count(counters, medium, 'out')
        recv = receiving.get(medium)  # Janus receiving RTP from the phone?

        # phone → Janus  (Janus inbound from the WebRTC peer)
        in_ok = None
        if in_c is not None:
            in_ok = in_c['packets'] > 0
        elif recv is not None:
            in_ok = recv
        in_str = '{}p/{}b'.format(in_c['packets'], in_c['bytes']) if in_c else '?'

        # Janus → phone  (Janus outbound to the WebRTC peer; sourced from the SIP side)
        out_ok = out_c['packets'] > 0 if out_c is not None else None
        out_str = '{}p/{}b'.format(out_c['packets'], out_c['bytes']) if out_c else '?'

        off = webrtc_sdp.get('offer', {}).get(medium, {}).get('direction', '?')
        ans = webrtc_sdp.get('answer', {}).get(medium, {}).get('direction', '?')
        sloc = sip_sdp.get('local', {}).get(medium, {}).get('direction', '?')
        srem = sip_sdp.get('remote', {}).get(medium, {}).get('direction', '?')

        lines.append('  {m:<5} phone {gi}─▶ Janus   in : {ins:<14} recv_from_phone={recv}'.format(
            m=medium, gi=_dir_glyph(in_ok), ins=in_str, recv=('?' if recv is None else recv)))
        lines.append('  {pad} phone {go}◀─ Janus   out: {outs:<14} (RTP arriving from MediaProxy side)'.format(
            pad=' ' * 5, go=_dir_glyph(out_ok), outs=out_str))
        lines.append('  {pad} sdp webrtc[offer={off} answer={ans}] sip[local={sloc} remote={srem}]'.format(
            pad=' ' * 5, off=off, ans=ans, sloc=sloc, srem=srem))
        if slowlink.get('down_lost') is not None or slowlink.get('up_lost') is not None:
            lines.append('  {pad} slowlink down_lost={d} up_lost={u}'.format(
                pad=' ' * 5, d=slowlink.get('down_lost', '?'), u=slowlink.get('up_lost', '?')))

        lines.append('  {pad} {verdict}'.format(pad=' ' * 5, verdict=_verdict(medium, in_ok, out_ok, off, ans, sloc, srem)))

    return '\n'.join(lines)


def _verdict(medium, in_ok, out_ok, off, ans, sloc, srem):
    # Negotiated one-way check first — a one-way SDP explains a one-way call
    # without any packet loss at all.
    negotiated_oneway = any(d in ('sendonly', 'recvonly', 'inactive')
                            for d in (off, ans, sloc, srem) if d not in ('?', None))
    if in_ok and out_ok:
        return 'verdict: {} OK — bidirectional RTP across this Janus leg'.format(medium)
    if in_ok is False and out_ok is False:
        base = 'verdict: {} DEAD both ways on this leg'.format(medium)
    elif out_ok is False:
        # phone is heard by Janus, but Janus has nothing to send to the phone:
        # nothing is arriving from the MediaProxy side.
        base = ('verdict: {} one-way — phone→Janus OK, Janus→phone DEAD; '
                'break is UPSTREAM (MediaProxy → this Janus, far leg, or far phone)'.format(medium))
    elif in_ok is False:
        # Janus → phone works, but the phone isn't reaching Janus.
        base = ('verdict: {} one-way — Janus→phone OK, phone→Janus DEAD; '
                'break is the WebRTC uplink (this phone → this Janus: ICE/DTLS/mute/SDP)'.format(medium))
    else:
        base = 'verdict: {} inconclusive (no Janus counters; check admin_url)'.format(medium)
    if negotiated_oneway:
        base += '  [NOTE: SDP negotiated one-way — off={} ans={} sip_local={} sip_remote={}]'.format(off, ans, sloc, srem)
    return base
