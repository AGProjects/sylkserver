# Copyright (C) 2015 AG Projects. See LICENSE for details
#

from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import StringList

from sylk.configuration.datatypes import SIPProxyAddress


class GeneralConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'General'

    web_origins = ConfigSetting(type=StringList, value=['*'])
    sip_domains = ConfigSetting(type=StringList, value=['*'])
    outbound_sip_proxy = ConfigSetting(type=SIPProxyAddress, value=None)
    trace_websocket = False


class JanusConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'Janus'

    api_url = 'ws://127.0.0.1:8188'
    api_secret = '0745f2f74f34451c89343afcdcae5809'
    trace_janus = False

