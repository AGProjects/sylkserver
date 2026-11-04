
import os

from application.configuration import ConfigSection, ConfigSetting

from sylk.configuration import ServerConfig
from sylk.configuration.datatypes import Path

__all__ = 'EchoConfig',


class EchoConfig(ConfigSection):
    __cfgfile__ = 'echo.ini'
    __section__ = 'Echo'

    rtp_statistics_enabled = False
    rtp_statistics_log = ConfigSetting(type=Path, value=Path(os.path.join(ServerConfig.log_dir.normalized, 'echo', 'rtp.log')))
    rtp_statistics_interval = 5
