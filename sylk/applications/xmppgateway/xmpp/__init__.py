# Copyright (C) 2012 AG Projects. See LICENSE for details
#

import os

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from datetime import datetime
from sipsimple.util import Timestamp, TimestampedNotificationData
from twisted.internet import reactor
from twisted.words.protocols.jabber.jid import internJID as JID
from wokkel.component import InternalComponent, Router as _Router
from wokkel.server import ServerService, XMPPS2SServerFactory, DeferredS2SClientFactory
from zope.interface import implements

from sylk.applications import ApplicationLogger
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import FrozenURI
from sylk.applications.xmppgateway.logger import Logger
from sylk.applications.xmppgateway.xmpp.protocols import MessageProtocol, PresenceProtocol
from sylk.applications.xmppgateway.xmpp.session import XMPPChatSessionManager
from sylk.applications.xmppgateway.xmpp.subscription import XMPPSubscriptionManager

log = ApplicationLogger(os.path.dirname(__file__).split(os.path.sep)[-1])


xmpp_logger = Logger()


# Utility classes

class Router(_Router):
    def route(self, stanza):
        """
        Route a stanza. (subclassed to avoid vebose logging)

        @param stanza: The stanza to be routed.
        @type stanza: L{domish.Element}.
        """
        destination = JID(stanza['to'])

        if destination.host in self.routes:
            self.routes[destination.host].send(stanza)
        else:
            self.routes[None].send(stanza)


class XMPPS2SServerFactory(XMPPS2SServerFactory):
    def onConnectionMade(self, xs):
        super(self.__class__, self).onConnectionMade(xs)

        def logDataIn(buf):
            buf = buf.strip()
            if buf:
                xmpp_logger.msg("RECEIVED", Timestamp(datetime.now()), buf)

        def logDataOut(buf):
            buf = buf.strip()
            if buf:
                xmpp_logger.msg("SENDING", Timestamp(datetime.now()), buf)

        if XMPPGatewayConfig.trace_xmpp:
            xs.rawDataInFn = logDataIn
            xs.rawDataOutFn = logDataOut


class DeferredS2SClientFactory(DeferredS2SClientFactory):
    def onConnectionMade(self, xs):
        super(self.__class__, self).onConnectionMade(xs)

        def logDataIn(buf):
            if buf:
                xmpp_logger.msg("RECEIVED", Timestamp(datetime.now()), buf)

        def logDataOut(buf):
            if buf:
                xmpp_logger.msg("SENDING", Timestamp(datetime.now()), buf)

        if XMPPGatewayConfig.trace_xmpp:
            xs.rawDataInFn = logDataIn
            xs.rawDataOutFn = logDataOut

# Patch Wokkel's DeferredS2SClientFactory to use our logger
import wokkel.server
wokkel.server.DeferredS2SClientFactory = DeferredS2SClientFactory
del wokkel.server

# Manager

class XMPPManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        config = XMPPGatewayConfig

        self.stopped = False

        router = Router()
        self._server_service = ServerService(router)
        self._server_service.domains = set(config.domains)
        self._server_service.logTraffic = False    # done manually

        self._s2s_factory = XMPPS2SServerFactory(self._server_service)
        self._s2s_factory.logTraffic = False    # done manually

        self._internal_component = InternalComponent(router)
        self._internal_component.domains = set(config.domains)

        self._message_protocol = MessageProtocol()
        self._message_protocol.setHandlerParent(self._internal_component)

        self._presence_protocol = PresenceProtocol()
        self._presence_protocol.setHandlerParent(self._internal_component)

        self._s2s_listener = None

        self.chat_session_manager = XMPPChatSessionManager()
        self.subscription_manager = XMPPSubscriptionManager()

    def start(self):
        self.stopped = False
        xmpp_logger.start()
        config = XMPPGatewayConfig
        self._s2s_listener = reactor.listenTCP(config.local_port, self._s2s_factory, interface=config.local_ip)
        listen_address = self._s2s_listener.getHost()
        log.msg("XMPP listener started on %s:%d" % (listen_address.host, listen_address.port))
        self.chat_session_manager.start()
        self.subscription_manager.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._internal_component)
        self._internal_component.startService()

    def stop(self):
        self.stopped = True
        self._s2s_listener.stopListening()
        self.subscription_manager.stop()
        self.chat_session_manager.stop()
        self._internal_component.stopService()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._internal_component)
        xmpp_logger.stop()

    def send_stanza(self, stanza):
        if self.stopped:
            return
        self._internal_component.send(stanza.to_xml_element())

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    # Process message stanzas

    def _NH_XMPPGotChatMessage(self, notification):
        message = notification.data.message
        try:
            session = self.chat_session_manager.sessions[(message.recipient.uri, message.sender.uri)]
        except KeyError:
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPGotChatMessage', sender=self, data=notification.data)
        else:
            session.channel.send(message)

    def _NH_XMPPGotNormalMessage(self, notification):
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotNormalMessage', sender=self, data=notification.data)

    def _NH_XMPPGotComposingIndication(self, notification):
        notification_center = NotificationCenter()
        composing_indication = notification.data.composing_indication
        try:
            session = self.chat_session_manager.sessions[(composing_indication.recipient.uri, composing_indication.sender.uri)]
        except KeyError:
            notification_center.post_notification('XMPPGotComposingIndication', sender=self, data=notification.data)
        else:
            session.channel.send(composing_indication)

    def _NH_XMPPGotErrorMessage(self, notification):
        error_message = notification.data.error_message
        try:
            session = self.chat_session_manager.sessions[(error_message.recipient.uri, error_message.sender.uri)]
        except KeyError:
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPGotErrorMessage', sender=self, data=notification.data)
        else:
            session.channel.send(error_message)

    def _NH_XMPPGotReceipt(self, notification):
        receipt = notification.data.receipt
        try:
            session = self.chat_session_manager.sessions[(receipt.recipient.uri, receipt.sender.uri)]
        except KeyError:
            pass
        else:
            session.channel.send(receipt)

    # Process presence stanzas

    def _NH_XMPPGotPresenceAvailability(self, notification):
        stanza = notification.data.presence_stanza
        if stanza.recipient.uri.resource is not None:
            # Skip directed presence
            return
        sender_uri = stanza.sender.uri
        sender_uri_bare = FrozenURI(sender_uri.user, sender_uri.host)
        try:
            subscription = self.subscription_manager.outgoing_subscriptions[(stanza.recipient.uri, sender_uri_bare)]
        except KeyError:
            # Ignore incoming presence stanzas if there is no subscription
            pass
        else:
            subscription.channel.send(stanza)

    def _NH_XMPPGotPresenceSubscriptionStatus(self, notification):
        stanza = notification.data.presence_stanza
        if stanza.sender.uri.resource is not None or stanza.recipient.uri.resource is not None:
            # Skip directed presence
            return
        if stanza.type in ('subscribed', 'unsubscribed'):
            try:
                subscription = self.subscription_manager.outgoing_subscriptions[(stanza.recipient.uri, stanza.sender.uri)]
            except KeyError:
                pass
            else:
                subscription.channel.send(stanza)
        elif stanza.type in ('subscribe', 'unsubscribe'):
            try:
                subscription = self.subscription_manager.incoming_subscriptions[(stanza.recipient.uri, stanza.sender.uri)]
            except KeyError:
                if stanza.type == 'subscribe':
                    notification_center = NotificationCenter()
                    notification_center.post_notification('XMPPGotPresenceSubscriptionRequest', sender=self, data=TimestampedNotificationData(stanza=stanza))
            else:
                subscription.channel.send(stanza)

    def _NH_XMPPGotPresenceProbe(self, notification):
        stanza = notification.data.presence_stanza
        if stanza.recipient.uri.resource is not None:
            # Skip directed presence
            return
        sender_uri = stanza.sender.uri
        sender_uri_bare = FrozenURI(sender_uri.user, sender_uri.host)
        try:
            subscription = self.subscription_manager.incoming_subscriptions[(stanza.recipient.uri, sender_uri_bare)]
        except KeyError:
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPGotPresenceSubscriptionRequest', sender=self, data=TimestampedNotificationData(stanza=stanza))
        else:
            subscription.channel.send(stanza)

