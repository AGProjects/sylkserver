# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

__all__ = ['ConferenceConfig', 'get_room_config']

import re

from application.configuration import ConfigFile, ConfigSection, ConfigSetting
from application.configuration.datatypes import StringList, Hostname
from application.system import host

from sylk.configuration.datatypes import IPAddress, Path, Port
from sylk.resources import Resources, VarResources


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
        domain = uri.host
        uri = re.sub('^(sip:|sips:)', '', str(uri))
        return uri in self or domain in self


# Configuration objects

class ConferenceConfig(ConfigSection):
    __cfgfile__ = 'conference.ini'
    __section__ = 'Conference'

    history_size = 20

    access_policy = ConfigSetting(type=AccessPolicyValue, value=AccessPolicyValue('allow, deny'))
    allow = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('all'))
    deny = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('none'))

    file_transfer_dir = ConfigSetting(type=Path, value=Path(VarResources.get('spool/sylkserver/files')))
    push_file_transfer = False

    screen_sharing_dir = ConfigSetting(type=Path, value=Path(VarResources.get('spool/sylkserver/screensharing')))
    screen_sharing_ip = ConfigSetting(type=IPAddress, value=IPAddress(host.default_ip))
    screen_sharing_hostname = ConfigSetting(type=Hostname, value=IPAddress(host.default_ip))
    screen_sharing_port = ConfigSetting(type=Port, value=0)
    screen_sharing_use_https = True
    screen_sharing_certificate = ConfigSetting(type=Path, value=Path(Resources.get('tls/default.crt')))

    advertise_xmpp_support = True
    pstn_access_numbers = ConfigSetting(type=StringList, value='')


class RoomConfig(ConfigSection):
    __cfgfile__ = 'conference.ini'

    access_policy = ConfigSetting(type=AccessPolicyValue, value=AccessPolicyValue('allow, deny'))
    allow = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('all'))
    deny = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('none'))

    pstn_access_numbers = ConferenceConfig.pstn_access_numbers
    advertise_xmpp_support = ConferenceConfig.advertise_xmpp_support


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

