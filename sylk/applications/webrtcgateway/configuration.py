
import os
import re

from application.configuration import ConfigFile, ConfigSection, ConfigSetting
from application.configuration.datatypes import NetworkAddress, StringList

from sylk.configuration import ServerConfig
from sylk.configuration.datatypes import Path, SIPProxyAddress

__all__ = ['get_room_config']

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


class PolicySettingValue(list):
    def __init__(self, value):
        if isinstance(value, (tuple, list)):
            l = [str(x) for x in value]
        elif isinstance(value, basestring):
            if value.lower() in ('none', ''):
                return list.__init__(self, [])
            elif value.lower() in ('any', 'all', '*'):
                return list.__init__(self, ['*'])
            else:
                l = re.split(r'\s*,\s*', value)
        else:
            raise TypeError("value must be a string, list or tuple")
        values = []
        for item in l:
            if '@' in item:
                values.append(SIPAddress(item))
            else:
                values.append(Domain(item))
        return list.__init__(self, values)

    def match(self, uri):
        if self == ['*']:
            return True

        (user, domain) = uri.split("@")
        uri = re.sub('^(sip:|sips:)', '', str(uri))
        return uri in self or domain in self


class ManagementInterfaceAddress(NetworkAddress):
    default_port = 20888


# Configuration objects

class GeneralConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'General'

    web_origins = ConfigSetting(type=StringList, value=['*'])
    sip_domains = ConfigSetting(type=StringList, value=['*'])
    outbound_sip_proxy = ConfigSetting(type=SIPProxyAddress, value=None)
    trace_websocket = False
    websocket_ping_interval = 120
    recording_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'videoconference', 'recordings')))
    http_management_interface = ConfigSetting(type=ManagementInterfaceAddress, value=ManagementInterfaceAddress('127.0.0.1'))
    http_management_auth_secret = ConfigSetting(type=str, value=None)
    firebase_server_key = ConfigSetting(type=str, value=None)


class JanusConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'Janus'

    api_url = 'ws://127.0.0.1:8188'
    api_secret = '0745f2f74f34451c89343afcdcae5809'
    trace_janus = False


class RoomConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'

    record = False
    access_policy = ConfigSetting(type=AccessPolicyValue, value=AccessPolicyValue('allow, deny'))
    allow = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('all'))
    deny = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('none'))


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


