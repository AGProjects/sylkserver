
from application.notification import NotificationCenter, NotificationData
from twisted.internet import defer, reactor
from twisted.words.protocols.jabber.error import StanzaError
from twisted.words.protocols.jabber.jid import JID
from wokkel import disco, muc, ping, xmppim

from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI
from sylk.applications.xmppgateway.xmpp.stanzas import RECEIPTS_NS, CHATSTATES_NS, MUC_USER_NS, ErrorStanza, ChatComposingIndication
from sylk.applications.xmppgateway.xmpp.stanzas import ChatMessage, GroupChatMessage, IncomingInvitationMessage, NormalMessage, MessageReceipt
from sylk.applications.xmppgateway.xmpp.stanzas import AvailabilityPresence, SubscriptionPresence, ProbePresence, MUCAvailabilityPresence
from sylk.applications.xmppgateway.xmpp.stanzas import jingle


__all__ = 'DiscoProtocol', 'JingleProtocol', 'MessageProtocol', 'MUCServerProtocol', 'MUCPresenceProtocol', 'PresenceProtocol'


class MessageProtocol(xmppim.MessageProtocol):
    messageTypes = None, 'normal', 'chat', 'headline', 'groupchat', 'error'

    def _onMessage(self, message):
        if message.handled:
            return
        messageType = message.getAttribute("type")
        if messageType not in self.messageTypes:
            message["type"] = 'normal'
        self.onMessage(message)

    def onMessage(self, msg):
        notification_center = NotificationCenter()

        sender_uri = FrozenURI.parse('xmpp:'+msg['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+msg['to'])
        recipient = Identity(recipient_uri)

        msg_type = msg.getAttribute('type')
        msg_id = msg.getAttribute('id', None)
        is_empty = msg.body is None and msg.html is None

        if msg_type == 'error':
            error_type = msg.error['type']
            conditions = [(child.name, child.defaultUri) for child in msg.error.elements()]
            error_message = ErrorStanza('message', sender, recipient, error_type, conditions, id=msg_id)
            notification_center.post_notification('XMPPGotErrorMessage', sender=self.parent, data=NotificationData(error_message=error_message))
            return

        if msg_type in (None, 'normal', 'chat') and not is_empty:
            body = None
            html_body = None
            if msg.html is not None:
                html_body = msg.html.toXml()
            if msg.body is not None:
                body = unicode(msg.body)
            try:
                elem = next(c for c in msg.elements() if c.uri == RECEIPTS_NS)
            except StopIteration:
                use_receipt = False
            else:
                use_receipt = elem.name == u'request'
            if msg_type == 'chat':
                message = ChatMessage(sender, recipient, body, html_body, id=msg_id, use_receipt=use_receipt)
                notification_center.post_notification('XMPPGotChatMessage', sender=self.parent, data=NotificationData(message=message))
            else:
                message = NormalMessage(sender, recipient, body, html_body, id=msg_id, use_receipt=use_receipt)
                notification_center.post_notification('XMPPGotNormalMessage', sender=self.parent, data=NotificationData(message=message))
            return

        # Check if it's a composing indication
        if msg_type == 'chat' and is_empty:
            for elem in msg.elements():
                try:
                    elem = next(c for c in msg.elements() if c.uri == CHATSTATES_NS)
                except StopIteration:
                    pass
                else:
                    composing_indication = ChatComposingIndication(sender, recipient, elem.name, id=msg_id)
                    notification_center.post_notification('XMPPGotComposingIndication', sender=self.parent, data=NotificationData(composing_indication=composing_indication))
                    return

        # Check if it's a receipt acknowledgement
        if is_empty:
            try:
                elem = next(c for c in msg.elements() if c.uri == RECEIPTS_NS)
            except StopIteration:
                pass
            else:
                if elem.name == u'received' and msg_id is not None:
                    receipt = MessageReceipt(sender, recipient, msg_id)
                    notification_center.post_notification('XMPPGotReceipt', sender=self.parent, data=NotificationData(receipt=receipt))


class PresenceProtocol(xmppim.PresenceProtocol):
    def availableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        show = stanza.show
        statuses = stanza.statuses
        presence_stanza = AvailabilityPresence(sender, recipient, available=True, show=show, statuses=statuses, id=id)
        NotificationCenter().post_notification('XMPPGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def unavailableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = AvailabilityPresence(sender, recipient, available=False, id=id)
        NotificationCenter().post_notification('XMPPGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def _process_subscription_stanza(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        type = stanza.element.getAttribute('type')
        presence_stanza = SubscriptionPresence(sender, recipient, type, id=id)
        NotificationCenter().post_notification('XMPPGotPresenceSubscriptionStatus', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def subscribedReceived(self, stanza):
        self._process_subscription_stanza(stanza)

    def unsubscribedReceived(self, stanza):
        self._process_subscription_stanza(stanza)

    def subscribeReceived(self, stanza):
        self._process_subscription_stanza(stanza)

    def unsubscribeReceived(self, stanza):
        self._process_subscription_stanza(stanza)

    def probeReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = ProbePresence(sender, recipient, id=id)
        NotificationCenter().post_notification('XMPPGotPresenceProbe', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))


class MUCServerProtocol(xmppim.BasePresenceProtocol):
    messageTypes = None, 'normal', 'chat', 'groupchat'

    presenceTypeParserMap = {'available': muc.UserPresence,
                             'unavailable': muc.UserPresence}

    def connectionInitialized(self):
        self.xmlstream.addObserver('/presence/x[@xmlns="%s"]' % muc.NS_MUC, self._onPresence)
        self.xmlstream.addObserver('/message', self._onMessage)

    def _onMessage(self, message):
        if message.handled:
            return
        messageType = message.getAttribute("type")
        if messageType == 'error':
            return
        if messageType not in self.messageTypes:
            message['type'] = 'normal'
        if messageType == 'groupchat':
            self.onGroupChat(message)
        else:
            to_uri = FrozenURI.parse('xmpp:'+message['to'])
            if to_uri.host in self.parent.domains:
                # Check if it's an invitation
                if message.x is not None and message.x.invite is not None and message.x.invite.uri == MUC_USER_NS:
                    self.onInvitation(message)
            else:
                # TODO: give error, private messages not supported
                pass

    def onGroupChat(self, msg):
        sender_uri = FrozenURI.parse('xmpp:'+msg['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+msg['to'])
        recipient = Identity(recipient_uri)
        body = None
        html_body = None
        if msg.html is not None:
            html_body = msg.html.toXml()
        if msg.body is not None:
            body = unicode(msg.body)
        message = GroupChatMessage(sender, recipient, body, html_body, id=msg.getAttribute('id', None))
        NotificationCenter().post_notification('XMPPMucGotGroupChat', sender=self.parent, data=NotificationData(message=message))

    def onInvitation(self, msg):
        sender_uri = FrozenURI.parse('xmpp:'+msg['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+msg['to'])
        recipient = Identity(recipient_uri)
        invited_user_uri = FrozenURI.parse('xmpp:'+msg.x.invite['to'])
        invited_user = Identity(invited_user_uri)
        if msg.x.invite.reason is not None and msg.x.invite.reason.uri == MUC_USER_NS:
            reason = unicode(msg.x.invite.reason)
        else:
            reason = None
        invitation = IncomingInvitationMessage(sender, recipient, invited_user=invited_user, reason=reason, id=msg.getAttribute('id', None))
        NotificationCenter().post_notification('XMPPMucGotInvitation', sender=self.parent, data=NotificationData(invitation=invitation))

    def availableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = MUCAvailabilityPresence(sender, recipient, available=True, id=id)
        NotificationCenter().post_notification('XMPPMucGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def unavailableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = MUCAvailabilityPresence(sender, recipient, available=False, id=id)
        NotificationCenter().post_notification('XMPPMucGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))


class DiscoProtocol(disco.DiscoHandler):

    def info(self, requestor, target, nodeIdentifier):
        """
        Gather data for a disco info request.

        @param requestor: The entity that sent the request.
        @type requestor: L{JID<twisted.words.protocols.jabber.jid.JID>}
        @param target: The entity the request was sent to.
        @type target: L{JID<twisted.words.protocols.jabber.jid.JID>}
        @param nodeIdentifier: The optional node being queried, or C{''}.
        @type nodeIdentifier: C{unicode}
        @return: Deferred with the gathered results from sibling handlers.
        @rtype: L{defer.Deferred}
        """

        xmpp_manager = self.parent.manager

        if target.host not in xmpp_manager.domains | xmpp_manager.muc_domains:
            return defer.fail(StanzaError('service-unavailable'))

        elements = [disco.DiscoFeature(disco.NS_DISCO_INFO),
                    disco.DiscoFeature(disco.NS_DISCO_ITEMS),
                    disco.DiscoFeature('http://sylkserver.com')]

        if target.host in xmpp_manager.muc_domains:
            elements.append(disco.DiscoIdentity('conference', 'text', 'SylkServer Chat Service'))
            elements.append(disco.DiscoFeature('http://jabber.org/protocol/muc'))
            elements.append(disco.DiscoFeature('urn:ietf:rfc:3264'))
            elements.append(disco.DiscoFeature('urn:xmpp:coin'))
            elements.append(disco.DiscoFeature(jingle.NS_JINGLE))
            elements.append(disco.DiscoFeature(jingle.NS_JINGLE_APPS_RTP))
            elements.append(disco.DiscoFeature(jingle.NS_JINGLE_APPS_RTP_AUDIO))
            #elements.append(disco.DiscoFeature(jingle.NS_JINGLE_APPS_RTP_VIDEO))
            elements.append(disco.DiscoFeature(jingle.NS_JINGLE_ICE_UDP_TRANSPORT))
            elements.append(disco.DiscoFeature(jingle.NS_JINGLE_RAW_UDP_TRANSPORT))
            if target.user:
                # We can't say much more here, because the actual conference may end up on a different server
                elements.append(disco.DiscoFeature('muc_temporary'))
                elements.append(disco.DiscoFeature('muc_unmoderated'))
        else:
            elements.append(disco.DiscoFeature(ping.NS_PING))
            if not target.user:
                elements.append(disco.DiscoIdentity('gateway', 'simple', 'SylkServer'))
                elements.append(disco.DiscoIdentity('server', 'im', 'SylkServer'))
            else:
                elements.append(disco.DiscoIdentity('client', 'pc'))
                elements.append(disco.DiscoFeature('http://jabber.org/protocol/caps'))
                elements.append(disco.DiscoFeature('http://jabber.org/protocol/chatstates'))
                elements.append(disco.DiscoFeature('urn:ietf:rfc:3264'))
                elements.append(disco.DiscoFeature('urn:xmpp:coin'))
                elements.append(disco.DiscoFeature(jingle.NS_JINGLE))
                elements.append(disco.DiscoFeature(jingle.NS_JINGLE_APPS_RTP))
                elements.append(disco.DiscoFeature(jingle.NS_JINGLE_APPS_RTP_AUDIO))
                #elements.append(disco.DiscoFeature(jingle.NS_JINGLE_APPS_RTP_VIDEO))
                elements.append(disco.DiscoFeature(jingle.NS_JINGLE_ICE_UDP_TRANSPORT))
                elements.append(disco.DiscoFeature(jingle.NS_JINGLE_RAW_UDP_TRANSPORT))

        return defer.succeed(elements)

    def items(self, requestor, target, nodeIdentifier):
        """
        Gather data for a disco items request.

        @param requestor: The entity that sent the request.
        @type requestor: L{JID<twisted.words.protocols.jabber.jid.JID>}
        @param target: The entity the request was sent to.
        @type target: L{JID<twisted.words.protocols.jabber.jid.JID>}
        @param nodeIdentifier: The optional node being queried, or C{''}.
        @type nodeIdentifier: C{unicode}
        @return: Deferred with the gathered results from sibling handlers.
        @rtype: L{defer.Deferred}
        """

        xmpp_manager = self.parent.manager

        items = []

        if not target.user and target.host in xmpp_manager.domains:
            items.append(disco.DiscoItem(JID('%s.%s' % (XMPPGatewayConfig.muc_prefix, target.host)), name='Multi-User Chat'))

        return defer.succeed(items)


class JingleProtocol(jingle.JingleHandler):
    # Functions here need to return immediately so that the IQ result is sent, so schedule them in the reactor
    # TODO: review and remove this, just post notifications?

    def onSessionInitiate(self, request):
        reactor.callLater(0, NotificationCenter().post_notification,
                             'XMPPGotJingleSessionInitiate',
                             sender=self.parent,
                             data=NotificationData(stanza=request, protocol=self))

    def onSessionTerminate(self, request):
        reactor.callLater(0, NotificationCenter().post_notification,
                             'XMPPGotJingleSessionTerminate',
                             sender=self.parent,
                             data=NotificationData(stanza=request))

    def onSessionAccept(self, request):
        reactor.callLater(0, NotificationCenter().post_notification,
                             'XMPPGotJingleSessionAccept',
                             sender=self.parent,
                             data=NotificationData(stanza=request))

    def onSessionInfo(self, request):
        reactor.callLater(0, NotificationCenter().post_notification,
                             'XMPPGotJingleSessionInfo',
                             sender=self.parent,
                             data=NotificationData(stanza=request))

    def onDescriptionInfo(self, request):
        reactor.callLater(0, NotificationCenter().post_notification,
                             'XMPPGotJingleDescriptionInfo',
                             sender=self.parent,
                             data=NotificationData(stanza=request))

    def onTransportInfo(self, request):
        reactor.callLater(0, NotificationCenter().post_notification,
                             'XMPPGotJingleTransportInfo',
                             sender=self.parent,
                             data=NotificationData(stanza=request))


class MUCPresenceProtocol(xmppim.PresenceProtocol):
    """Protocol implementation to handle presence subscription to MUC URIs
    """

    def subscribeReceived(self, stanza):
        """
        Subscription request was received.
        """
        self.subscribed(stanza.sender, sender=stanza.recipient)
        self.send_available(stanza)

    def unsubscribeReceived(self, stanza):
        """
        Unsubscription request was received.
        """
        self.unsubscribed(stanza.sender, sender=stanza.recipient)

    def probeReceived(self, stanza):
        """
        Probe presence was received.
        """
        self.send_available(stanza)

    def send_available(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)

        available = AvailabilityPresence(sender=recipient, recipient=sender)
        self.send(available.to_xml_element())

