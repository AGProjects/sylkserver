#!/usr/bin/env python3
"""
Reproduce the Janus NoSIP crash with an answer that uses SDES-SRTP for audio
and plain RTP for video: once the media is up, the first plain RTP packet on
the video line makes NoSIP call srtp_unprotect() with a NULL context.

Usage: repro_nosip_mixed_srtp.py <janus http api url, e.g. http://127.0.0.1:8088/janus>
Exit status: 0 if Janus is still alive after the packets, 1 if it died.
"""

import json
import random
import socket
import struct
import sys
import time
import urllib.request

API = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8088/janus'

# A WebRTC offer with one audio and one video line (as sent by a browser)
WEBRTC_OFFER = '''v=0
o=- 7433555369094094516 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE 0 1
a=msid-semantic: WMS stream
m=audio 9 UDP/TLS/RTP/SAVPF 111 0 8
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:lU1/
a=ice-pwd:LsfbnKFcHrGMce1vygWz8uCT
a=ice-options:trickle
a=fingerprint:sha-256 F8:B4:ED:CE:C4:41:6D:E1:D1:D2:C0:34:C5:09:80:04:60:98:82:FD:3D:76:40:0C:DF:4A:2F:9F:65:BA:0B:7F
a=setup:actpass
a=mid:0
a=sendrecv
a=msid:stream audio0
a=rtcp-mux
a=rtpmap:111 opus/48000/2
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=ssrc:237042528 cname:test
m=video 9 UDP/TLS/RTP/SAVPF 96
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:lU1/
a=ice-pwd:LsfbnKFcHrGMce1vygWz8uCT
a=ice-options:trickle
a=fingerprint:sha-256 F8:B4:ED:CE:C4:41:6D:E1:D1:D2:C0:34:C5:09:80:04:60:98:82:FD:3D:76:40:0C:DF:4A:2F:9F:65:BA:0B:7F
a=setup:actpass
a=mid:1
a=sendrecv
a=msid:stream video0
a=rtcp-mux
a=rtpmap:96 VP8/90000
a=ssrc:3984668848 cname:test
'''.replace('\n', '\r\n')


def request(path, body):
    body = dict(body, transaction='%016x' % random.getrandbits(64))
    data = json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(API + path, data=data, headers={'Content-Type': 'application/json'}), timeout=10) as response:
        return json.load(response)


def wait_event(session_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen('{}/{}?maxev=1'.format(API, session_id), timeout=35) as response:
            event = json.load(response)
        if event.get('janus') == 'event':
            return event
    raise RuntimeError('no event')


def alive():
    try:
        with urllib.request.urlopen(API + '/info', timeout=3) as response:
            return json.load(response).get('janus') == 'server_info'
    except Exception:
        return False


def main():
    session_id = request('', {'janus': 'create'})['data']['id']
    handle_id = request('/{}'.format(session_id), {'janus': 'attach', 'plugin': 'janus.plugin.nosip'})['data']['id']
    path = '/{}/{}'.format(session_id, handle_id)

    # 1. The browser's offer becomes a plain offer, with optional SDES on both lines
    request(path, {'janus': 'message', 'body': {'request': 'generate', 'srtp': 'sdes_optional'},
                   'jsep': {'type': 'offer', 'sdp': WEBRTC_OFFER}})
    generated = wait_event(session_id)['plugindata']['data']['result']
    offer = generated['sdp']
    ports = {line.split()[0][2:]: int(line.split()[1]) for line in offer.splitlines() if line.startswith('m=')}
    print('plain offer: audio port {audio}, video port {video}'.format(**ports))

    # 2. The SIP peer answers SDES for audio, plain RTP for video
    peer_audio, peer_video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_audio.bind(('127.0.0.1', 0))
    peer_video.bind(('127.0.0.1', 0))
    answer = '\r\n'.join([
        'v=0', 'o=- 1 1 IN IP4 127.0.0.1', 's=peer', 'c=IN IP4 127.0.0.1', 't=0 0',
        'm=audio {} RTP/AVP 0'.format(peer_audio.getsockname()[1]),
        'a=rtpmap:0 PCMU/8000',
        'a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz',
        'a=sendrecv',
        'm=video {} RTP/AVP 96'.format(peer_video.getsockname()[1]),
        'a=rtpmap:96 VP8/90000',
        'a=sendrecv', ''])
    request(path, {'janus': 'message', 'body': {'request': 'process', 'type': 'answer', 'sdp': answer}})
    wait_event(session_id)
    print('answer processed (SRTP audio, RTP video)')
    time.sleep(0.5)

    # 3. The peer sends plain RTP video
    for seq in range(5):
        packet = struct.pack('!BBHII', 0x80, 96, seq, seq * 3000, 0x11223344) + b'\x00' * 100
        peer_video.sendto(packet, ('127.0.0.1', ports['video']))
        time.sleep(0.05)
    time.sleep(1)

    if alive():
        print('Janus is alive')
        return 0
    print('Janus died')
    return 1


if __name__ == '__main__':
    sys.exit(main())
