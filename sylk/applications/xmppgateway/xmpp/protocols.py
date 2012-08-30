# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from application.notification import NotificationCenter, NotificationData
from wokkel.muc import UserPresence
from wokkel.xmppim import BasePresenceProtocol, MessageProtocol, PresenceProtocol

from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI
from sylk.applications.xmppgateway.xmpp.stanzas import RECEIPTS_NS, CHATSTATES_NS, ErrorStanza, \
        NormalMessage, MessageReceipt, ChatMessage, ChatComposingIndication,                    \
        AvailabilityPresence, SubscriptionPresence, ProbePresence,                              \
        MUCAvailabilityPresence, GroupChatMessage                                               \

__all__ = ['MessageProtocol', 'MUCProtocol', 'PresenceProtocol']


class MessageProtocol(MessageProtocol):
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
        type = msg.getAttribute('type')

        if type == 'error':
            error_type = msg.error['type']
            conditions = [(child.name, child.defaultUri) for child in msg.error.children]
            error_message = ErrorStanza('message', sender, recipient, error_type, conditions, id=msg.getAttribute('id', None))
            notification_center.post_notification('XMPPGotErrorMessage', sender=self.parent, data=NotificationData(error_message=error_message))
            return

        if type in (None, 'normal', 'chat') and msg.body is not None or msg.html is not None:
            body = None
            html_body = None
            if msg.html is not None:
                html_body = msg.html.toXml()
            if msg.body is not None:
                body = unicode(msg.body)
            use_receipt = msg.request is not None and msg.request.defaultUri == RECEIPTS_NS
            if type == 'chat':
                message = ChatMessage(sender, recipient, body, html_body, id=msg.getAttribute('id', None), use_receipt=use_receipt)
                notification_center.post_notification('XMPPGotChatMessage', sender=self.parent, data=NotificationData(message=message))
            else:
                message = NormalMessage(sender, recipient, body, html_body, id=msg.getAttribute('id', None), use_receipt=use_receipt)
                notification_center.post_notification('XMPPGotNormalMessage', sender=self.parent, data=NotificationData(message=message))
            return

        if type == 'chat' and msg.body is None and msg.html is None:
            # Check if it's a composing indication
            for state in ('active', 'inactive', 'composing', 'paused', 'gone'):
                state_obj = getattr(msg, state, None)
                if state_obj is not None and state_obj.defaultUri == CHATSTATES_NS:
                    composing_indication = ChatComposingIndication(sender, recipient, state, id=msg.getAttribute('id', None))
                    notification_center.post_notification('XMPPGotComposingIndication', sender=self.parent, data=NotificationData(composing_indication=composing_indication))
                    return

        # Check if it's a receipt acknowledgement
        if msg.body is None and msg.html is None and msg.received is not None and msg.received.defaultUri == RECEIPTS_NS:
            receipt_id = msg.getAttribute('id', None)
            if receipt_id is not None:
                receipt = MessageReceipt(sender, recipient, receipt_id)
                notification_center.post_notification('XMPPGotReceipt', sender=self.parent, data=NotificationData(receipt=receipt))


class PresenceProtocol(PresenceProtocol):
    def availableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        show = stanza.show
        statuses = stanza.statuses
        presence_stanza = AvailabilityPresence(sender, recipient, available=True, show=show, statuses=statuses, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def unavailableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = AvailabilityPresence(sender, recipient, available=False, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def _process_subscription_stanza(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        type = stanza.element.getAttribute('type')
        presence_stanza = SubscriptionPresence(sender, recipient, type, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotPresenceSubscriptionStatus', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

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
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotPresenceProbe', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))


class MUCProtocol(BasePresenceProtocol):
    messageTypes = None, 'normal', 'chat', 'groupchat'

    presenceTypeParserMap = {'available': UserPresence,
                             'unavailable': UserPresence}

    def connectionInitialized(self):
        BasePresenceProtocol.connectionInitialized(self)
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
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPMucGotGroupChat', sender=self.parent, data=NotificationData(message=message))

    def availableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = MUCAvailabilityPresence(sender, recipient, available=True, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPMucGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

    def unavailableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = MUCAvailabilityPresence(sender, recipient, available=False, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPMucGotPresenceAvailability', sender=self.parent, data=NotificationData(presence_stanza=presence_stanza))

