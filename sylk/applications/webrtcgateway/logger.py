
import logging
import os

from application.log import ContextualLogger

from sylk.applications import ApplicationLogger
from sylk.log import TraceLogger

from .configuration import GeneralConfig, JanusConfig


__all__ = ('log', 'messaging_log', 'addressbook_log', 'file_transfers_log', 'connections_log', 'LogCategories',
           'ConnectionLogger', 'VideoroomLogger', 'QUIET_CONTENT_TYPES')


log = ApplicationLogger(__package__)


# Log categories, each switched on and off with a log_<category> setting in
# webrtcgateway.ini [General], live (see sylk.configwatch). A disabled
# category logs nothing at all. messaging, addressbook and file_transfers
# write to their own file (<category>_log setting); connections stays in the
# main log.

class _MainLogName(logging.Filter):
    # main log lines of a category keep the [webrtcgateway] prefix
    def filter(self, record):
        record.name = log.name
        return True


class _FileFormatter(logging.Formatter):
    def __init__(self):
        super(_FileFormatter, self).__init__('%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s', '%Y-%m-%d %H:%M:%S')


class LogCategory(object):
    def __init__(self, name, separate):
        self.name = name
        self.separate = separate
        self.logger = logging.getLogger('{}.{}'.format(log.name, name))
        self.logger.addFilter(_MainLogName())
        self.handler = None
        self.filename = None
        self.apply()

    @property
    def enabled(self):
        return getattr(GeneralConfig, 'log_' + self.name)

    @property
    def path(self):
        return getattr(GeneralConfig, self.name + '_log').normalized

    def apply(self):
        self.logger.disabled = not self.enabled
        if not self.separate:
            return
        if not self.enabled:
            self._close()
            return
        if self.handler is not None and self.filename == self.path:
            return
        self._close()
        filename = self.path
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            handler = logging.FileHandler(filename)
        except OSError as e:
            log.warning('Cannot open the {} log {}, logging it here: {}'.format(self.name, filename, e))
            self.logger.propagate = True
            return
        handler.setFormatter(_FileFormatter())
        self.logger.addHandler(handler)
        self.logger.propagate = False
        self.handler, self.filename = handler, filename

    def _close(self):
        if self.handler is not None:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = self.filename = None


class LogCategories(object):
    messaging = LogCategory('messaging', separate=True)
    addressbook = LogCategory('addressbook', separate=True)
    file_transfers = LogCategory('file_transfers', separate=True)
    connections = LogCategory('connections', separate=False)

    all = (messaging, addressbook, file_transfers, connections)

    hot_settings = tuple(['log_' + c.name for c in all] + [c.name + '_log' for c in all if c.separate] + ['log_webrtc_client_api_ping'])

    @classmethod
    def set(cls, key, raw):
        # a hot setting changed in webrtcgateway.ini (raw is None if removed)
        setting = GeneralConfig.__settings__[key]
        try:
            setattr(GeneralConfig, key, setting.type(raw) if raw is not None else GeneralConfig.__defaults__[key])
        except (TypeError, ValueError) as e:
            log.warning('Ignoring invalid {} value {!r}: {}'.format(key, raw, e))
            return
        for category in cls.all:
            if key not in ('log_' + category.name, category.name + '_log'):
                continue
            was_enabled = not category.logger.disabled
            category.apply()
            if category.enabled != was_enabled or (category.enabled and category.separate and key.endswith('_log')):
                log.info('{} logging {}{}'.format(category.name.replace('_', ' '), 'on' if category.enabled else 'off',
                                                    ' to {}'.format(category.path) if category.enabled and category.separate else ''))

    @classmethod
    def describe(cls):
        return ', '.join('{} ({})'.format(c.name, ('' if c.enabled else 'off') or (c.path if c.separate else 'main log')) for c in cls.all)


messaging_log = LogCategories.messaging.logger
addressbook_log = LogCategories.addressbook.logger
file_transfers_log = LogCategories.file_transfers.logger
connections_log = LogCategories.connections.logger

# Message payload types too frequent to log at info level; sending and
# receiving them is logged at debug.
QUIET_CONTENT_TYPES = frozenset({
    'message/imdn+xml',
    'application/sylk-api-conversation-read',
    'application/sylk-conversation-read',
})


class ConnectionLogger(ContextualLogger):
    def __init__(self, connection, logger=log):
        super(ConnectionLogger, self).__init__(logger=logger)
        self.device_id = connection.device_id
        self.peer=connection.protocol.peer
        self.connection = connection
        if logger is log:
            self.messaging = ConnectionLogger(connection, messaging_log)
            self.addressbook = ConnectionLogger(connection, addressbook_log)
            self.file_transfers = ConnectionLogger(connection, file_transfers_log)
            self.connections = ConnectionLogger(connection, connections_log)

    def apply_context(self, message):
        try:
            account_id = self.connection.devices_map[self.device_id]
        except KeyError:
            return '[device {0}] {1}'.format(self.device_id, message) if message != '' else ''
        else:
            return '[account {0}/{2}] {1}'.format(account_id, message, self.device_id) if message != '' else ''


class VideoroomLogger(ContextualLogger):
    def __init__(self, videoroom, logger=log):
        super(VideoroomLogger, self).__init__(logger=logger)
        self.room_uri = videoroom.uri
        if logger is log:
            self.file_transfers = VideoroomLogger(videoroom, file_transfers_log)

    def apply_context(self, message):
        return '[videoroom {0}] {1}'.format(self.room_uri, message) if message != '' else ''


class WebRTCClientTraceFormatter(logging.Formatter):
    _format = '{time} Packet {packet} {data.direction}, client at {data.peer}\n{data.message}\n'
    _packet = 0

    def format(self, record):
        self._packet += 1
        notification = record.notification
        return self._format.format(time=notification.datetime, packet=self._packet, data=notification.data)


class WebRTCJanusTraceFormatter(logging.Formatter):
    _format = '{time} Packet {packet} {data.direction}, janus at {data.peer}\n{data.message}\n'
    _packet = 0

    def format(self, record):
        self._packet += 1
        notification = record.notification
        return self._format.format(time=notification.datetime, packet=self._packet, data=notification.data)


class WebRTCClientTraceLogger(TraceLogger):
    name = 'webrtc_client_trace'
    owner = 'webrtcgateway'
    subdir = 'webrtc'
    enabled = GeneralConfig.log_webrtc_client_api
    formatter = WebRTCClientTraceFormatter()

    def _NH_WebRTCClientTrace(self, notification):
        self.logger.log_notification(notification)


class WebRTCJanusTraceLogger(TraceLogger):
    name = 'webrtc_janus_trace'
    owner = 'webrtcgateway'
    subdir = 'webrtc'
    enabled = JanusConfig.log_janus_api
    formatter = WebRTCJanusTraceFormatter()

    def _NH_WebRTCJanusTrace(self, notification):
        self.logger.log_notification(notification)
