# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

__all__ = ['get_room_configuration']

from application.configuration import ConfigSection, ConfigSetting
from application.configuration.datatypes import EndpointAddress


def get_room_configuration(room):
    IRCConferenceConfig.read(section=room)
    config = Configuration(dict(IRCConferenceConfig))
    IRCConferenceConfig.reset()
    return config


class Configuration(object):
    def __init__(self, data):
        self.__dict__.update(data)

class IRCServer(EndpointAddress):
    default_port = 6667
    name = 'IRC server address'

class IRCConferenceConfig(ConfigSection):
    __cfgfile__ = 'ircconference.ini'

    channel = 'test'
    server = ConfigSetting(type=IRCServer, value='irc.freenode.net:6667')

