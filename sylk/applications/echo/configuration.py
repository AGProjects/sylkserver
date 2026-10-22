
from application.configuration import ConfigSection, ConfigSetting

from sylk.configuration.datatypes import Path

__all__ = 'EchoConfig',


class EchoConfig(ConfigSection):
    __cfgfile__ = 'echo.ini'
    __section__ = 'Echo'

    rtp_statistics_enabled = False
    rtp_statistics_log = ConfigSetting(type=Path, value=Path('/var/log/sylkserver/echo/rtp.log'))
    rtp_statistics_interval = 5
