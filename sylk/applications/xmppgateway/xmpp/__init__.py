
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from twisted.internet import reactor
from wokkel.disco import DiscoClientProtocol
from wokkel.generic import FallbackHandler, VersionHandler
from wokkel.ping import PingHandler
from wokkel.server import ServerService, XMPPS2SServerFactory
from zope.interface import implementer

from sylk import __version__ as SYLK_VERSION
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import FrozenURI
from sylk.applications.xmppgateway.logger import log
from sylk.applications.xmppgateway.xmpp.jingle.session import JingleSession, JingleSessionManager
from sylk.applications.xmppgateway.xmpp.protocols import DiscoProtocol, JingleProtocol, MessageProtocol, MUCServerProtocol, MUCPresenceProtocol, PresenceProtocol
from sylk.applications.xmppgateway.xmpp.server import SylkInternalComponent, SylkRouter
from sylk.applications.xmppgateway.xmpp.session import XMPPChatSessionManager, XMPPMucSessionManager
from sylk.applications.xmppgateway.xmpp.subscription import XMPPSubscriptionManager

import os
from twisted.internet.ssl import DefaultOpenSSLContextFactory


@implementer(IObserver)
class XMPPManager(object, metaclass=Singleton):

    def __init__(self):
        config = XMPPGatewayConfig

        self.stopped = False

        self.domains = set(config.domains)
        self.muc_domains = set('%s.%s' % (config.muc_prefix, domain) for domain in self.domains)

        router = SylkRouter()
        self._server_service = ServerService(router)
        self._server_service.domains = self.domains | self.muc_domains
        self._server_service.logTraffic = False    # done manually

        self._s2s_factory = XMPPS2SServerFactory(self._server_service)
        self._s2s_factory.logTraffic = False    # done manually

        # Setup internal components

        self._internal_component = SylkInternalComponent(router)
        self._internal_component.domains = self.domains
        self._internal_component.manager = self
        self._muc_component = SylkInternalComponent(router)
        self._muc_component.domains = self.muc_domains
        self._muc_component.manager = self

        # Setup protocols

        self.message_protocol = MessageProtocol()
        self.message_protocol.setHandlerParent(self._internal_component)

        self.presence_protocol = PresenceProtocol()
        self.presence_protocol.setHandlerParent(self._internal_component)

        self.disco_protocol = DiscoProtocol()
        self.disco_protocol.setHandlerParent(self._internal_component)

        self.disco_client_protocol = DiscoClientProtocol()
        self.disco_client_protocol.setHandlerParent(self._internal_component)

        self.muc_protocol = MUCServerProtocol()
        self.muc_protocol.setHandlerParent(self._muc_component)

        self.muc_presence_protocol = MUCPresenceProtocol()
        self.muc_presence_protocol.setHandlerParent(self._muc_component)

        self.disco_muc_protocol = DiscoProtocol()
        self.disco_muc_protocol.setHandlerParent(self._muc_component)

        self.version_protocol = VersionHandler('SylkServer', SYLK_VERSION)
        self.version_protocol.setHandlerParent(self._internal_component)

        self.fallback_protocol = FallbackHandler()
        self.fallback_protocol.setHandlerParent(self._internal_component)

        self.fallback_muc_protocol = FallbackHandler()
        self.fallback_muc_protocol.setHandlerParent(self._muc_component)

        self.ping_protocol = PingHandler()
        self.ping_protocol.setHandlerParent(self._internal_component)

        self.jingle_protocol = JingleProtocol()
        self.jingle_protocol.setHandlerParent(self._internal_component)

        self.jingle_coin_protocol = JingleProtocol()
        self.jingle_coin_protocol.setHandlerParent(self._muc_component)

        self._s2s_listener = None

        self.chat_session_manager = XMPPChatSessionManager()
        self.muc_session_manager = XMPPMucSessionManager()
        self.subscription_manager = XMPPSubscriptionManager()
        self.jingle_session_manager = JingleSessionManager()

    def start(self):
        self.stopped = False
        # noinspection PyUnresolvedReferences
        interface = XMPPGatewayConfig.local_ip
        port = XMPPGatewayConfig.local_port
        cert_path = XMPPGatewayConfig.certificate.normalized if XMPPGatewayConfig.certificate else None
        cert_chain_path = XMPPGatewayConfig.ca_file.normalized if XMPPGatewayConfig.ca_file else None
        if XMPPGatewayConfig.transport == 'tls':
            if cert_path is not None:
                if not os.path.isfile(cert_path):
                    log.error('Certificate file %s could not be found' % cert_path)
                    return
                try:
                    ssl_ctx_factory = DefaultOpenSSLContextFactory(cert_path, cert_path)
                except Exception:
                    log.exception('Creating TLS context')
                    return
                if cert_chain_path is not None:
                    if not os.path.isfile(cert_chain_path):
                        log.error('Certificate chain file %s could not be found' % cert_chain_path)
                        return
                    ssl_ctx = ssl_ctx_factory.getContext()
                    try:
                        ssl_ctx.use_certificate_chain_file(cert_chain_path)
                    except Exception:
                        log.exception('Setting TLS certificate chain file')
                        return
                self._s2s_listener = reactor.listenSSL(port, self._s2s_factory, ssl_ctx_factory, interface=interface)
        else:
            self._s2s_listener = reactor.listenTCP(port, self._s2s_factory, interface=interface)


        port = self._s2s_listener.getHost().port
        listen_address = self._s2s_listener.getHost()
        log.info("XMPP S2S component listening on %s:%d (%s)" % (listen_address.host, listen_address.port, XMPPGatewayConfig.transport.upper()))

        self.chat_session_manager.start()
        self.muc_session_manager.start()
        self.subscription_manager.start()
        self.jingle_session_manager.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._internal_component)
        notification_center.add_observer(self, sender=self._muc_component)
        self._internal_component.startService()
        self._muc_component.startService()

    def stop(self):
        self.stopped = True
        self._s2s_listener.stopListening()
        self.jingle_session_manager.stop()
        self.subscription_manager.stop()
        self.muc_session_manager.stop()
        self.chat_session_manager.stop()
        self._internal_component.stopService()
        self._muc_component.stopService()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._internal_component)
        notification_center.remove_observer(self, sender=self._muc_component)

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

    def _NH_XMPPMucGotSubject(self, notification):
        message = notification.data.message
        muc_uri = FrozenURI(message.recipient.uri.user, message.recipient.uri.host)
        try:
            session = self.muc_session_manager.incoming[(muc_uri, message.sender.uri)]
        except KeyError:
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

    # Jingle

    def _NH_XMPPGotJingleSessionInitiate(self, notification):
        stanza = notification.data.stanza
        try:
            self.jingle_session_manager.sessions[stanza.jingle.sid]
        except KeyError:
            session = JingleSession(notification.data.protocol)
            session.init_incoming(stanza)
            session.send_ring_indication()

    def _NH_XMPPGotJingleSessionTerminate(self, notification):
        stanza = notification.data.stanza
        try:
            session = self.jingle_session_manager.sessions[stanza.jingle.sid]
        except KeyError:
            return
        session.handle_notification(notification)

    def _NH_XMPPGotJingleSessionInfo(self, notification):
        stanza = notification.data.stanza
        try:
            session = self.jingle_session_manager.sessions[stanza.jingle.sid]
        except KeyError:
            return
        session.handle_notification(notification)

    def _NH_XMPPGotJingleSessionAccept(self, notification):
        stanza = notification.data.stanza
        try:
            session = self.jingle_session_manager.sessions[stanza.jingle.sid]
        except KeyError:
            return
        session.handle_notification(notification)

    def _NH_XMPPGotJingleDescriptionInfo(self, notification):
        stanza = notification.data.stanza
        try:
            session = self.jingle_session_manager.sessions[stanza.jingle.sid]
        except KeyError:
            return
        session.handle_notification(notification)

    def _NH_XMPPGotJingleTransportInfo(self, notification):
        stanza = notification.data.stanza
        try:
            session = self.jingle_session_manager.sessions[stanza.jingle.sid]
        except KeyError:
            return
        session.handle_notification(notification)

