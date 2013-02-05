# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor
from twisted.words.protocols.jabber.error import StanzaError
from twisted.words.protocols.jabber.jid import JID, internJID
from wokkel import disco, ping
from wokkel.component import InternalComponent, Router as _Router
from wokkel.generic import FallbackHandler, VersionHandler
from wokkel.server import ServerService, XMPPS2SServerFactory, DeferredS2SClientFactory
from zope.interface import implements

from sylk import __version__ as SYLK_VERSION
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import FrozenURI
from sylk.applications.xmppgateway.logger import log
from sylk.applications.xmppgateway.xmpp.logger import Logger as XMPPLogger
from sylk.applications.xmppgateway.xmpp.protocols import DiscoProtocol, MessageProtocol, MUCServerProtocol, PresenceProtocol
from sylk.applications.xmppgateway.xmpp.session import XMPPChatSessionManager, XMPPMucSessionManager
from sylk.applications.xmppgateway.xmpp.subscription import XMPPSubscriptionManager


xmpp_logger = XMPPLogger()


# Utility classes

class Router(_Router):
    def route(self, stanza):
        """
        Route a stanza. (subclassed to avoid vebose logging)

        @param stanza: The stanza to be routed.
        @type stanza: L{domish.Element}.
        """
        destination = internJID(stanza['to'])

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
                xmpp_logger.msg("RECEIVED", ISOTimestamp.now(), buf)

        def logDataOut(buf):
            buf = buf.strip()
            if buf:
                xmpp_logger.msg("SENDING", ISOTimestamp.now(), buf)

        if XMPPGatewayConfig.trace_xmpp:
            xs.rawDataInFn = logDataIn
            xs.rawDataOutFn = logDataOut


class DeferredS2SClientFactory(DeferredS2SClientFactory):
    def onConnectionMade(self, xs):
        super(self.__class__, self).onConnectionMade(xs)

        def logDataIn(buf):
            buf = buf.strip()
            if buf:
                xmpp_logger.msg("RECEIVED", ISOTimestamp.now(), buf)

        def logDataOut(buf):
            buf = buf.strip()
            if buf:
                xmpp_logger.msg("SENDING", ISOTimestamp.now(), buf)

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

        self.domains = set(config.domains)
        self.muc_domains = set(['%s.%s' % (config.muc_prefix, domain) for domain in self.domains])

        router = Router()
        self._server_service = ServerService(router)
        self._server_service.domains = self.domains | self.muc_domains
        self._server_service.logTraffic = False    # done manually

        self._s2s_factory = XMPPS2SServerFactory(self._server_service)
        self._s2s_factory.logTraffic = False    # done manually

        # Setup internal components

        self._internal_component = InternalComponent(router)
        self._internal_component.domains = self.domains
        self._muc_component = InternalComponent(router)
        self._muc_component.domains = self.muc_domains

        # Setup protocols

        self._protocols = set()

        message_protocol = MessageProtocol()
        message_protocol.setHandlerParent(self._internal_component)
        self._protocols.add(message_protocol)

        presence_protocol = PresenceProtocol()
        presence_protocol.setHandlerParent(self._internal_component)
        self._protocols.add(presence_protocol)

        disco_protocol = DiscoProtocol()
        disco_protocol.setHandlerParent(self._internal_component)
        self._protocols.add(disco_protocol)

        muc_protocol = MUCServerProtocol()
        muc_protocol.setHandlerParent(self._muc_component)
        self._protocols.add(muc_protocol)

        disco_muc_protocol = DiscoProtocol()
        disco_muc_protocol.setHandlerParent(self._muc_component)
        self._protocols.add(disco_muc_protocol)

        version_protocol = VersionHandler('SylkServer', SYLK_VERSION)
        version_protocol.setHandlerParent(self._internal_component)
        self._protocols.add(version_protocol)

        fallback_protocol = FallbackHandler()
        fallback_protocol.setHandlerParent(self._internal_component)
        self._protocols.add(fallback_protocol)

        fallback_muc_protocol = FallbackHandler()
        fallback_muc_protocol.setHandlerParent(self._muc_component)
        self._protocols.add(fallback_muc_protocol)

        ping_protocol = ping.PingHandler()
        ping_protocol.setHandlerParent(self._internal_component)
        self._protocols.add(ping_protocol)

        self._s2s_listener = None

        self.chat_session_manager = XMPPChatSessionManager()
        self.muc_session_manager = XMPPMucSessionManager()
        self.subscription_manager = XMPPSubscriptionManager()

    def start(self):
        self.stopped = False
        xmpp_logger.start()
        config = XMPPGatewayConfig
        self._s2s_listener = reactor.listenTCP(config.local_port, self._s2s_factory, interface=config.local_ip)
        listen_address = self._s2s_listener.getHost()
        log.msg("XMPP listener started on %s:%d" % (listen_address.host, listen_address.port))
        self.chat_session_manager.start()
        self.muc_session_manager.start()
        self.subscription_manager.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._internal_component)
        notification_center.add_observer(self, sender=self._muc_component)
        self._internal_component.startService()
        self._muc_component.startService()

    def stop(self):
        self.stopped = True
        self._s2s_listener.stopListening()
        self.subscription_manager.stop()
        self.muc_session_manager.stop()
        self.chat_session_manager.stop()
        self._internal_component.stopService()
        self._muc_component.stopService()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._internal_component)
        notification_center.remove_observer(self, sender=self._muc_component)
        xmpp_logger.stop()

    def send_stanza(self, stanza):
        if self.stopped:
            return
        self._internal_component.send(stanza.to_xml_element())

    def send_muc_stanza(self, stanza):
        if self.stopped:
            return
        self._muc_component.send(stanza.to_xml_element())

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    # Process message stanzas

    def _NH_XMPPGotChatMessage(self, notification):
        message = notification.data.message
        try:
            session = self.chat_session_manager.sessions[(message.recipient.uri, message.sender.uri)]
        except KeyError:
            notification.center.post_notification('XMPPGotChatMessage', sender=self, data=notification.data)
        else:
            session.channel.send(message)

    def _NH_XMPPGotNormalMessage(self, notification):
        notification.center.post_notification('XMPPGotNormalMessage', sender=self, data=notification.data)

    def _NH_XMPPGotComposingIndication(self, notification):
        composing_indication = notification.data.composing_indication
        try:
            session = self.chat_session_manager.sessions[(composing_indication.recipient.uri, composing_indication.sender.uri)]
        except KeyError:
            notification.center.post_notification('XMPPGotComposingIndication', sender=self, data=notification.data)
        else:
            session.channel.send(composing_indication)

    def _NH_XMPPGotErrorMessage(self, notification):
        error_message = notification.data.error_message
        try:
            session = self.chat_session_manager.sessions[(error_message.recipient.uri, error_message.sender.uri)]
        except KeyError:
            notification.center.post_notification('XMPPGotErrorMessage', sender=self, data=notification.data)
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
                    notification.center.post_notification('XMPPGotPresenceSubscriptionRequest', sender=self, data=NotificationData(stanza=stanza))
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
            notification.center.post_notification('XMPPGotPresenceSubscriptionRequest', sender=self, data=NotificationData(stanza=stanza))
        else:
            subscription.channel.send(stanza)

    # Process muc stanzas

    def _NH_XMPPMucGotGroupChat(self, notification):
        message = notification.data.message
        muc_uri = FrozenURI(message.recipient.uri.user, message.recipient.uri.host)
        try:
            session = self.muc_session_manager.incoming[(muc_uri, message.sender.uri)]
        except KeyError:
            # Ignore groupchat messages if there was no session created
            pass
        else:
            session.channel.send(message)

    def _NH_XMPPMucGotPresenceAvailability(self, notification):
        stanza = notification.data.presence_stanza
        if not stanza.sender.uri.resource:
            return
        muc_uri = FrozenURI(stanza.recipient.uri.user, stanza.recipient.uri.host)
        try:
            session = self.muc_session_manager.incoming[(muc_uri, stanza.sender.uri)]
        except KeyError:
            if stanza.available:
                notification.center.post_notification('XMPPGotMucJoinRequest', sender=self, data=NotificationData(stanza=stanza))
            else:
                notification.center.post_notification('XMPPGotMucLeaveRequest', sender=self, data=NotificationData(stanza=stanza))
        else:
            session.channel.send(stanza)

    def _NH_XMPPMucGotInvitation(self, notification):
        invitation = notification.data.invitation
        data = NotificationData(sender=invitation.sender, recipient=invitation.recipient, participant=invitation.invited_user)
        notification.center.post_notification('XMPPGotMucAddParticipantRequest', sender=self, data=data)

    # Disco

    def _NH_XMPPGotDiscoInfoRequest(self, notification):
        d = notification.data.deferred
        target_uri = notification.data.target.uri

        if target_uri.host not in self.domains | self.muc_domains:
            d.errback(StanzaError('service-unavailable'))
            return

        elements = []

        if target_uri.host in self.muc_domains:
            elements.append(disco.DiscoIdentity('conference', 'text', 'SylkServer Chat Service'))
            elements.append(disco.DiscoFeature('http://jabber.org/protocol/muc'))
            if target_uri.user:
                # We can't say much more here, because the actual conference may end up on a different server
                elements.append(disco.DiscoFeature('muc_temporary'))
                elements.append(disco.DiscoFeature('muc_unmoderated'))
        else:
            elements.append(disco.DiscoFeature(ping.NS_PING))
            if not target_uri.user:
                elements.append(disco.DiscoIdentity('gateway', 'simple', 'SylkServer'))
                elements.append(disco.DiscoIdentity('server', 'im', 'SylkServer'))
            else:
                elements.append(disco.DiscoIdentity('account', 'registered'))
                elements.append(disco.DiscoFeature('http://jabber.org/protocol/caps'))

        elements.append(disco.DiscoFeature(disco.NS_DISCO_INFO))
        elements.append(disco.DiscoFeature(disco.NS_DISCO_ITEMS))
        elements.append(disco.DiscoFeature('http://sylkserver.com'))

        d.callback(elements)

    def _NH_XMPPGotDiscoItemsRequest(self, notification):
        d = notification.data.deferred
        target_uri = notification.data.target.uri
        items = []

        if not target_uri.user and target_uri.host in self.domains:
            items.append(disco.DiscoItem(JID('%s.%s' % (XMPPGatewayConfig.muc_prefix, target_uri.host)), name='Multi-User Chat'))

        d.callback(items)

