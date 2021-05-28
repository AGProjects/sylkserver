
from application.system import host
from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import StringList
from sipsimple.configuration.datatypes import NonNegativeInteger

from sylk.configuration.datatypes import IPAddress, Port
from sylk.configuration.datatypes import Path


class XMPPGatewayConfig(ConfigSection):
    __cfgfile__ = 'xmppgateway.ini'
    __section__ = 'general'

    local_ip = ConfigSetting(type=IPAddress, value=IPAddress(host.default_ip))
    local_port = ConfigSetting(type=Port, value=5269)
    trace_xmpp = False
    log_presence = False
    transport = ConfigSetting(type=str, value='tls')
    ca_file = ConfigSetting(type=Path, value=Path('/etc/sylkserver/tls/ca.crt'))
    certificate = ConfigSetting(type=Path, value=Path('/etc/sylkserver/tls/default.crt'))
    domains = ConfigSetting(type=StringList, value=[])
    muc_prefix = 'conference'
    sip_session_timeout = ConfigSetting(type=NonNegativeInteger, value=86400)
    use_msrp_for_chat = True

