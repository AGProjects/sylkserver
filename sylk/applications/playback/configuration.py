# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

__all__ = ['get_file_for_uri']

import os

from application.configuration import ConfigFile, ConfigSection, ConfigSetting

from sylk.configuration.datatypes import Path, ResourcePath


class GeneralConfig(ConfigSection):
    __cfgfile__ = 'playback.ini'
    __section__ = 'Playback'

    files_dir = ConfigSetting(type=Path, value=ResourcePath('sounds/playback').normalized)


class PlaybackConfig(ConfigSection):
    __cfgfile__ = 'playback.ini'

    file = ConfigSetting(type=Path, value=None)


def get_file_for_uri(uri):
    config_file = ConfigFile(PlaybackConfig.__cfgfile__)
    section = config_file.get_section(uri)
    if section is not None:
        PlaybackConfig.read(section=uri)
        if not os.path.isabs(PlaybackConfig.file):
            f = os.path.join(GeneralConfig.files_dir, PlaybackConfig.file)
        else:
            f = PlaybackConfig.file
        PlaybackConfig.reset()
        return f
    return None

