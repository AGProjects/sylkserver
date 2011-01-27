# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

from application.configuration import ConfigSection, ConfigSetting


class ConferenceConfig(ConfigSection):
    __cfgfile__ = 'conference.ini'
    __section__ = 'Conference'

    db_uri = ConfigSetting(type=str, value='sqlite:///var/lib/sylkserver/conference.sqlite')
    history_table = ConfigSetting(type=str, value='message_history')
    enable_sip_message = False
    replay_history = 20


