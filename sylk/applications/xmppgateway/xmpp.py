# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.descriptor import WriteOnceAttribute
from application.python.types import Singleton
from datetime import datetime
from eventlet import coros, proc
from sipsimple.util import Timestamp, TimestampedNotificationData
from twisted.internet import reactor
from twisted.words.protocols.jabber.jid import internJID as JID
from twisted.words.xish import domish
from wokkel.component import InternalComponent, Router as _Router
from wokkel.server import ServerService, XMPPS2SServerFactory as _XMPPS2SServerFactory, DeferredS2SClientFactory as _DeferredS2SClientFactory
from wokkel.xmppim import MessageProtocol as _MessageProtocol, PresenceProtocol as _PresenceProtocol
from zope.interface import implements

from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI
from sylk.applications.xmppgateway.logger import Logger


CHATSTATES_NS = 'http://jabber.org/protocol/chatstates'
RECEIPTS_NS   = 'urn:xmpp:receipts'
STANZAS_NS    = 'urn:ietf:params:xml:ns:xmpp-stanzas'
XML_NS        = 'http://www.w3.org/XML/1998/namespace'

xmpp_logger = Logger()

# Datatypes

class BaseStanza(object):
    _stanza_type = None     # to be defined by subclasses

    def __init__(self, sender, recipient, id=None, type=None):
        self.sender = sender
        self.recipient = recipient
        self.id = id
        self.type = type

    def to_xml_element(self):
        xml_element = domish.Element((None, self._stanza_type))
        xml_element['from'] = self.sender.uri.as_string('xmpp')
        xml_element['to'] = self.recipient.uri.as_string('xmpp')
        if self.type:
            xml_element['type'] = self.type
        if self.id is not None:
            xml_element['id'] = self.id
        return xml_element

class MessageStanza(BaseStanza):
    _stanza_type = 'message'

    def __init__(self, sender, recipient, type='', id=None, use_receipt=False):
        super(MessageStanza, self).__init__(sender, recipient, id=id, type=type)
        self.use_receipt = use_receipt

    def to_xml_element(self):
        xml_element = super(MessageStanza, self).to_xml_element()
        if self.id is not None and self.recipient.uri.resource is not None and self.use_receipt:
            xml_element.addElement('request', defaultUri=RECEIPTS_NS)
        return xml_element

class NormalMessage(MessageStanza):
    def __init__(self, sender, recipient, body, content_type='text/plain', id=None, use_receipt=False):
        super(NormalMessage, self).__init__(sender, recipient, type='', id=id, use_receipt=use_receipt)
        self.body = body
        self.content_type = content_type

    def to_xml_element(self):
        xml_element = super(NormalMessage, self).to_xml_element()
        xml_element.addElement('body', content=self.body)    # TODO: what if content type is text/html ?
        return xml_element

class ChatMessage(MessageStanza):
    def __init__(self, sender, recipient, body, content_type='text/plain', id=None, use_receipt=True):
        super(ChatMessage, self).__init__(sender, recipient, type='chat', id=id, use_receipt=use_receipt)
        self.body = body
        self.content_type = content_type

    def to_xml_element(self):
        xml_element = super(ChatMessage, self).to_xml_element()
        xml_element.addElement('active', defaultUri=CHATSTATES_NS)
        xml_element.addElement('body', content=self.body)    # TODO: what if content type is text/html ?
        return xml_element

class ChatComposingIndication(MessageStanza):
    def __init__(self, sender, recipient, state, id=None, use_receipt=False):
        super(ChatComposingIndication, self).__init__(sender, recipient, type='chat', id=id, use_receipt=use_receipt)
        self.state = state

    def to_xml_element(self):
        xml_element = super(ChatComposingIndication, self).to_xml_element()
        xml_element.addElement(self.state, defaultUri=CHATSTATES_NS)
        return xml_element

class MessageReceipt(MessageStanza):
    def __init__(self, sender, recipient, receipt_id, id=None):
        super(MessageReceipt, self).__init__(sender, recipient, type='', id=id, use_receipt=False)
        self.receipt_id = receipt_id

    def to_xml_element(self):
        xml_element = super(MessageReceipt, self).to_xml_element()
        receipt_element = domish.Element((RECEIPTS_NS, 'received'))
        receipt_element['id'] = self.receipt_id
        xml_element.addChild(receipt_element)
        return xml_element

class PresenceStanza(BaseStanza):
    _stanza_type = 'presence'

    def __init__(self, sender, recipient, type=None, id=None):
        super(PresenceStanza, self).__init__(sender, recipient, type=type, id=id)

class AvailabilityPresence(PresenceStanza):
    def __init__(self, sender, recipient, available=True, show=None, statuses=None, priority=0, id=None):
        super(AvailabilityPresence, self).__init__(sender, recipient, id=id)
        self.available = available
        self.show = show
        self.priority = priority
        self.statuses = statuses or {}
    
    def _get_available(self):
        return self.__dict__['available']
    def _set_available(self, available):
        if available:
            self.type = None
        else:
            self.type = 'unavailable'
        self.__dict__['available'] = available
    available = property(_get_available, _set_available)
    del _get_available, _set_available

    @property
    def status(self):
        status = self.statuses.get(None)
        if status is None:
            try:
                status = self.statuses.itervalues().next()
            except StopIteration:
                pass
        return status

    def to_xml_element(self):
        xml_element = super(PresenceStanza, self).to_xml_element()
        if self.available:
            if self.show is not None:
                xml_element.addElement('show', content=self.show)
            if self.priority != 0:
                xml_element.addElement('priority', content=unicode(self.priority))
        for lang, text in self.statuses.iteritems():
            status = xml_element.addElement('status', content=text)
            if lang:
                status[(XML_NS, 'lang')] = lang
        return xml_element

class SubscriptionPresence(PresenceStanza):
    def __init__(self, sender, recipient, type, id=None):
        super(SubscriptionPresence, self).__init__(sender, recipient, type=type, id=id)

class ProbePresence(PresenceStanza):
    def __init__(self, sender, recipient, id=None):
        super(ProbePresence, self).__init__(sender, recipient, type='probe', id=id)

class ErrorStanza(object):
    """
    Stanza representing an error of another stanza. It's not a base stanza type on its own.
    """

    def __init__(self, stanza_type, sender, recipient, error_type, conditions, id=None):
        self.stanza_type = stanza_type
        self.sender = sender
        self.recipient = recipient
        self.id = id
        self.conditions = conditions
        self.error_type = error_type

    @classmethod
    def from_stanza(cls, stanza, error_type, conditions):
        # In error stanzas sender and recipient are swapped
        return cls(stanza._stanza_type, stanza.recipient, stanza.sender, error_type, conditions, id=stanza.id)

    def to_xml_element(self):
        xml_element = domish.Element((None, self.stanza_type))
        xml_element['from'] = self.sender.uri.as_string('xmpp')
        xml_element['to'] = self.recipient.uri.as_string('xmpp')
        xml_element['type'] = 'error'
        if self.id is not None:
            xml_element['id'] = self.id
        error_element = domish.Element((None, 'error'))
        error_element['type'] = self.error_type
        [error_element.addChild(domish.Element((ns, condition))) for condition, ns in self.conditions]
        xml_element.addChild(error_element)
        return xml_element


# Protocols

class MessageProtocol(_MessageProtocol):
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


class PresenceProtocol(_PresenceProtocol):

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


# Sessions

class XMPPChatSession(object):
    local_identity = WriteOnceAttribute()
    remote_identity = WriteOnceAttribute()

    def __init__(self, local_identity, remote_identity):
        self.local_identity = local_identity
        self.remote_identity = remote_identity
        self.state = None
        self.pending_receipts = {}
        self.channel = coros.queue()
        self._proc = None
        self.xmpp_manager = XMPPManager()

    def start(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPChatSessionDidStart', sender=self, data=TimestampedNotificationData())
        self._proc = proc.spawn(self._run)
        self.state = 'started'

    def end(self):
        self.send_composing_indication('gone')
        self._clear_pending_receipts()
        self._proc.kill()
        self._proc = None
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPChatSessionDidEnd', sender=self, data=TimestampedNotificationData(originator='local'))
        self.state = 'terminated'

    def send_message(self, body, content_type='text/plain', message_id=None, use_receipt=True):
        message = ChatMessage(self.local_identity, self.remote_identity, body, content_type, id=message_id, use_receipt=use_receipt)
        self.xmpp_manager.send_stanza(message)
        if message_id is not None:
            timer = reactor.callLater(30, self._receipt_timer_expired, message_id)
            self.pending_receipts[message_id] = timer
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPChatSessionDidSendMessage', sender=self, data=TimestampedNotificationData(message=message))

    def send_composing_indication(self, state, message_id=None, use_receipt=False):
        message = ChatComposingIndication(self.local_identity, self.remote_identity, state, id=message_id, use_receipt=use_receipt)
        self.xmpp_manager.send_stanza(message)
        if message_id is not None:
            timer = reactor.callLater(30, self._receipt_timer_expired, message_id)
            self.pending_receipts[message_id] = timer
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPChatSessionDidSendMessage', sender=self, data=TimestampedNotificationData(message=message))

    def send_receipt_acknowledgement(self, receipt_id):
        message = MessageReceipt(self.local_identity, self.remote_identity, receipt_id)
        self.xmpp_manager.send_stanza(message)

    def send_error(self, stanza, error_type, conditions):
        message = ErrorStanza.from_stanza(stanza, error_type, conditions)
        self.xmpp_manager.send_stanza(message)

    def _run(self):
        notification_center = NotificationCenter()
        while True:
            item = self.channel.wait()
            if isinstance(item, ChatMessage):
                notification_center.post_notification('XMPPChatSessionGotMessage', sender=self, data=TimestampedNotificationData(message=item))
            elif isinstance(item, ChatComposingIndication):
                if item.state == 'gone':
                    self._clear_pending_receipts()
                    notification_center.post_notification('XMPPChatSessionDidEnd', sender=self, data=TimestampedNotificationData(originator='remote'))
                    self.state = 'terminated'
                    break
                else:
                    notification_center.post_notification('XMPPChatSessionGotComposingIndication', sender=self, data=TimestampedNotificationData(message=item))
            elif isinstance(item, MessageReceipt):
                if item.receipt_id in self.pending_receipts:
                    timer = self.pending_receipts.pop(item.receipt_id)
                    timer.cancel()
                    notification_center.post_notification('XMPPChatSessionDidDeliverMessage', sender=self, data=TimestampedNotificationData(message_id=item.receipt_id))
            elif isinstance(item, ErrorStanza):
                if item.id in self.pending_receipts:
                    timer = self.pending_receipts.pop(item.id)
                    timer.cancel()
                    # TODO: translate cause
                    notification_center.post_notification('XMPPChatSessionDidNotDeliverMessage', sender=self, data=TimestampedNotificationData(message_id=item.id, code=503, reason='Service Unavailable'))
        self._proc = None

    def _receipt_timer_expired(self, message_id):
        self.pending_receipts.pop(message_id)
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPChatSessionDidNotDeliverMessage', sender=self, data=TimestampedNotificationData(message_id=message_id, code=408, reason='Timeout'))
        
    def _clear_pending_receipts(self):
        notification_center = NotificationCenter()
        while self.pending_receipts:
            message_id, timer = self.pending_receipts.popitem()
            timer.cancel()
            notification_center.post_notification('XMPPChatSessionDidNotDeliverMessage', sender=self, data=TimestampedNotificationData(message_id=message_id, code=408, reason='Timeout'))


class XMPPChatSessionManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        self.sessions = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='XMPPChatSessionDidStart')
        notification_center.add_observer(self, name='XMPPChatSessionDidEnd')

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='XMPPChatSessionDidStart')
        notification_center.remove_observer(self, name='XMPPChatSessionDidEnd')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_XMPPChatSessionDidStart(self, notification):
        session = notification.sender
        self.sessions[(session.local_identity.uri, session.remote_identity.uri)] = session

    def _NH_XMPPChatSessionDidEnd(self, notification):
        session = notification.sender
        del self.sessions[(session.local_identity.uri, session.remote_identity.uri)]


# Subscriptions

class XMPPSubscription(object):
    local_identity = WriteOnceAttribute()
    remote_identity = WriteOnceAttribute()

    def __init__(self, local_identity, remote_identity):
        self.local_identity = local_identity
        self.remote_identity = remote_identity
        self.state = None
        self.channel = coros.queue()
        self._proc = None
        self.xmpp_manager = XMPPManager()

    def _set_state(self, new_state):
        prev_state = self.__dict__.get('state', None)
        self.__dict__['state'] = new_state
        if prev_state != new_state:
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPSubscriptionChangedState', sender=self, data=TimestampedNotificationData(prev_state=prev_state, state=new_state))
    def _get_state(self):
        return self.__dict__['state']
    state = property(_get_state, _set_state)
    del _get_state, _set_state

    def start(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPSubscriptionDidStart', sender=self, data=TimestampedNotificationData())
        self._proc = proc.spawn(self._run)
        self.subscribe()

    def end(self):
        if self.state == 'terminated':
            return
        self._proc.kill()
        self._proc = None
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPSubscriptionDidEnd', sender=self, data=TimestampedNotificationData(originator='local'))
        self.state = 'terminated'

    def subscribe(self):
        self.state = 'subscribe_sent'
        stanza = SubscriptionPresence(self.local_identity, self.remote_identity, 'subscribe')
        self.xmpp_manager.send_stanza(stanza)
        # If we are already subscribed we may not receive an answer, send a probe just in case
        self._send_probe()

    def unsubscribe(self):
        self.state = 'unsubscribe_sent'
        stanza = SubscriptionPresence(self.local_identity, self.remote_identity, 'unsubscribe')
        self.xmpp_manager.send_stanza(stanza)

    def _send_probe(self):
        self.state = 'subscribe_sent'
        stanza = ProbePresence(self.local_identity, self.remote_identity)
        self.xmpp_manager.send_stanza(stanza)

    def _run(self):
        notification_center = NotificationCenter()
        while True:
            item = self.channel.wait()
            if isinstance(item, AvailabilityPresence):
                if self.state == 'subscribe_sent':
                    self.state == 'active'
                notification_center.post_notification('XMPPSubscriptionGotNotify', sender=self, data=TimestampedNotificationData(presence=item))
            elif isinstance(item, SubscriptionPresence):
                if self.state == 'subscribe_sent' and item.type == 'subscribed':
                    self.state = 'active'
                elif item.type == 'unsubscribed':
                    prev_state = self.state
                    self.state = 'terminated'
                    if prev_state in ('active', 'unsubscribe_sent'):
                        notification_center.post_notification('XMPPSubscriptionDidEnd', sender=self, data=TimestampedNotificationData())
                    else:
                        notification_center.post_notification('XMPPSubscriptionDidFail', sender=self, data=TimestampedNotificationData())
                    break
        self._proc = None


class XMPPIncomingSubscription(object):
    local_identity = WriteOnceAttribute()
    remote_identity = WriteOnceAttribute()

    def __init__(self, local_identity, remote_identity):
        self.local_identity = local_identity
        self.remote_identity = remote_identity
        self.state = None
        self.channel = coros.queue()
        self._proc = None
        self.xmpp_manager = XMPPManager()

    def _set_state(self, new_state):
        prev_state = self.__dict__.get('state', None)
        self.__dict__['state'] = new_state
        if prev_state != new_state:
            notification_center = NotificationCenter()
            notification_center.post_notification('XMPPIncomingSubscriptionChangedState', sender=self, data=TimestampedNotificationData(prev_state=prev_state, state=new_state))
    def _get_state(self):
        return self.__dict__['state']
    state = property(_get_state, _set_state)
    del _get_state, _set_state

    def start(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPIncomingSubscriptionDidStart', sender=self, data=TimestampedNotificationData())
        self._proc = proc.spawn(self._run)

    def end(self):
        if self.state == 'terminated':
            return
        self.state = 'terminated'
        self._proc.kill()
        self._proc = None
        notification_center = NotificationCenter()
        notification_center.post_notification('XMPPIncomingSubscriptionDidEnd', sender=self, data=TimestampedNotificationData(originator='local'))

    def accept(self):
        self.state = 'active'
        stanza = SubscriptionPresence(self.local_identity, self.remote_identity, 'subscribed')
        self.xmpp_manager.send_stanza(stanza)

    def reject(self):
        self.state = 'terminating'
        stanza = SubscriptionPresence(self.local_identity, self.remote_identity, 'unsubscribed')
        self.xmpp_manager.send_stanza(stanza)
        self.end()
    
    def send_presence(self, stanza):
        self.xmpp_manager.send_stanza(stanza)

    def _run(self):
        notification_center = NotificationCenter()
        while True:
            item = self.channel.wait()
            if isinstance(item, SubscriptionPresence):
                if item.type == 'subscribe':
                    notification_center.post_notification('XMPPIncomingSubscriptionGotSubscribe', sender=self, data=TimestampedNotificationData())
                elif item.type == 'unsubscribe':
                    self.state = 'terminated'
                    notification_center = NotificationCenter()
                    notification_center.post_notification('XMPPIncomingSubscriptionGotUnsubscribe', sender=self, data=TimestampedNotificationData())
                    notification_center.post_notification('XMPPIncomingSubscriptionDidEnd', sender=self, data=TimestampedNotificationData(originator='local'))
                    break
            elif isinstance(item, ProbePresence):
                notification_center = NotificationCenter()
                notification_center.post_notification('XMPPIncomingSubscriptionGotProbe', sender=self, data=TimestampedNotificationData())
        self._proc = None


class XMPPSubscriptionManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        self.incoming_subscriptions = {}
        self.outgoing_subscriptions = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='XMPPSubscriptionDidStart')
        notification_center.add_observer(self, name='XMPPSubscriptionDidEnd')
        notification_center.add_observer(self, name='XMPPIncomingSubscriptionDidStart')
        notification_center.add_observer(self, name='XMPPIncomingSubscriptionDidEnd')

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='XMPPSubscriptionDidStart')
        notification_center.remove_observer(self, name='XMPPSubscriptionDidEnd')
        notification_center.remove_observer(self, name='XMPPIncomingSubscriptionDidStart')
        notification_center.remove_observer(self, name='XMPPIncomingSubscriptionDidEnd')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_XMPPSubscriptionDidStart(self, notification):
        subscription = notification.sender
        self.outgoing_subscriptions[(subscription.local_identity.uri, subscription.remote_identity.uri)] = subscription

    def _NH_XMPPSubscriptionDidEnd(self, notification):
        subscription = notification.sender
        del self.outgoing_subscriptions[(subscription.local_identity.uri, subscription.remote_identity.uri)]

    def _NH_XMPPIncomingSubscriptionDidStart(self, notification):
        subscription = notification.sender
        self.incoming_subscriptions[(subscription.local_identity.uri, subscription.remote_identity.uri)] = subscription

    def _NH_XMPPIncomingSubscriptionDidEnd(self, notification):
        subscription = notification.sender
        del self.incoming_subscriptions[(subscription.local_identity.uri, subscription.remote_identity.uri)]


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


class XMPPS2SServerFactory(_XMPPS2SServerFactory):
    def onConnectionMade(self, xs):
        super(XMPPS2SServerFactory, self).onConnectionMade(xs)

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


class DeferredS2SClientFactory(_DeferredS2SClientFactory):
    def onConnectionMade(self, xs):
        super(DeferredS2SClientFactory, self).onConnectionMade(xs)

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


