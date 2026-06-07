
import os
import re
import socket

from application.configuration import ConfigFile, ConfigSection, ConfigSetting
from application.configuration.datatypes import NetworkAddress, StringList

from sylk.configuration import ServerConfig
from sylk.configuration.datatypes import Path, SIPProxyAddress, URL


__all__ = 'ConferenceConfig', 'get_room_config', 'iter_registered_rooms', 'pick_default_admin_ip'


# Interface name prefixes considered virtual / surrogate. We skip these when
# scanning for a private IPv4 to publish in the conference-info payload.
# Covers Docker bridges/containers, KVM/libvirt, Kubernetes CNIs, WireGuard,
# VPNs and the usual collection of macOS internal interfaces.
_VIRTUAL_IFACE_PREFIXES = (
    'lo', 'docker', 'br-', 'veth', 'vnet', 'tun', 'tap', 'virbr', 'kube',
    'cali', 'flannel', 'cni', 'cilium', 'weave', 'wg', 'zt', 'vmnet',
    'vboxnet', 'utun', 'awdl', 'llw', 'gif', 'stf', 'ap',
)


def _is_private_ipv4(ip):
    """RFC 1918 + RFC 6598 (carrier-grade NAT) check."""
    try:
        parts = [int(x) for x in ip.split('.')]
    except (ValueError, AttributeError):
        return False
    if len(parts) != 4:
        return False
    a, b = parts[0], parts[1]
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 100 and 64 <= b <= 127:  # 100.64.0.0/10 — CGNAT
        return True
    return False


def _iter_interface_ipv4_addresses():
    """Yield (interface_name, ipv4_address) tuples for every IPv4 address
    bound to a local interface. Linux/BSD path uses socket.if_nameindex()
    plus the SIOCGIFADDR ioctl; falls back to silence if not available.
    """
    try:
        import fcntl
        import struct
    except ImportError:
        return
    try:
        interfaces = socket.if_nameindex()
    except (AttributeError, OSError):
        return
    SIOCGIFADDR = 0x8915
    for _idx, name in interfaces:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ifr = struct.pack('256s', name.encode('utf-8')[:15])
            packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR, ifr)
            ip = socket.inet_ntoa(packed[20:24])
            yield (name, ip)
        except OSError:
            continue
        finally:
            s.close()


def _connect_trick_source_ip():
    """Open a UDP socket toward a private destination and read back the
    kernel's chosen source IP — the address the box would use to reach
    the default route. No packets are actually sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def pick_default_admin_ip():
    """Return the IPv4 address the admin web server should bind to by default.

    Selection order:
      1. First RFC 1918 (or CGNAT) IPv4 found on a non-virtual interface.
      2. Source IP toward the default route if it is private.
      3. '127.0.0.1' as a safe fallback.
    """
    candidates = []
    for name, ip in _iter_interface_ipv4_addresses():
        if any(name == p or name.startswith(p) for p in _VIRTUAL_IFACE_PREFIXES):
            continue
        if ip in ('0.0.0.0', '127.0.0.1'):
            continue
        if _is_private_ipv4(ip):
            candidates.append(ip)
    if candidates:
        return candidates[0]
    fallback = _connect_trick_source_ip()
    if fallback and _is_private_ipv4(fallback):
        return fallback
    return '127.0.0.1'


class ManagementInterfaceAddress(NetworkAddress):
    default_port = 10889


# Datatypes

class AccessPolicyValue(str):
    allowed_values = ('allow,deny', 'deny,allow')

    def __new__(cls, value):
        value = re.sub('\s', '', value)
        if value not in cls.allowed_values:
            raise ValueError('invalid value, allowed values are: %s' % ' | '.join(cls.allowed_values))
        return str.__new__(cls, value)


class Domain(str):
    domain_re = re.compile(r"^[a-zA-Z0-9\-_]+(\.[a-zA-Z0-9\-_]+)*$")

    def __new__(cls, value):
        value = str(value)
        if not cls.domain_re.match(value):
            raise ValueError("illegal domain: %s" % value)
        return str.__new__(cls, value)


class SIPAddress(str):
    def __new__(cls, address):
        address = str(address)
        address = address.replace('@', '%40', address.count('@')-1)
        try:
            username, domain = address.split('@')
            Domain(domain)
        except ValueError:
            raise ValueError("illegal SIP address: %s, must be in user@domain format" % address)
        return str.__new__(cls, address)


class PolicyItem(object):
    def __new__(cls, item):
        lowercase_item = item.lower()
        if lowercase_item in ('none', ''):
            return 'none'
        elif lowercase_item in ('any', 'all', '*'):
            return 'all'
        elif '@' in item:
            return SIPAddress(item)
        else:
            return Domain(item)


class PolicySettingValue(object):
    def __init__(self, value):
        if isinstance(value, (tuple, list)):
            items = [str(x) for x in value]
        elif isinstance(value, str):
            items = re.split(r'\s*,\s*', value)
        else:
            raise TypeError("value must be a string, list or tuple")
        self.items = {PolicyItem(item) for item in items}
        self.items.discard('none')

    def __repr__(self):
        return '{0.__class__.__name__}({1})'.format(self, sorted(self.items))

    def match(self, uri):
        if 'all' in self.items:
            return True
        elif not self.items:
            return False
        uri = re.sub('^(sip:|sips:)', '', str(uri))
        domain = uri.split('@')[-1]
        return uri in self.items or domain in self.items


# Configuration objects

class ConferenceConfig(ConfigSection):
    __cfgfile__ = 'conference.ini'
    __section__ = 'Conference'

    history_size = 20

    access_policy = ConfigSetting(type=AccessPolicyValue, value=AccessPolicyValue('allow, deny'))
    allow = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('all'))
    deny = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('none'))

    file_transfer_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'conference', 'files')))
    push_file_transfer = False

    screensharing_images_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'conference', 'screensharing')))

    advertise_xmpp_support = False
    pstn_access_numbers = ConfigSetting(type=StringList, value='')
    webrtc_gateway_url = ConfigSetting(type=URL, value='')

    zrtp_auto_verify = True

    # Music on hold. Global default for rooms; overridable per room via the
    # same `disable_music_on_hold` setting in RoomConfig. If `moh_disable_header`
    # is set to a header name, every incoming INVITE is inspected — if that
    # header is present with value 'Yes' (case-insensitive) the room's MoH is
    # disabled for the rest of the room's lifetime. When `moh_disable_header`
    # is empty (the default) no header check is performed.
    disable_music_on_hold = False
    moh_disable_header = ''

    # Default media types to offer a participant invited via REFER when
    # the Refer-To URI does NOT carry a `;media=<csv>` parameter. CSV of
    # `audio`, `chat`, `msrp` or `text` (msrp/text are aliases for chat).
    # Empty (the default) means: fall back to whatever media the room
    # currently has active. A REFER that DOES carry `;media=...` always
    # wins over this default — see IncomingReferralHandler in
    # sylk/applications/conference/__init__.py for the precedence rules.
    #
    # Example:
    #     [Conference]
    #     default_refer_media = audio,chat
    default_refer_media = ConfigSetting(type=str, value='')

    # Anti-fraud eviction timer for invitees whose inviter has left
    # the conference. Each outgoing INVITE issued by a REFER
    # ;method=INVITE is tagged with the inviter's AoR (the REFER's
    # From header) if its Refer-To target matches the configured
    # destination pattern (see inviter_eviction_destinations below).
    # When that AoR's last session leaves the room, a per-invitee
    # grace timer is armed; when it expires the invitee's leg is
    # BYE'd by the conference. The intent is to stop an abandoned
    # call from racking up termination charges against the inviter's
    # account when they themselves have already disconnected. The
    # timer is cancelled if the inviter rejoins (any device, matched
    # by AoR) or the invitee leaves on their own. SIP/PSTN
    # participants cannot issue REFERs in our deployment, so this
    # only ever touches WebRTC-initiated invitations. Set the grace
    # period to 0 to disable the feature entirely.
    #
    # While a timer is armed, the server log emits one line per
    # invitee per minute showing minutes remaining.
    inviter_eviction_grace_period = 1200  # seconds (20 minutes)

    # Which Refer-To destinations get tracked by the anti-fraud
    # eviction timer. One of:
    #   * `pstn` (default) — only PSTN numbers. The Refer-To URI's
    #     user part must begin with `0` or `+`. Pure SIP destinations
    #     (alice@example.com) are ignored. This is the original
    #     billing-protection scope: outbound PSTN is where the
    #     inviter's account actually gets charged termination fees.
    #   * `sip`           — only non-PSTN SIP destinations. Inverse
    #     of `pstn`. Useful when SIP-trunk providers also bill per
    #     leg and you want the protection without the PSTN-format
    #     gate.
    #   * `all`           — every Refer-To destination is tracked,
    #     regardless of URI shape.
    # Anything unrecognised falls back to `pstn` so a typo never
    # silently widens the protection scope.
    inviter_eviction_destinations = ConfigSetting(type=str, value='pstn')

    # Marker that lets a SIP-to-Janus audio bridge identify itself on its
    # INVITE. When the incoming Request-URI carries a parameter
    # `;app=<this value>`, the conference application:
    #   * answers immediately (skips the 4-second human ringback delay)
    #   * forces music-on-hold off for the joining room
    # Set to '' to disable the detection entirely.
    audio_bridge_app_param = 'sylk-janus-audio-bridge'

    # IVR for the conference selector. When a call comes in to
    # <default_conference_selector>@<domain>, the application plays a prompt
    # and collects DTMF, then routes the same session into <digits>@<domain>
    # as if the caller had dialed that conference directly. Override the
    # default user name in conference.ini if needed, for example:
    #     [Conference]
    #     default_conference_selector = conference
    default_conference_selector = 'conference'
    asterisk_sounds_dir = ConfigSetting(type=Path, value=Path('/usr/share/asterisk/sounds/en'))
    select_conference_prompt = 'conf-getconfno.wav'
    select_conference_invalid_prompt = 'conf-invalid.wav'
    select_conference_goodbye_prompt = 'vm-goodbye.wav'
    select_conference_max_digits = 32
    select_conference_initial_timeout = 10  # seconds to wait for first digit
    select_conference_interdigit_timeout = 5  # seconds of silence before finalizing
    select_conference_overall_timeout = 60  # absolute upper bound

    # Administrative HTTP API for the conference application.
    #
    # When http_management_interface is set, an HTTP listener is started on
    # the given host:port that exposes per-room information (participants,
    # audio levels) and accepts commands (mute, kick) authenticated with
    # http_management_auth_secret as a bearer-style Authorization header,
    # or with the per-room token published in the conference-info payload
    # of the sylk-janus-audio-bridge participant.
    #
    # The default host is auto-picked at startup: the first private IPv4
    # address on a non-virtual interface (Docker, KVM, k8s CNIs, VPN tunnels
    # and similar surrogate interfaces are skipped). Falls back to 127.0.0.1
    # when no private address is found. Override in conference.ini if you
    # want a specific bind address.
    #
    # Set http_management_interface to '' (empty) to disable the admin
    # interface entirely.
    http_management_interface = ConfigSetting(
        type=ManagementInterfaceAddress,
        value=ManagementInterfaceAddress('%s:10889' % pick_default_admin_ip()),
    )
    http_management_auth_secret = ConfigSetting(type=str, value=None)

    # How often the audio levels of each participant are sampled, in
    # milliseconds. The result is published to subscribers of the admin
    # API's SSE stream and made available as a snapshot via the snapshot
    # endpoint. Set to 0 to disable the periodic sampling entirely.
    audio_level_sample_period = 100

    # How often (seconds) the server emits a summary log line per room
    # listing each participant's average tx/rx level over the window.
    # The average is computed from every sample taken in the window
    # (cadence controlled by audio_level_sample_period). Set to 0 to
    # disable the periodic log entirely.
    audio_level_log_period = 5

    # How often (milliseconds) the server publishes a ConferenceRoomAudioLevels
    # notification. The notification is consumed by the admin SSE stream
    # and by the webrtcgateway, which pushes a real-time
    # conference-audio-levels event to every connected WebRTC client in
    # the matching videoroom. Decoupled from audio_level_sample_period:
    # we sample at the faster rate and roll up to mean + peak at this
    # rate. Default 250ms = 4 Hz, fine for VU-meter use without flooding
    # the WebSocket. Set to 0 to disable notifications entirely (the
    # log still works, since it has its own accumulator).
    audio_level_notify_period = 250

    # UDP server endpoint for real-time audio-level updates. The
    # conference focus binds here and accepts `subscribe` / `unsubscribe`
    # datagrams; for each live subscription it streams a JSON `audio-levels`
    # datagram on every notify tick. Used when the webrtcgateway runs on a
    # different host than the conference focus (in-process NotificationCenter
    # doesn't bridge processes).
    #
    # Set to empty to disable the UDP server entirely (the local
    # NotificationCenter path and the admin SSE endpoint are unaffected).
    audio_level_udp_listen = ConfigSetting(
        type=ManagementInterfaceAddress,
        value=ManagementInterfaceAddress('0.0.0.0:11000'))

    # Externally-reachable host:port string published in the conference-info
    # NOTIFY (as <agp-conf:audio_levels_udp_endpoint> on the audio-bridge
    # participant's User element). Set this when audio_level_udp_listen
    # binds to 0.0.0.0 / an internal interface and the reachable address
    # is different (NAT, dual-homed host, container hostname). Leave
    # empty to advertise the actual bound host:port from listenUDP.
    audio_level_udp_advertised_endpoint = ConfigSetting(type=str, value=None)

    # Shared secret required on every inbound subscribe/unsubscribe
    # datagram. Subscribers (the webrtcgateway) supply it via
    # audio_level_udp_token on their side. Defaults to
    # http_management_auth_secret when unset.
    audio_level_udp_token = ConfigSetting(type=str, value=None)


class RoomConfig(ConfigSection):
    __cfgfile__ = 'conference.ini'

    access_policy = ConfigSetting(type=AccessPolicyValue, value=AccessPolicyValue('allow, deny'))
    allow = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('all'))
    deny = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('none'))

    pstn_access_numbers = ConfigSetting(type=StringList, value=ConferenceConfig.pstn_access_numbers)
    advertise_xmpp_support = ConferenceConfig.advertise_xmpp_support
    webrtc_gateway_url = ConferenceConfig.webrtc_gateway_url

    disable_music_on_hold = ConferenceConfig.disable_music_on_hold
    zrtp_auto_verify = ConferenceConfig.zrtp_auto_verify

    # SIP REGISTER at a foreign domain. When `registrar_uri` is set on a
    # room section the conference application creates a sipsimple Account
    # for that AOR at startup and registers it perpetually. Inbound calls
    # arriving at the registered Contact (matched by To header / Request-URI
    # against `registrar_uri`) are routed into THIS room.
    #
    #   registrar_uri           = user@foreign.example   ; AOR to register
    #   password                = secret                  ; SIP auth password
    #   registrar_outbound_proxy = host[:port][;transport=udp|tcp|tls]
    #                                                    ; optional explicit
    #                                                    ; outbound proxy /
    #                                                    ; registrar; empty
    #                                                    ; means: DNS lookup
    #                                                    ; on the registrar
    #                                                    ; domain.
    registrar_uri = ConfigSetting(type=SIPAddress, value=None)
    password = ConfigSetting(type=str, value=None)
    registrar_outbound_proxy = ConfigSetting(type=SIPProxyAddress, value=None)


class Configuration(object):
    def __init__(self, data):
        self.__dict__.update(data)


def get_room_config(room):
    config_file = ConfigFile(RoomConfig.__cfgfile__)
    section = config_file.get_section(room)
    if section is not None:
        RoomConfig.read(section=room)
        config = Configuration(dict(RoomConfig))
        RoomConfig.reset()
    else:
        # Apply general policy
        config = Configuration(dict(RoomConfig))
    return config


def iter_registered_rooms():
    """Yield (room_uri, RoomConfig-Configuration) for every conference.ini
    section that declares a `registrar_uri` (i.e. wants to register at a
    foreign SIP domain so that inbound calls to that AOR drop the caller
    into the section's room).

    Sections without a `registrar_uri` value are skipped. Sections whose
    `registrar_uri` fails validation are skipped with a warning.

    The room URI is the section name verbatim — a section like
    `[agp@conference.sip2sip.info]` registers as <registrar_uri> at the
    foreign registrar and routes inbound calls into the
    `agp@conference.sip2sip.info` conference room.
    """
    from sylk.applications.conference.logger import log
    config_file = ConfigFile(RoomConfig.__cfgfile__)
    parser = getattr(config_file, 'parser', None)
    if parser is None:
        # Older python-application: fall back to scanning the file
        # ourselves with the stdlib parser. The ConfigFile object will
        # have at least .files exposing the list of loaded files.
        try:
            import configparser
            files = list(getattr(config_file, 'files', []) or [])
            if not files:
                return
            parser = configparser.ConfigParser(strict=False)
            parser.read(files)
        except Exception as e:
            log.warning('cannot enumerate room sections for registration: %s' % e)
            return
    for section_name in parser.sections():
        if section_name == ConferenceConfig.__section__:
            continue
        # Skip sections that don't look like a room URI (must be user@host).
        if '@' not in section_name:
            continue
        # Skip sections without a registrar_uri.
        try:
            if not parser.has_option(section_name, 'registrar_uri'):
                continue
        except Exception:
            continue
        try:
            RoomConfig.read(section=section_name)
            cfg = Configuration(dict(RoomConfig))
        except Exception as e:
            log.warning('skipping room section %r: configuration error: %s' % (section_name, e))
            RoomConfig.reset()
            continue
        finally:
            RoomConfig.reset()
        if not cfg.registrar_uri:
            continue
        yield section_name, cfg

