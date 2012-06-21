# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from application.notification import NotificationCenter
from sipsimple.util import TimestampedNotificationData
from wokkel.xmppim import MessageProtocol, PresenceProtocol

from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI
from sylk.applications.xmppgateway.xmpp.stanzas import NormalMessage, MessageReceipt, ChatMessage, \
        ChatComposingIndication, ErrorStanza, AvailabilityPresence, SubscriptionPresence, ProbePresence
from sylk.applications.xmppgateway.xmpp.stanzas import RECEIPTS_NS, CHATSTATES_NS



class MessageProtocol(MessageProtocol):
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
            notification_center.post_notification('XMPPGotErrorMessage', sender=self.parent, data=TimestampedNotificationData(error_message=error_message))
            return

        if type in (None, 'normal', 'chat') and msg.body is not None:
            if msg.html is not None:
                content_type = 'text/html'
                body = msg.html.toXml()
            else:
                content_type = 'text/plain'
                body = unicode(msg.body)
            use_receipt = msg.request is not None and msg.request.defaultUri == RECEIPTS_NS
            if type == 'chat':
                message = ChatMessage(sender, recipient, body, content_type, id=msg.getAttribute('id', None), use_receipt=use_receipt)
                notification_center.post_notification('XMPPGotChatMessage', sender=self.parent, data=TimestampedNotificationData(message=message))
            else:
                message = NormalMessage(sender, recipient, body, content_type, id=msg.getAttribute('id', None), use_receipt=use_receipt)
                notification_center.post_notification('XMPPGotNormalMessage', sender=self.parent, data=TimestampedNotificationData(message=message))
            return

        if type == 'chat' and msg.body is None:
            # Check if it's a composing indication
            for attr in ('active', 'inactive', 'composing', 'paused', 'gone'):
                attr_obj = getattr(msg, attr, None)
                if attr_obj is not None and attr_obj.defaultUri == CHATSTATES_NS:
                    state = attr
                    break
            else:
                state = None
            if state is not None:
                composing_indication = ChatComposingIndication(sender, recipient, state, id=msg.getAttribute('id', None))
                notification_center.post_notification('XMPPGotComposingIndication', sender=self.parent, data=TimestampedNotificationData(composing_indication=composing_indication))
                return

        # Check if it's a receipt acknowledgement
        if msg.body is None and msg.received is not None and msg.received.defaultUri == RECEIPTS_NS:
            receipt_id = msg.getAttribute('id', None)
            if receipt_id is not None:
                receipt = MessageReceipt(sender, recipient, receipt_id)
                notification_center.post_notification('XMPPGotReceipt', sender=self.parent, data=TimestampedNotificationData(receipt=receipt))


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
        notification_center.post_notification('XMPPGotPresenceAvailability', sender=self.parent, data=TimestampedNotificationData(presence_stanza=presence_stanza))

    def unavailableReceived(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        presence_stanza = AvailabilityPresence(sender, recipient, available=False, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotPresenceAvailability', sender=self.parent, data=TimestampedNotificationData(presence_stanza=presence_stanza))

    def _process_subscription_stanza(self, stanza):
        sender_uri = FrozenURI.parse('xmpp:'+stanza.element['from'])
        sender = Identity(sender_uri)
        recipient_uri = FrozenURI.parse('xmpp:'+stanza.element['to'])
        recipient = Identity(recipient_uri)
        id = stanza.element.getAttribute('id')
        type = stanza.element.getAttribute('type')
        presence_stanza = SubscriptionPresence(sender, recipient, type, id=id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPGotPresenceSubscriptionStatus', sender=self.parent, data=TimestampedNotificationData(presence_stanza=presence_stanza))

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
        notification_center.post_notification('XMPPGotPresenceProbe', sender=self.parent, data=TimestampedNotificationData(presence_stanza=presence_stanza))



