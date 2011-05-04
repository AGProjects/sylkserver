# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import NetworkRangeList, StringList
from application.system import host
from sipsimple.configuration.datatypes import NonNegativeInteger, SRTPEncryption

from sylk import configuration_filename
from sylk.configuration.datatypes import AudioCodecs, IPAddress, Port, PortRange, SIPProxyAddress
from sylk.tls import Certificate, PrivateKey


class ServerConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'Server'

    ca_file = ConfigSetting(type=str, value='/etc/sylkserver/tls/ca.crt')
    certificate = ConfigSetting(type=str, value='/etc/sylkserver/tls/sylkserver.crt')
    verify_server = False
    default_application = 'conference'
    application_map = ConfigSetting(type=StringList, value='')
    trace_dir = ConfigSetting(type=str, value='/var/log/sylkserver')
    trace_sip = False
    trace_msrp = False
    trace_notifications = False


class SIPConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'SIP'

    local_ip = ConfigSetting(type=IPAddress, value=host.default_ip)
    local_udp_port = ConfigSetting(type=Port, value=5060)
    local_tcp_port = ConfigSetting(type=Port, value=5060)
    local_tls_port = ConfigSetting(type=Port, value=None)
    outbound_proxy = ConfigSetting(type=SIPProxyAddress, value=None)
    trusted_peers = ConfigSetting(type=NetworkRangeList, value=NetworkRangeList('any'))


class MSRPConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'MSRP'

    use_tls = False


class RTPConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'RTP'

    audio_codecs = ConfigSetting(type=AudioCodecs, value=None)
    port_range = ConfigSetting(type=PortRange, value=PortRange('50000:50500'))
    srtp_encryption = ConfigSetting(type=SRTPEncryption, value='optional')
    timeout = ConfigSetting(type=NonNegativeInteger, value=30)


class ThorNodeConfig(ConfigSection):
    __cfgfile__ = configuration_filename
    __section__ = 'ThorNetwork'

    enabled = False
    domain = "sipthor.net"
    multiply = 1000
    certificate = ConfigSetting(type=Certificate, value=None)
    private_key = ConfigSetting(type=PrivateKey, value=None)
    ca = ConfigSetting(type=Certificate, value=None)


