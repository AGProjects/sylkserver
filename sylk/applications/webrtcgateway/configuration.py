
import os
import re

from application.configuration import ConfigFile, ConfigSection, ConfigSetting
from application.configuration.datatypes import NetworkAddress, StringList, HostnameList

from sylk.configuration import ServerConfig
from sylk.configuration.datatypes import Path, SIPProxyAddress, VideoBitrate, VideoCodec


__all__ = 'GeneralConfig', 'JanusConfig', 'get_room_config', 'ExternalAuthConfig', 'get_auth_config', 'CassandraConfig'


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


class ManagementInterfaceAddress(NetworkAddress):
    default_port = 20888


class AuthType(str):
    allowed_values = ('SIP', 'IMAP')

    def __new__(cls, value):
        value = re.sub('\s', '', value)
        if value not in cls.allowed_values:
            raise ValueError('invalid value, allowed values are: %s' % ' | '.join(cls.allowed_values))
        return str.__new__(cls, value)


class SIPAddressList(object):
    """A list of SIP uris separated by commas"""

    def __new__(cls, value):
        if isinstance(value, (tuple, list)):
            return [SIPAddress(x) for x in value]
        elif isinstance(value, str):
            if value.lower() in ('none', ''):
                return []
            items = re.split(r'\s*,\s*', value)
            items = {SIPAddress(item) for item in items}
            return items
        else:
            raise TypeError('value must be a string, list or tuple')


# Configuration objects

class GeneralConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'General'

    web_origins = ConfigSetting(type=StringList, value=['*'])
    sip_domains = ConfigSetting(type=StringList, value=['*'])
    outbound_sip_proxy = ConfigSetting(type=SIPProxyAddress, value=None)
    trace_client = False
    websocket_ping_interval = 120
    recording_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'videoconference', 'recordings')))
    filesharing_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'videoconference', 'files')))
    http_management_interface = ConfigSetting(type=ManagementInterfaceAddress, value=ManagementInterfaceAddress('127.0.0.1'))
    http_management_auth_secret = ConfigSetting(type=str, value=None)
    sylk_push_url = ConfigSetting(type=str, value=None)
    local_sip_messages = False


class JanusConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'Janus'

    api_url = 'ws://127.0.0.1:8188'
    api_secret = '0745f2f74f34451c89343afcdcae5809'
    trace_janus = False
    max_bitrate = ConfigSetting(type=VideoBitrate, value=VideoBitrate(2016000))  # ~2 MBits/s
    video_codec = ConfigSetting(type=VideoCodec, value=VideoCodec('vp9'))
    decline_code = 486


class CassandraConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'Cassandra'

    cluster_contact_points = ConfigSetting(type=HostnameList, value=None)
    keyspace = ConfigSetting(type=str, value='')
    push_tokens_table = ConfigSetting(type=str, value='')


class RoomConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'

    record = False
    access_policy = ConfigSetting(type=AccessPolicyValue, value=AccessPolicyValue('allow, deny'))
    allow = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('all'))
    deny = ConfigSetting(type=PolicySettingValue, value=PolicySettingValue('none'))
    max_bitrate = ConfigSetting(type=VideoBitrate, value=JanusConfig.max_bitrate)
    video_codec = ConfigSetting(type=VideoCodec, value=JanusConfig.video_codec)
    video_disabled = False
    invite_participants = ConfigSetting(type=SIPAddressList, value=[])


class VideoroomConfiguration(object):
    video_codec = 'vp9'
    max_bitrate = 2016000
    record = False
    recording_dir = None
    filesharing_dir = None

    def __init__(self, data):
        self.__dict__.update(data)

    @property
    def janus_data(self):
        return dict(videocodec=self.video_codec, bitrate=self.max_bitrate, record=self.record, rec_dir=self.recording_dir)


def get_room_config(room):
    config_file = ConfigFile(RoomConfig.__cfgfile__)
    section = config_file.get_section(room)
    if section is not None:
        RoomConfig.read(section=room)
        config = VideoroomConfiguration(dict(RoomConfig))
        RoomConfig.reset()
    else:
        config = VideoroomConfiguration(dict(RoomConfig))  # use room defaults
    config.recording_dir = os.path.join(GeneralConfig.recording_dir, room)
    config.filesharing_dir = os.path.join(GeneralConfig.filesharing_dir, room)
    return config


class ExternalAuthConfig(ConfigSection):
    __cfgfile__ = 'auth.ini'
    __section__ = 'ExternalAuth'

    enable = False
    # this can't be per-server due to limitations in imaplib
    imap_ca_cert_file = ConfigSetting(type=str, value='/etc/ssl/certs/ca-certificates.crt')


class AuthConfig(ConfigSection):
    __cfgfile__ = 'auth.ini'

    auth_type = ConfigSetting(type=AuthType, value=AuthType('SIP'))
    imap_server = ConfigSetting(type=str, value='')


class AuthConfiguration(object):
    auth_type = AuthType('SIP')

    def __init__(self, data):
        self.__dict__.update(data)


def get_auth_config(domain):
    config_file = ConfigFile(AuthConfig.__cfgfile__)
    section = config_file.get_section(domain)
    if section is not None:
        AuthConfig.read(section=domain)
        config = AuthConfiguration(dict(AuthConfig))
        AuthConfig.reset()
    else:
        config = AuthConfiguration(dict(AuthConfig))  # use auth defaults
    return config
