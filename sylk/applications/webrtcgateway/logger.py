
import logging

from application.log import ContextualLogger

from sylk.applications import ApplicationLogger
from sylk.log import TraceLogger

from .configuration import GeneralConfig, JanusConfig


__all__ = 'log', 'ConnectionLogger', 'VideoroomLogger'


log = ApplicationLogger(__package__)


class ConnectionLogger(ContextualLogger):
    def __init__(self, connection):
        super(ConnectionLogger, self).__init__(logger=log)
        self.device_id = connection.device_id
        self.peer=connection.protocol.peer
        self.connection = connection

    def apply_context(self, message):
        try:
            account_id = self.connection.devices_map[self.device_id]
        except KeyError:
            return '[device {0}/{2}] {1}'.format(self.peer, message, self.device_id) if message != '' else ''
        else:
            return '[account {0}/{2}] {1}'.format(account_id, message, self.device_id) if message != '' else ''


class VideoroomLogger(ContextualLogger):
    def __init__(self, videoroom):
        super(VideoroomLogger, self).__init__(logger=log)
        self.room_uri = videoroom.uri

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
    enabled = GeneralConfig.trace_client
    formatter = WebRTCClientTraceFormatter()

    def _NH_WebRTCClientTrace(self, notification):
        self.logger.log_notification(notification)


class WebRTCJanusTraceLogger(TraceLogger):
    name = 'webrtc_janus_trace'
    owner = 'webrtcgateway'
    enabled = JanusConfig.trace_janus
    formatter = WebRTCJanusTraceFormatter()

    def _NH_WebRTCJanusTrace(self, notification):
        self.logger.log_notification(notification)
