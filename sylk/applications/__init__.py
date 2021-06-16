
import abc
import imp
import logging
import os
import socket
import struct
import sys

from application import log
from application.configuration.datatypes import NetworkRange
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.decorator import execute_once
from application.python.types import Singleton
from collections import defaultdict
from itertools import chain
from sipsimple.threading import run_in_twisted_thread
from zope.interface import implementer

from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig


__all__ = 'ISylkApplication', 'ApplicationRegistry', 'SylkApplication', 'IncomingRequestHandler', 'ApplicationLogger'


SYLK_APP_HEADER = 'X-Sylk-App'


def find_builtin_applications():
    applications_directory = os.path.dirname(__file__)
    for path, dirs, files in os.walk(applications_directory):
        parent_directory, name = os.path.split(path)
        if parent_directory == applications_directory and '__init__.py' in files and name not in ServerConfig.disabled_applications:
            yield name
        if path != applications_directory:
            del dirs[:]  # do not descend more than 1 level


def find_extra_applications():
    if ServerConfig.extra_applications_dir:
        applications_directory = os.path.realpath(ServerConfig.extra_applications_dir.normalized)
        for path, dirs, files in os.walk(applications_directory):
            parent_directory, name = os.path.split(path)
            if parent_directory == applications_directory and '__init__.py' in files and name not in ServerConfig.disabled_applications:
                yield name
            if path != applications_directory:
                del dirs[:]  # do not descend more than 1 level


def find_applications():
    return chain(find_builtin_applications(), find_extra_applications())


class ApplicationRegistry(object, metaclass=Singleton):
    def __init__(self):
        self.application_map = {}

    def __getitem__(self, name):
        return self.application_map[name]

    def __contains__(self, name):
        return name in self.application_map

    def __iter__(self):
        return iter(list(self.application_map.values()))

    def __len__(self):
        return len(self.application_map)

    #@execute_once
    def load_applications(self):
        for name in find_builtin_applications():
            try:
                __import__('sylk.applications.{name}'.format(name=name))
            except ImportError as e:
                log.error('Failed to load builtin application {name!r}: {exception!s}'.format(name=name, exception=e))
        for name in find_extra_applications():
            if name in sys.modules:
                # being able to log this is contingent on this function only executing once
                log.warning('Not loading extra application {name!r} as it would overshadow a system package/module'.format(name=name))
                continue
            try:
                imp.load_module(name, *imp.find_module(name, [ServerConfig.extra_applications_dir.normalized]))
            except ImportError as e:
                log.error('Failed to load extra application {name!r}: {exception!s}'.format(name=name, exception=e))

    def add(self, app_class):
        try:
            app = app_class()
        except Exception as e:
            log.exception('Failed to initialize {app.__appname__!r} application: {exception!s}'.format(app=app_class, exception=e))
        else:
            self.application_map[app.__appname__] = app

    def get(self, name, default=None):
        return self.application_map.get(name, default)


class ApplicationName(object):
    def __get__(self, instance, instance_type):
        name = instance_type.__name__
        return name[:-11].lower() if name.endswith('Application') else name.lower()


class SylkApplicationMeta(abc.ABCMeta, Singleton):
    """Metaclass for defining SylkServer applications: a Singleton that also adds them to the application registry"""

    def __init__(cls, name, bases, dic):
        super(SylkApplicationMeta, cls).__init__(name, bases, dic)
        if name != 'SylkApplication':
            ApplicationRegistry().add(cls)


class SylkApplication(object, metaclass=SylkApplicationMeta):
    """Base class for all SylkServer applications"""
    __appname__ = ApplicationName()

    @abc.abstractmethod
    def start(self):
        pass

    @abc.abstractmethod
    def stop(self):
        pass

    @abc.abstractmethod
    def incoming_session(self, session):
        pass

    @abc.abstractmethod
    def incoming_subscription(self, subscribe_request, data):
        pass

    @abc.abstractmethod
    def incoming_referral(self, refer_request, data):
        pass

    @abc.abstractmethod
    def incoming_message(self, message_request, data):
        pass


class ApplicationNotLoadedError(Exception):
    pass


@implementer(IObserver)
class IncomingRequestHandler(object, metaclass=Singleton):
    """Handle incoming requests and match them to applications"""

    def __init__(self):
        self.application_registry = ApplicationRegistry()
        self.application_registry.load_applications()
        log.info('Loaded applications: {}'.format(', '.join(sorted(app.__appname__ for app in self.application_registry))))
        if ServerConfig.default_application not in self.application_registry:
            log.warning('Default application "%s" does not exist, falling back to "conference"' % ServerConfig.default_application)
            ServerConfig.default_application = 'conference'
        else:
            log.info('Default application: %s' % ServerConfig.default_application)
        self.application_map = dict((item.split(':')) for item in ServerConfig.application_map)
        if self.application_map:
            txt = 'Application map:\n'
            inverted_app_map = defaultdict(list)
            for url, app in self.application_map.items():
                inverted_app_map[app].append(url)
            for app, urls in inverted_app_map.items():
                txt += '  {}: {}\n'.format(app, ', '.join(urls))
            log.info(txt[:-1])
        self.authorization_handler = AuthorizationHandler()

    def start(self):
        for app in self.application_registry:
            try:
                app.start()
            except Exception as e:
                log.exception('Failed to start {app.__appname__!r} application: {exception!s}'.format(app=app, exception=e))
        self.authorization_handler.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionNewIncoming')
        notification_center.add_observer(self, name='SIPIncomingSubscriptionGotSubscribe')
        notification_center.add_observer(self, name='SIPIncomingReferralGotRefer')
        notification_center.add_observer(self, name='SIPIncomingRequestGotRequest')

    def stop(self):
        self.authorization_handler.stop()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SIPSessionNewIncoming')
        notification_center.remove_observer(self, name='SIPIncomingSubscriptionGotSubscribe')
        notification_center.remove_observer(self, name='SIPIncomingReferralGotRefer')
        notification_center.remove_observer(self, name='SIPIncomingRequestGotRequest')
        for app in self.application_registry:
            try:
                app.stop()
            except Exception as e:
                log.exception('Failed to stop {app.__appname__!r} application: {exception!s}'.format(app=app, exception=e))

    def get_application(self, ruri, headers):
        if SYLK_APP_HEADER in headers:
            application_name = headers[SYLK_APP_HEADER].body.strip()
        else:
            application_name = ServerConfig.default_application
            if self.application_map:
                prefixes = ("%s@%s" % (ruri.user, ruri.host), ruri.host, ruri.user)
                for prefix in prefixes:
                    if prefix in self.application_map:
                        application_name = self.application_map[prefix]
                        break
        try:
            return self.application_registry[application_name]
        except KeyError:
            log.error('Application %s is not loaded' % application_name)
            raise ApplicationNotLoadedError

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionNewIncoming(self, notification):
        session = notification.sender
        try:
            self.authorization_handler.authorize_source(session.peer_address.ip)
        except UnauthorizedRequest:
            session.reject(403)
            return
        try:
            app = self.get_application(session.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            session.reject(404)
        else:
            app.incoming_session(session)

    def _NH_SIPIncomingSubscriptionGotSubscribe(self, notification):
        subscribe_request = notification.sender
        try:
            self.authorization_handler.authorize_source(subscribe_request.peer_address.ip)
        except UnauthorizedRequest:
            subscribe_request.reject(403)
            return
        try:
            app = self.get_application(notification.data.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            subscribe_request.reject(404)
        else:
            app.incoming_subscription(subscribe_request, notification.data)

    def _NH_SIPIncomingReferralGotRefer(self, notification):
        refer_request = notification.sender
        try:
            self.authorization_handler.authorize_source(refer_request.peer_address.ip)
        except UnauthorizedRequest:
            refer_request.reject(403)
            return
        try:
            app = self.get_application(notification.data.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            refer_request.reject(404)
        else:
            app.incoming_referral(refer_request, notification.data)

    def _NH_SIPIncomingRequestGotRequest(self, notification):
        request = notification.sender
        if notification.data.method != 'MESSAGE':
            request.answer(405)
            return
        try:
            self.authorization_handler.authorize_source(request.peer_address.ip)
        except UnauthorizedRequest:
            request.answer(403)
            return
        try:
            app = self.get_application(notification.data.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            request.answer(404)
        else:
            app.incoming_message(request, notification.data)


class UnauthorizedRequest(Exception):
    pass


@implementer(IObserver)
class AuthorizationHandler(object):

    def __init__(self):
        self.state = None
        self.trusted_peers = SIPConfig.trusted_peers
        self.thor_nodes = []

    @property
    def trusted_parties(self):
        if ThorNodeConfig.enabled:
            return self.thor_nodes
        return self.trusted_peers

    def start(self):
        NotificationCenter().add_observer(self, name='ThorNetworkGotUpdate')
        self.state = 'started'

    def stop(self):
        self.state = 'stopped'
        NotificationCenter().remove_observer(self, name='ThorNetworkGotUpdate')

    def authorize_source(self, ip_address):
        if self.state != 'started':
            raise UnauthorizedRequest
        for range in self.trusted_parties:
            if struct.unpack('!L', socket.inet_aton(ip_address.decode()))[0] & range[1] == range[0]:
                return True
        raise UnauthorizedRequest

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ThorNetworkGotUpdate(self, notification):
        self.thor_nodes = [NetworkRange(node.decode()) for node in chain.from_iterable(n.nodes for n in list(notification.data.networks.values()))]


class ApplicationLogger(object):
    def __new__(cls, package):
        return logging.getLogger(package.split('.')[-1])
