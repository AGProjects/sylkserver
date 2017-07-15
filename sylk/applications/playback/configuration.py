
import os

from application.configuration import ConfigFile, ConfigSection, ConfigSetting

from sylk.configuration.datatypes import Path
from sylk.resources import Resources


__all__ = 'get_config',


class GeneralConfig(ConfigSection):
    __cfgfile__ = 'playback.ini'
    __section__ = 'Playback'

    files_dir = ConfigSetting(type=Path, value=Path(Resources.get('sounds/playback')))
    enable_video = False
    answer_delay = 1


class PlaybackConfig(ConfigSection):
    __cfgfile__ = 'playback.ini'

    file = ConfigSetting(type=Path, value=None)
    enable_video = GeneralConfig.enable_video
    answer_delay = GeneralConfig.answer_delay


class Configuration(object):
    def __init__(self, data):
        self.__dict__.update(data)


def get_config(uri):
    config_file = ConfigFile(PlaybackConfig.__cfgfile__)
    GeneralConfig.read(cfgfile=config_file)
    section = config_file.get_section(uri)
    if section is not None:
        PlaybackConfig.read(section=uri)
        if not os.path.isabs(PlaybackConfig.file):
            PlaybackConfig.file = os.path.join(GeneralConfig.files_dir, PlaybackConfig.file)
        config = Configuration(dict(PlaybackConfig))
        PlaybackConfig.reset()
        return config
    return None
