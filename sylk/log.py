
import inspect
import logging
import os

from abc import ABCMeta, abstractproperty
from application import log
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import MarkerType, Singleton
from application.system import makedirs
from collections import defaultdict
from itertools import chain
from sipsimple.threading import run_in_thread
from zope.interface import implementer

from sylk.applications import find_applications
from sylk.configuration import ServerConfig


__all__ = 'TraceLogManager', 'TraceLogger'

# Override emit() from logging.StreamHandler
def shemit(self, record):
    try:
        msg = self.format(record)
        stream = self.stream
        if type(msg) == bytes:
            try:
                msg = msg.decode()
            except UnicodeDecodeError:
                msg = msg.decode(errors='replace')
        stream.write(msg + self.terminator)
        self.flush()
    except RecursionError:
        raise
    except Exception:
        self.handleError(record)
setattr(logging.StreamHandler, 'emit', shemit)


@implementer(IObserver)
class TraceLogManager(object, metaclass=Singleton):

    def __init__(self):
        self.loggers = set()  # todo: make it a list to preserve their order?
        self.notification_map = defaultdict(set)  # maps notification names to trace loggers that are interested in them
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        directory = ServerConfig.trace_dir.normalized
        try:
            makedirs(directory)
        except Exception as e:
            log.error('Failed to create tracelog directory at {directory}: {exception!s}'.format(directory=directory, exception=e))
        else:
            for logger in (logger for logger in self.loggers if logger.enabled):
                logger.start()
                for name in logger.handled_notifications:
                    self.notification_map[name].add(logger)
            if self.notification_map:
                notification_center = NotificationCenter()
                notification_center.add_observer(self)
                log.info('TraceLogManager started in {} for: {}'.format(directory, ', '.join(sorted(logger.name for logger in self.loggers if logger.enabled))))
            else:
                log.info('TraceLogManager started in {}'.format(directory))

    def stop(self):
        if not self.started:
            return
        notification_center = NotificationCenter()
        notification_center.discard_observer(self)
        self.notification_map.clear()
        for logger in self.loggers:
            logger.stop()
        self.started = False

    @classmethod
    def register_logger(cls, logger):  # this is a class method for convenience
        if inspect.isclass(logger) and issubclass(logger, TraceLogger):
            logger = logger()

        assert isinstance(logger, TraceLogger), 'logger must be a TraceLogger instance or class'

        self = cls()

        if logger in self.loggers:
            return

        self.loggers.add(logger)
        if self.started and logger.enabled:
            logger.start()
            for name in logger.handled_notifications:
                self.notification_map[name].add(logger)
            if self.notification_map:
                notification_center = NotificationCenter()
                notification_center.add_observer(self)
            log.info('TraceLogManager added {logger.name} logger for {logger.owner}'.format(logger=logger))

    @run_in_thread('file-logging')
    def handle_notification(self, notification):
        for logger in self.notification_map.get(AllNotifications, ()):
            logger.handle_notification(notification)
        for logger in self.notification_map.get(notification.name, ()):
            handler = getattr(logger, '_NH_{notification.name}'.format(notification=notification), Null)
            handler(notification)


class TraceLoggerType(ABCMeta, Singleton):
    def __init__(cls, name, bases, dic):
        super(TraceLoggerType, cls).__init__(name, bases, dic)
        if not inspect.isabstract(cls):
            # noinspection PyTypeChecker
            TraceLogManager.register_logger(cls)


class TraceLogger(object, metaclass=TraceLoggerType):
    """
    Abstract class that defines the interface for TraceLogger objects.

    A trace logger is created by subclassing TraceLogger and defining the
    name, owner, enabled and formatter class attributes and any number of
    notification handlers, which are instance methods that have a name
    starting with _NH_ followed by the notification name and receive a
    notification as sole argument.
    
    def _NH_SomeNotificationName(self, notification):
        self.logger.log_notification(notification)

    def _NH_SomeOtherNotificationName(self, notification):
        if some_condition:
            self.logger.log_notification(notification)

    Any non-abstract TraceLogger is automatically registered with the
    TraceLogManager and becomes operational if enabled.
    """

    name = abstractproperty()       # The name of this trace logger (should be the log filename without the .log extension)
    owner = abstractproperty()      # The name of the application that owns this trace logger
    enabled = abstractproperty()    # Boolean indicating if this trace logger is enabled (usually mirrors a configuration setting)
    formatter = abstractproperty()  # The formatter used by this trace logger

    def __init__(self):
        self.logger = None

    @property
    def handled_notifications(self):
        return {name[4:] for name in dir(self.__class__) if name.startswith('_NH_') and callable(getattr(self, name))}

    # noinspection PyTypeChecker
    def start(self):
        if self.enabled and self.logger is None:
            self.logger = NotificationLogger(self.name, ServerConfig.trace_dir.normalized, self.formatter)

    def stop(self):
        self.logger = None

    def handle_notification(self, notification):
        self.logger.log_notification(notification)


class AllNotifications(metaclass=MarkerType):
    pass


class NotificationLogger(object):
    def __init__(self, name, directory, formatter):
        self.name = name
        self.filename = os.path.join(directory, name + '.log')
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.FileHandler(self.filename)
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def log_notification(self, notification):
        self.logger.info('', extra=dict(notification=notification))


# The formatters for the trace loggers
#

class NetworkAddress(object):
    def __init__(self, address):
        self.type = address.type
        self.host = address.host
        self.port = address.port

    def __str__(self):
        return '{0.host}:{0.port}'.format(self)


class DNSTraceFormatter(logging.Formatter):
    _format_success = '{time} DNS lookup {data.query_type} {data.query_name} succeeded: TTL={data.answer.ttl} {answer!s}'
    _format_failure = '{time} DNS lookup {data.query_type} {data.query_name} failed: {data.error!s}'

    def format(self, record):
        notification = record.notification
        if notification.data.error is None:
            if notification.data.query_type == 'A':
                answer = ' | '.join(record.address for record in notification.data.answer)
            elif notification.data.query_type == 'SRV':
                answer = ' | '.join('{0.priority} {0.weight} {0.port} {0.target}'.format(dns_record) for dns_record in notification.data.answer)
            elif notification.data.query_type == 'NAPTR':
                answer = ' | '.join('{0.order} {0.preference} {0.flags!r} {0.service!r} {0.regexp!r} {0.replacement}'.format(dns_record) for dns_record in notification.data.answer)
            else:
                answer = ''
            return self._format_success.format(time=notification.datetime, data=notification.data, answer=answer)
        else:
            return self._format_failure.format(time=notification.datetime, data=notification.data)


class SIPTraceFormatter(logging.Formatter):
    _format = '{time} Packet {packet} {direction} {data.transport} {data.source_ip}:{data.source_port} -> {data.destination_ip}:{data.destination_port}\n{sip_packet}\n'
    _packet = 0

    def format(self, record):
        self._packet += 1
        notification = record.notification
        direction = 'INCOMING' if notification.data.received else 'OUTGOING'
        return self._format.format(time=notification.datetime, packet=self._packet, direction=direction, data=notification.data, sip_packet=notification.data.data.decode())


# noinspection PyPep8Naming,PyMethodMayBeStatic
class MSRPTraceFormatter(logging.Formatter):
    _packet = 0

    def format(self, record):
        handler = getattr(self, '_FH_{}'.format(record.notification.name), Null)
        return handler(record)

    def _FH_MSRPLibraryLog(self, record):
        return '{notification.datetime} {notification.data.level!s} {notification.data.message}'.format(notification=record.notification)

    def _FH_MSRPTransportTrace(self, record):
        self._packet += 1
        notification = record.notification
        local_address = NetworkAddress(notification.data.local_address)
        remote_address = NetworkAddress(notification.data.remote_address)
        if notification.data.direction == 'incoming':
            _format = '{time} Packet {packet} INCOMING {local_address} <- {remote_address}\n{data}\n'
        else:
            _format = '{time} Packet {packet} OUTGOING {local_address} -> {remote_address}\n{data}\n'
        return _format.format(time=notification.datetime, packet=self._packet, local_address=local_address, remote_address=remote_address, data=notification.data.data)


class CoreTraceFormatter(logging.Formatter):
    _format = '{data.message}'  # todo: include data.level?

    def format(self, record):
        # return self._format.format(data=record.notification.data)
        return record.notification.data.message


class NotificationTraceFormatter(logging.Formatter):
    _format = '{notification.datetime} Notification name={notification.name} sender={notification.sender} data={notification.data}'

    def format(self, record):
        return self._format.format(notification=record.notification)


# The trace loggers
#

class DNSTraceLogger(TraceLogger):
    name = 'dns_trace'
    owner = 'core'
    enabled = ServerConfig.trace_dns
    formatter = DNSTraceFormatter()

    def _NH_DNSLookupTrace(self, notification):
        self.logger.log_notification(notification)


class SIPTraceLogger(TraceLogger):
    name = 'sip_trace'
    owner = 'core'
    enabled = ServerConfig.trace_sip
    formatter = SIPTraceFormatter()

    def _NH_SIPEngineSIPTrace(self, notification):
        self.logger.log_notification(notification)


class MSRPTraceLogger(TraceLogger):
    name = 'msrp_trace'
    owner = 'core'
    enabled = ServerConfig.trace_msrp
    formatter = MSRPTraceFormatter()

    def _NH_MSRPTransportTrace(self, notification):
        self.logger.log_notification(notification)

    def _NH_MSRPLibraryLog(self, notification):
        if notification.data.level >= logging.INFO:  # use logging.WARNING here?
            self.logger.log_notification(notification)


class CoreTraceLogger(TraceLogger):
    name = 'core_trace'
    owner = 'core'
    enabled = ServerConfig.trace_core
    formatter = CoreTraceFormatter()

    def _NH_SIPEngineLog(self, notification):
        self.logger.log_notification(notification)


class NotificationTraceLogger(TraceLogger):
    name = 'notification_trace'
    owner = 'core'
    enabled = ServerConfig.trace_notifications
    formatter = NotificationTraceFormatter()

    @property
    def handled_notifications(self):
        return {AllNotifications}

    def handle_notification(self, notification):
        if notification.name not in ('SIPEngineLog', 'SIPEngineSIPTrace'):
            self.logger.log_notification(notification)


# Customize the default logging formatter
#
core_name = 'sylk.core'

root_logger = log.get_logger()
root_logger.name = core_name
log.Formatter.prefix_format = '{record.levelname:<8s} [{record.name}] '
log.Formatter.prefix_length = max(len(name) for name in chain(find_applications(), [core_name])) + 8 + 4  # max name length + max level name length + 2 square brackets + 2 spaces
