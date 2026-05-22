
import os
import re

from application.configuration import ConfigFile, ConfigSection, ConfigSetting
from application.configuration.datatypes import NetworkAddress, StringList

from sylk.configuration import ServerConfig
from sylk.configuration.datatypes import Path, URL


__all__ = 'ConferenceConfig', 'get_room_config'


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
    # http_management_auth_secret as a bearer-style Authorization header.
    #
    # Set http_management_interface to '' (empty) to disable the admin
    # interface entirely.
    http_management_interface = ConfigSetting(type=ManagementInterfaceAddress,
                                              value=ManagementInterfaceAddress('127.0.0.1:10889'))
    http_management_auth_secret = ConfigSetting(type=str, value=None)

    # How often the audio levels of each participant are sampled, in
    # milliseconds. The result is published to subscribers of the admin
    # API's SSE stream and made available as a snapshot via the snapshot
    # endpoint. Set to 0 to disable the periodic sampling entirely.
    audio_level_sample_period = 100


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

