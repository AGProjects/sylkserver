
import re

from collections import defaultdict
from itertools import count
from sipsimple.core import SDPSession, SDPMediaStream, SDPAttribute, SDPConnection

from sylk.applications.xmppgateway.xmpp.stanzas import jingle

__all__ = 'jingle_to_sdp', 'sdp_to_jingle'


# IPv4 only for now, I'm sorry
ipv4_re = re.compile("^\d{1,3}.\d{1,3}.\d{1,3}.\d{1,3}$")


def content_to_sdpstream(content):
    if content.description is None or content.transport is None:
        raise ValueError
    media_stream = SDPMediaStream(str(content.description.media), 0, 'RTP/AVP')
    formats = []
    attributes = []
    for item in content.description.payloads:
        formats.append(item.id)
        attributes.append(SDPAttribute('rtpmap', '%d %s/%d' % (item.id, str(item.name), item.clockrate)))
        if item.maxptime:
            attributes.append(SDPAttribute('maxptime', str(item.maxptime)))
        if item.ptime:
            attributes.append(SDPAttribute('ptime', str(item.ptime)))
        if item.parameters:
            parameters_str = ';'.join(('%s=%s' % (p.name, p.value) for p in item.parameters))
            attributes.append(SDPAttribute('fmtp', '%d %s' % (item.id, str(parameters_str))))
    media_stream.formats = map(str, formats)
    media_stream.attributes = attributes  # set attributes so that _codec_list is generated
    if content.description.encryption:
        if content.description.encryption.required:
            media_stream.transport = 'RTP/SAVP'
        for crypto in content.description.encryption.cryptos:
            crypto_str = '%s %s %s' % (crypto.tag, crypto.crypto_suite, crypto.key_params)
            if crypto.session_params:
                crypto_str += ' %s' % crypto.session_params
            media_stream.attributes.append(SDPAttribute('crypto', str(crypto_str)))
    if isinstance(content.transport, jingle.IceUdpTransport):
        if content.transport.ufrag:
            media_stream.attributes.append(SDPAttribute('ice-ufrag', str(content.transport.ufrag)))
        if content.transport.password:
            media_stream.attributes.append(SDPAttribute('ice-pwd', str(content.transport.password)))
        for candidate in content.transport.candidates:
            if not ipv4_re.match(candidate.ip):
                continue
            candidate_str = '%s %d %s %d %s %d typ %s' % (candidate.foundation, candidate.component, candidate.protocol.upper(), candidate.priority, candidate.ip, candidate.port, candidate.typ)
            if candidate.related_addr and candidate.related_port:
                candidate_str += ' raddr %s rport %d' % (candidate.related_addr, candidate.related_port)
            media_stream.attributes.append(SDPAttribute('candidate', str(candidate_str)))
        if content.transport.remote_candidate:
            remote_candidate = content.transport.remote_candidate
            remote_candidates_str = '%d %s %d' % (remote_candidate.component, remote_candidate.ip, remote_candidate.port)
            media_stream.attributes.append(SDPAttribute('remote-candidates', str(remote_candidates_str)))
    elif isinstance(content.transport, jingle.RawUdpTransport):
        # Nothing to do here
        pass
    else:
        raise ValueError
    # Set the proper connection information, pick the first RTP candidate and use that
    try:
        candidate = next(c for c in content.transport.candidates if c.component == 1 and ipv4_re.match(c.ip))
    except StopIteration:
        raise ValueError
    media_stream.connection = SDPConnection(str(candidate.ip))
    media_stream.port = candidate.port

    return media_stream


def jingle_to_sdp(payload):
    sdp = SDPSession('127.0.0.1')
    for c in payload.content:
        try:
            media_stream = content_to_sdpstream(c)
        except ValueError:
            continue
        sdp.media.append(media_stream)
    return sdp


ice_candidate_re = re.compile(r"""^(?P<foundation>[a-zA-Z0-9+/]+) (?P<component>\d+) (?P<protocol>[a-zA-Z]+) (?P<priority>\d+) (?P<ip>[0-9a-fA-F.:]+) (?P<port>\d+) typ (?P<type>[a-zA-Z]+)(?: raddr (?P<raddr>[0-9a-fA-F.:]+) rport (?P<rport>\d+))?$""", re.MULTILINE)
crypto_re = re.compile(r"""^(?P<tag>\d+) (?P<suite>[a-zA-Z0-9\_]+) (?P<key_params>[a-zA-Z0-9\:\+]+)(?: (?P<session_params>[a-zA-Z0-9\:\+]+))?$""", re.MULTILINE)


def sdpstream_to_content(sdp, index):
    media_stream = sdp.media[index]
    content = jingle.Content('initiator', media_stream.media)
    content.description = jingle.RTPDescription(media=media_stream.media)
    try:
        ptime = next(attr.value for attr in media_stream.attributes if attr.name=='ptime')
    except StopIteration:
        ptime = None
    try:
        maxptime = next(attr.value for attr in media_stream.attributes if attr.name=='maxptime')
    except StopIteration:
        maxptime = None
    rtp_mappings = media_stream.rtp_mappings.copy()
    MediaCodec = rtp_mappings[0].__class__
    rtpmap_lines = '\n'.join(attr.value for attr in media_stream.attributes if attr.name=='rtpmap')
    rtpmap_codecs = dict([(int(type), MediaCodec(name, rate)) for type, name, rate in media_stream.rtpmap_re.findall(rtpmap_lines)])
    rtp_mappings.update(rtpmap_codecs)
    for item in media_stream.formats:
        codec = rtp_mappings.get(int(item), None)
        if codec is not None:
            pt = jingle.PayloadType(int(item), codec.name, codec.rate, 1, ptime=ptime, maxptime=maxptime)
            for attr in (attr for attr in media_stream.attributes if attr.name=='fmtp' and attr.value.startswith(item)):
                value = attr.value.split(' ', 1)[1]
                for v in value.split(';'):
                    fmtp_name, sep, fmtp_value = v.partition('=')
                    pt.parameters.append(jingle.Parameter(fmtp_name, fmtp_value))
            content.description.payloads.append(pt)
    content.description.encryption = jingle.Encryption(required=media_stream.transport=='RTP/SAVP')
    crypto_lines = '\n'.join(attr.value for attr in media_stream.attributes if attr.name=='crypto')
    for tag, suite, key_params, session_params in crypto_re.findall(crypto_lines):
        content.description.encryption.cryptos.append(jingle.Crypto(suite, key_params, tag, session_params))
    if media_stream.has_ice_candidates:
        foundation_counter = count(1)
        foundation_map = defaultdict(foundation_counter.next)
        id_counter = count(100)
        if not media_stream.has_ice_attributes and not sdp.has_ice_attributes:
            raise ValueError
        ufrag_attr = next(attr for attr in media_stream.attributes+sdp.attributes if attr.name=='ice-ufrag')
        pwd_attr = next(attr for attr in media_stream.attributes+sdp.attributes if attr.name=='ice-pwd')
        content.transport = jingle.IceUdpTransport(ufrag=ufrag_attr.value, pwd=pwd_attr.value)
        candidate_lines = '\n'.join(attr.value for attr in media_stream.attributes if attr.name=='candidate')
        for foundation, component, protocol, priority, ip, port, type, raddr, rport in ice_candidate_re.findall(candidate_lines):
            candidate = jingle.ICECandidate(component, foundation_map[foundation], 0, next(id_counter), ip, 0, port, priority, protocol.lower(), type, raddr or None, rport or None)
            content.transport.candidates.append(candidate)
        # TODO: translate remote-candidate
    else:
        content.transport = jingle.RawUdpTransport()
        connection = media_stream.connection or sdp.connection
        if not connection:
            raise ValueError
        content.transport.candidates.append(jingle.UDPCandidate(1, 0, 100, connection.address, media_stream.port, 'UDP'))
        # TODO: component for RTCP
    return content


def sdp_to_jingle(sdp):
    payload = jingle.Jingle(None, None)
    # action and sid will be filled up by the session
    for index, media_stream in enumerate(sdp.media):
        try:
            content = sdpstream_to_content(sdp, index)
        except ValueError:
            continue
        payload.content.append(content)
    return payload

