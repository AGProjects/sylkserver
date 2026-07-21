
import os
import re

from application.configuration import ConfigFile, ConfigSection, ConfigSetting
from application.configuration.datatypes import (HostnameList, NetworkAddress,
                                                 StringList)

from sylk.configuration import ServerConfig
from sylk.configuration.datatypes import (URL, Path, SIPProxyAddress,
                                          VideoBitrate, VideoCodec)
from sylk.resources import VarResources

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


# Special keyword for http(s)_management_interface: instead of starting a
# standalone listener, mount the admin/management API on the main SylkServer
# web server (the [WebServer] section of config.ini) under
# /webrtcgateway/admin. The main server determines the scheme: HTTPS when a
# certificate is configured there, plain HTTP otherwise.
BUILTIN_WEBSERVER = 'builtin_webserver'


class ManagementInterfaceSetting(object):
    """Parse a host[:port] network address or the keyword 'builtin_webserver'."""

    def __new__(cls, value):
        if isinstance(value, str) and value.strip().lower() == BUILTIN_WEBSERVER:
            return BUILTIN_WEBSERVER
        return ManagementInterfaceAddress(value)


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

class ApplicationConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'General'

    application_dir = ConfigSetting(type=Path, value=Path(VarResources.get('lib/sylkserver/webrtcgateway')))


class GeneralConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'General'

    web_origins = ConfigSetting(type=StringList, value=['*'])
    sip_domains = ConfigSetting(type=StringList, value=['*'])
    outbound_sip_proxy = ConfigSetting(type=SIPProxyAddress, value=None)
    trace_client = False
    websocket_ping_interval = 120
    application_dir = ApplicationConfig.application_dir
    recording_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'videoconference', 'recordings')))
    filesharing_dir = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.spool_dir.normalized, 'videoconference', 'files')))
    file_transfer_dir = ConfigSetting(type=Path, value=Path(os.path.join(ApplicationConfig.application_dir.normalized, 'file_transfers')))
    http_management_interface = ConfigSetting(type=ManagementInterfaceSetting, value=ManagementInterfaceAddress('127.0.0.1'))
    http_management_auth_secret = ConfigSetting(type=str, value=None)
    # Credentials for the browser-based admin UI served at / on the
    # management interface. When both are set, the UI login form accepts
    # them and issues a session cookie; that cookie also authorises the
    # /rooms* JSON endpoints. Leave unset to disable UI login. The
    # shared-secret header auth (http_management_auth_secret) is
    # unaffected and keeps working for tooling like sip-janus-bridge.
    http_management_admin_username = ConfigSetting(type=str, value=None)
    http_management_admin_password = ConfigSetting(type=str, value=None)
    # The admin/management interface runs TWO independent listeners:
    #
    #   * http_management_interface  — always plain HTTP. Used by internal
    #     tooling (sip-janus-bridge, the audio bridge's /rooms/events SSE
    #     subscription, etc.) that talk to the gateway over a trusted
    #     network. Keep this bound to an internal/private address.
    #
    #   * https_management_interface — optional HTTPS, for reaching the
    #     browser admin UI over the internet. Uses the same certificate as
    #     the main web/WebSocket server (WebServerConfig.certificate /
    #     certificate_chain). Unset (None) disables the HTTPS listener.
    #
    # Both serve the same routes; pick a different port for the HTTPS one.
    #
    # Either setting also accepts the keyword 'builtin_webserver': no
    # standalone listener is started for it; instead the admin API is
    # mounted on the main SylkServer web server ([WebServer] in config.ini)
    # under /webrtcgateway/admin. The main server's TLS configuration
    # decides the scheme, so setting the keyword on both is equivalent to
    # setting it on one. Because the main web server is typically public,
    # the mount is refused unless authentication is configured
    # (http_management_auth_secret and/or admin username+password).
    https_management_interface = ConfigSetting(type=ManagementInterfaceSetting, value=None)
    # UDP listener for real-time audio-level updates pushed by a remote
    # conference focus (see sylk.applications.conference.audio_level_udp).
    # host:port; set to empty to disable. Pairs with the conference's
    # audio_level_udp_targets setting. Server-to-server only.
    audio_level_udp_listen = ConfigSetting(type=ManagementInterfaceAddress,
                                           value=ManagementInterfaceAddress('0.0.0.0:11000'))
    # Shared secret expected on inbound UDP datagrams. Defaults to
    # http_management_auth_secret. Datagrams that don't carry a matching
    # token are dropped silently (UDP, anyone could spoof — packets must
    # be authenticated even on a "trusted" network).
    audio_level_udp_token = ConfigSetting(type=str, value=None)
    # How often (seconds) the gateway emits a per-room summary log line
    # mirroring the conference focus's audio-levels log. Aggregates the
    # 4Hz UDP stream into one log line per room per period (avg/peak
    # per participant). Set to 0 to disable. Default 5s — same cadence
    # the conference uses for its own audio_level_log_period.
    audio_level_log_period = 5
    sylk_push_url = ConfigSetting(type=str, value=None)
    xcap_url = ConfigSetting(type=URL, value='')
    local_sip_messages = False
    filetransfer_expire_days = 15


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


class FileStorageConfig(ConfigSection):
    __cfgfile__ = 'webrtcgateway.ini'
    __section__ = 'FileStorage'

    storage_dir = ConfigSetting(type=Path, value=Path(os.path.join(GeneralConfig.application_dir.normalized, 'storage')))


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
    persistent = False


class VideoroomConfiguration(object):
    video_codec = 'vp9'
    max_bitrate = 2016000
    record = False
    recording_dir = None
    filesharing_dir = None
    # Janus videoroom audio-level detection. With these enabled Janus
    # inspects the RTP audio-level extension and emits talking /
    # stopped-talking events (carrying audio-level-dBov-avg) per
    # publisher, which the gateway surfaces as per-WebRTC-participant
    # speaker activity in the admin UI. Independent of the SIP conference
    # focus's UDP level feed, so it also works for pure-WebRTC rooms.
    #   audio_active_packets: packets averaged before deciding (Janus default 100)
    #   audio_level_average:  avg level threshold 0..127, 127=silence (Janus default 25)
    audiolevel_ext = True
    audiolevel_event = True
    audio_active_packets = 100
    audio_level_average = 25

    def __init__(self, data):
        self.__dict__.update(data)

    @property
    def janus_data(self):
        return dict(videocodec=self.video_codec, bitrate=self.max_bitrate, record=self.record, rec_dir=self.recording_dir,
                    audiolevel_ext=self.audiolevel_ext, audiolevel_event=self.audiolevel_event,
                    audio_active_packets=self.audio_active_packets, audio_level_average=self.audio_level_average)


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
