
import logging

from sylk.applications import ApplicationLogger
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.log import TraceLogger


__all__ = 'log',


log = ApplicationLogger(__package__)


class XMPPTraceFormatter(logging.Formatter):
    _format = '{time} Packet {packet} {data.direction}\n{data.message}\n'
    _packet = 0

    def format(self, record):
        self._packet += 1
        notification = record.notification
        return self._format.format(time=notification.datetime, packet=self._packet, data=notification.data)


class XMPPTraceLogger(TraceLogger):
    name = 'xmpp_trace'
    owner = 'xmppgateway'
    enabled = XMPPGatewayConfig.trace_xmpp
    formatter = XMPPTraceFormatter()

    def _NH_XMPPMessageTrace(self, notification):
        self.logger.log_notification(notification)
