
from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import NetworkRangeList, StringList
from application.system import host
from sipsimple.configuration.datatypes import NonNegativeInteger, SampleRate

from sylk.configuration.datatypes import (AudioCodecs, IPAddress, LogLevel,
                                          Path, Port, PortRange,
                                          SIPProxyAddress, SRTPEncryption,
                                          TrustedPeerList)
from sylk.resources import Resources, VarResources
from sylk.tls import Certificate, PrivateKey


class ServerConfig(ConfigSection):
    __cfgfile__ = 'config.ini'
    __section__ = 'Server'

    ca_file = ConfigSetting(type=Path, value=Path(Resources.get('tls/ca.crt')))
    certificate = ConfigSetting(type=Path, value=Path(Resources.get('tls/default.crt')))
    verify_server = False
    enable_bonjour = False
    default_application = 'conference'
    application_map = ConfigSetting(type=StringList, value=['echo:echo'])
    disabled_applications = ConfigSetting(type=StringList, value='')
    extra_applications_dir = ConfigSetting(type=Path, value=None)
    trace_dir = ConfigSetting(type=Path, value=Path(VarResources.get('log/sylkserver')))
    trace_dns = False
    trace_sip = False
    trace_msrp = False
    trace_core = False
    trace_notifications = False
    log_level = ConfigSetting(type=LogLevel, value=LogLevel('info'))
    spool_dir = ConfigSetting(type=Path, value=Path(VarResources.get('spool/sylkserver')))


class SIPConfig(ConfigSection):
    __cfgfile__ = 'config.ini'
    __section__ = 'SIP'

    local_ip = ConfigSetting(type=IPAddress, value=IPAddress(host.default_ip))
    local_udp_port = ConfigSetting(type=Port, value=5060)
    local_tcp_port = ConfigSetting(type=Port, value=5060)
    local_tls_port = ConfigSetting(type=Port, value=5061)
    advertised_ip = ConfigSetting(type=IPAddress, value=None)
    outbound_proxy = ConfigSetting(type=SIPProxyAddress, value=None)
    # [SIP] trusted_peers: an ACL of source IP ranges allowed to send SIP
    # requests to the server. Same syntax as a network range list (comma
    # separated IPs/CIDRs plus 'any'/'none') with one extra keyword:
    # 'thor_network', which trusts the live members of the Thor network
    # (only meaningful when Thor is enabled). The keyword can be combined
    # with explicit ranges, e.g. 'thor_network, 10.0.0.0/8'.
    trusted_peers = ConfigSetting(type=TrustedPeerList, value=TrustedPeerList('any'))
    enable_ice = False

    # DoS / call-flood mitigation. These limits cap the number of
    # concurrent SIP calls (INVITE sessions) the server will carry. A new
    # INVITE that would breach a limit is rejected with 603. A value of 0
    # disables that particular limit.
    #
    #   maximum_call_count          - total active calls across the server
    #   maximum_call_count_per_ip   - active calls allowed from one source IP
    #   maximum_call_count_exclude_ips
    #                               - source networks exempt from BOTH limits
    #                                 above (never rejected, never counted).
    #                                 Same ACL syntax as trusted_peers; the
    #                                 keywords 'any' and 'none' are accepted.
    #                                 Defaults to 'none' (no exemptions).
    maximum_call_count = ConfigSetting(type=NonNegativeInteger, value=200)
    maximum_call_count_per_ip = ConfigSetting(type=NonNegativeInteger, value=10)
    maximum_call_count_exclude_ips = ConfigSetting(type=NetworkRangeList, value=NetworkRangeList('none'))


class MSRPConfig(ConfigSection):
    __cfgfile__ = 'config.ini'
    __section__ = 'MSRP'

    use_tls = True


class RTPConfig(ConfigSection):
    __cfgfile__ = 'config.ini'
    __section__ = 'RTP'

    audio_codecs = ConfigSetting(type=AudioCodecs, value=['G722', 'opus', 'PCMA', 'PCMU'])
    port_range = ConfigSetting(type=PortRange, value=PortRange('50000:50500'))
    srtp_encryption = ConfigSetting(type=SRTPEncryption, value='sdes')
    timeout = ConfigSetting(type=NonNegativeInteger, value=30)
    sample_rate = ConfigSetting(type=SampleRate, value=16000)
    # Number of audio mixers to run. Each AudioMixer drives its own pjmedia
    # clock thread (with the GIL released), so N mixers spread the media work
    # (mixing + codec) across N CPU cores within this single process.
    #   1 = original behaviour (one mixer, one core)
    #   0 = auto (one mixer per CPU core)
    # Calls and conference rooms are distributed across the pool; a single
    # conference room always stays on one mixer (its participants must share
    # mixer slots to hear each other).
    mixer_pool_size = ConfigSetting(type=NonNegativeInteger, value=1)


class WebServerConfig(ConfigSection):
    __cfgfile__ = 'config.ini'
    __section__ = 'WebServer'

    local_ip = ConfigSetting(type=IPAddress, value=IPAddress(host.default_ip))
    local_port = ConfigSetting(type=Port, value=10888)
    public_port = ConfigSetting(type=Port, value=None)
    hostname = ''
    certificate = ConfigSetting(type=Path, value=None)
    certificate_chain = ConfigSetting(type=Path, value=None)
    log_dir = ConfigSetting(type=Path, value=Path(VarResources.get('log/sylkserver')))


class ThorNodeConfig(ConfigSection):
    __cfgfile__ = 'config.ini'
    __section__ = 'ThorNetwork'

    enabled = False
    domain = "sipthor.net"
    multiply = 1000
    certificate = ConfigSetting(type=Certificate, value=None)
    private_key = ConfigSetting(type=PrivateKey, value=None)
    ca = ConfigSetting(type=Certificate, value=None)
