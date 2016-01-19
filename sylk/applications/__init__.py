
__all__ = ['ISylkApplication', 'ApplicationRegistry', 'SylkApplication', 'IncomingRequestHandler', 'ApplicationLogger']

import abc
import os
import socket
import struct
import sys

from collections import defaultdict

from application import log
from application.configuration.datatypes import NetworkRange
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from itertools import chain
from sipsimple.threading import run_in_twisted_thread
from zope.interface import implements

from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig


SYLK_APP_HEADER = 'X-Sylk-App'


class ApplicationRegistry(object):
    __metaclass__ = Singleton

    def __init__(self):
        self.applications = []

    def __iter__(self):
        return iter(self.applications)

    def find_application(self, name):
        try:
            return next(app for app in self.applications if app.__appname__ == name)
        except StopIteration:
            return None

    def add(self, app):
        if app not in self.applications:
            self.applications.append(app)


class ApplicationName(object):
    def __get__(self, obj, objtype):
        name = objtype.__name__
        return name[:-11].lower() if name.endswith('Application') else name.lower()


class SylkApplicationMeta(abc.ABCMeta, Singleton):
    """Metaclass for defining SylkServer applications: a Singleton that also adds them to the application registry"""
    def __init__(cls, name, bases, dic):
        super(SylkApplicationMeta, cls).__init__(name, bases, dic)
        if name != 'SylkApplication':
            ApplicationRegistry().add(cls)


class SylkApplication(object):
    """Base class for all SylkServer applications"""

    __metaclass__ = SylkApplicationMeta
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


def load_builtin_applications():
    toplevel = os.path.dirname(__file__)
    app_list = [item for item in os.listdir(toplevel) if os.path.isdir(os.path.join(toplevel, item)) and '__init__.py' in os.listdir(os.path.join(toplevel, item))]
    for module in ['sylk.applications.%s' % item for item in set(app_list).difference(ServerConfig.disabled_applications)]:
        try:
            __import__(module)
        except ImportError, e:
            log.warning('Error loading builtin "%s" application: %s' % (module, e))

def load_extra_applications():
    if ServerConfig.extra_applications_dir:
        toplevel = os.path.realpath(os.path.abspath(ServerConfig.extra_applications_dir.normalized))
        if os.path.isdir(toplevel):
            app_list = [item for item in os.listdir(toplevel) if os.path.isdir(os.path.join(toplevel, item)) and '__init__.py' in os.listdir(os.path.join(toplevel, item))]
            sys.path.append(toplevel)
            for module in (item for item in set(app_list).difference(ServerConfig.disabled_applications)):
                try:
                    __import__(module)
                except ImportError, e:
                    log.warning('Error loading extra "%s" application: %s' % (module, e))

def load_applications():
    load_builtin_applications()
    load_extra_applications()
    for app in ApplicationRegistry():
        try:
            app()
        except Exception, e:
            log.warning('Error loading application: %s' % e)
            log.err()


class ApplicationNotLoadedError(Exception):
    pass

class IncomingRequestHandler(object):
    """
    Handle incoming requests and match them to applications.
    """
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        load_applications()
        registry = ApplicationRegistry()
        self.applications = dict((app.__appname__, app) for app in registry)
        log.msg('Loaded applications: %s' % ', '.join(self.applications.keys()))
        default_application = registry.find_application(ServerConfig.default_application)
        if default_application is None:
            log.warning('Default application "%s" does not exist, falling back to "conference"' % ServerConfig.default_application)
            ServerConfig.default_application = 'conference'
        else:
            log.msg('Default application: %s' % ServerConfig.default_application)
        self.application_map = dict((item.split(':')) for item in ServerConfig.application_map)
        if self.application_map:
            txt = 'Application map:\n'
            invert_app_map = defaultdict(list)
            for url, app in self.application_map.iteritems():
                invert_app_map[app].append(url)
            for app, urls in invert_app_map.iteritems():
                txt += '    * %s:\n' % app
                for url in urls:
                    txt += '        - %s\n' % url
            log.msg(txt[:-1])
        self.authorization_handler = AuthorizationHandler()

    def start(self):
        for app in ApplicationRegistry():
            try:
                app().start()
            except Exception, e:
                log.warning('Error starting application: %s' % e)
                log.err()
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
        for app in ApplicationRegistry():
            try:
                app().stop()
            except Exception, e:
                log.warning('Error stopping application: %s' % e)
                log.err()

    def get_application(self, ruri, headers):
        if SYLK_APP_HEADER in headers:
            application = headers[SYLK_APP_HEADER].body.strip()
        else:
            application = ServerConfig.default_application
            if self.application_map:
                prefixes = ("%s@%s" % (ruri.user, ruri.host), ruri.host, ruri.user)
                for prefix in prefixes:
                    if prefix in self.application_map:
                        application = self.application_map[prefix]
                        break
        try:
            app = self.applications[application]
        except KeyError:
            log.error('Application %s is not loaded' % application)
            raise ApplicationNotLoadedError
        else:
            return app()

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

class AuthorizationHandler(object):
    implements(IObserver)

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
            if struct.unpack('!L', socket.inet_aton(ip_address))[0] & range[1] == range[0]:
                return True
        raise UnauthorizedRequest

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ThorNetworkGotUpdate(self, notification):
        thor_nodes = []
        for node in chain(*(n.nodes for n in notification.data.networks.values())):
            thor_nodes.append(NetworkRange(node))
        self.thor_nodes = thor_nodes


class ApplicationLogger(object):
    __metaclass__ = Singleton

    @classmethod
    def for_package(cls, package):
        return cls(package.split('.')[-1])

    def __init__(self, prefix):
        self.prefix = '[%s] ' % prefix

    def info(self, message, **context):
        log.info(self.prefix+message, **context)

    def warning(self, message, **context):
        log.warning(self.prefix+message, **context)

    def debug(self, message, **context):
        log.debug(self.prefix+message, **context)

    def error(self, message, **context):
        log.error(self.prefix+message, **context)

    def critical(self, message, **context):
        log.critical(self.prefix+message, **context)

    def exception(self, message=None, **context):
        if message is not None:
            message = self.prefix+message
        log.exception(message, **context)

    # Some aliases that are commonly used
    msg = info
    warn = warning
    fatal = critical
    err = exception


