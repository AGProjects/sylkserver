# Copyright (C) 2010-2011 AG Projects. See LICENSE for details
#

__all__ = ['ISylkApplication', 'ApplicationRegistry', 'sylk_application', 'IncomingRequestHandler']

import os
import socket
import struct

from application import log
from application.configuration.datatypes import NetworkRange
from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from itertools import chain
from sipsimple.threading import run_in_twisted_thread
from zope.interface import Attribute, Interface, implements

from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig


class ISylkApplication(Interface):
    """
    Interface defining attributes and methods any application must 
    implement.

    Each application must be a Singleton and has to be decorated with
    the @sylk_application decorator.
    """

    __appname__ = Attribute("Application name")

    def incoming_session(self, session):
        pass

    def incoming_subscription(self, subscribe_request, data):
        pass

    def incoming_referral(self, refer_request, data):
        pass

    def incoming_sip_message(self, message_request, data):
        pass


class ApplicationRegistry(object):
    __metaclass__ = Singleton

    def __init__(self):
        self.applications = []

    def __iter__(self):
        return iter(self.applications)

    def add(self, app):
        if app not in self.applications:
            self.applications.append(app)


def sylk_application(cls):
    """Class decorator for adding applications to the ApplicationRegistry"""
    ApplicationRegistry().add(cls())
    return cls


def load_applications():
    toplevel = os.path.dirname(__file__)
    app_list = ['sylk.applications.%s' % item for item in os.listdir(toplevel) if os.path.isdir(os.path.join(toplevel, item)) and '__init__.py' in os.listdir(os.path.join(toplevel, item))]
    map(__import__, app_list)


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
        log.msg('Loaded applications: %s' % ', '.join([app.__appname__ for app in ApplicationRegistry()]))
        self.application_map = dict((item.split(':')) for item in ServerConfig.application_map)
        self.authorization_handler = AuthorizationHandler()

    def start(self):
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

    def get_application(self, uri):
        application = self.application_map.get(uri.user, ServerConfig.default_application)
        try:
            app = (app for app in ApplicationRegistry() if app.__appname__ == application).next()
        except StopIteration:
            log.error('Application %s is not loaded' % application)
            raise ApplicationNotLoadedError
        else:
            return app

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
            app = self.get_application(session._invitation.request_uri)
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
            app = self.get_application(notification.data.request_uri)
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
            app = self.get_application(notification.data.request_uri)
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
            app = self.get_application(notification.data.request_uri)
        except ApplicationNotLoadedError:
            request.answer(404)
        else:
            app.incoming_sip_message(request, notification.data)


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
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='ThorNetworkGotUpdate')
        self.state = 'started'

    def stop(self):
        self.state = 'stopped'
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='ThorNetworkGotUpdate')

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


