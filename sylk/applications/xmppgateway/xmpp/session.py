
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.descriptor import WriteOnceAttribute
from application.python.types import Singleton
from eventlib import coros, proc
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications.xmppgateway.xmpp.stanzas import ChatMessage, ChatComposingIndication, MessageReceipt, ErrorStanza, GroupChatMessage, MUCAvailabilityPresence

__all__ = ['XMPPChatSession', 'XMPPChatSessionManager', 'XMPPIncomingMucSession', 'XMPPMucSessionManager']


# Chat sessions

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
        from sylk.applications.xmppgateway.xmpp import XMPPManager
        self.xmpp_manager = XMPPManager()

    def start(self):
        NotificationCenter().post_notification('XMPPChatSessionDidStart', sender=self)
        self._proc = proc.spawn(self._run)
        self.state = 'started'

    def end(self):
        self.send_composing_indication('gone')
        self._clear_pending_receipts()
        self._proc.kill()
        self._proc = None
        NotificationCenter().post_notification('XMPPChatSessionDidEnd', sender=self, data=NotificationData(originator='local'))
        self.state = 'terminated'

    def send_message(self, body, html_body, message_id=None, use_receipt=True):
        message = ChatMessage(self.local_identity, self.remote_identity, body, html_body, id=message_id, use_receipt=use_receipt)
        self.xmpp_manager.send_stanza(message)
        if message_id is not None:
            timer = reactor.callLater(30, self._receipt_timer_expired, message_id)
            self.pending_receipts[message_id] = timer
            NotificationCenter().post_notification('XMPPChatSessionDidSendMessage', sender=self, data=NotificationData(message=message))

    def send_composing_indication(self, state, message_id=None, use_receipt=False):
        message = ChatComposingIndication(self.local_identity, self.remote_identity, state, id=message_id, use_receipt=use_receipt)
        self.xmpp_manager.send_stanza(message)
        if message_id is not None:
            timer = reactor.callLater(30, self._receipt_timer_expired, message_id)
            self.pending_receipts[message_id] = timer
            NotificationCenter().post_notification('XMPPChatSessionDidSendMessage', sender=self, data=NotificationData(message=message))

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
                notification_center.post_notification('XMPPChatSessionGotMessage', sender=self, data=NotificationData(message=item))
            elif isinstance(item, ChatComposingIndication):
                if item.state == 'gone':
                    self._clear_pending_receipts()
                    notification_center.post_notification('XMPPChatSessionDidEnd', sender=self, data=NotificationData(originator='remote'))
                    self.state = 'terminated'
                    break
                else:
                    notification_center.post_notification('XMPPChatSessionGotComposingIndication', sender=self, data=NotificationData(message=item))
            elif isinstance(item, MessageReceipt):
                if item.receipt_id in self.pending_receipts:
                    timer = self.pending_receipts.pop(item.receipt_id)
                    timer.cancel()
                    notification_center.post_notification('XMPPChatSessionDidDeliverMessage', sender=self, data=NotificationData(message_id=item.receipt_id))
            elif isinstance(item, ErrorStanza):
                if item.id in self.pending_receipts:
                    timer = self.pending_receipts.pop(item.id)
                    timer.cancel()
                    # TODO: translate cause
                    notification_center.post_notification('XMPPChatSessionDidNotDeliverMessage', sender=self, data=NotificationData(message_id=item.id, code=503, reason='Service Unavailable'))
        self._proc = None

    def _receipt_timer_expired(self, message_id):
        self.pending_receipts.pop(message_id)
        NotificationCenter().post_notification('XMPPChatSessionDidNotDeliverMessage', sender=self, data=NotificationData(message_id=message_id, code=408, reason='Timeout'))

    def _clear_pending_receipts(self):
        notification_center = NotificationCenter()
        while self.pending_receipts:
            message_id, timer = self.pending_receipts.popitem()
            timer.cancel()
            notification_center.post_notification('XMPPChatSessionDidNotDeliverMessage', sender=self, data=NotificationData(message_id=message_id, code=408, reason='Timeout'))


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


# MUC sessions

class XMPPIncomingMucSession(object):
    local_identity = WriteOnceAttribute()
    remote_identity = WriteOnceAttribute()

    def __init__(self, local_identity, remote_identity):
        self.local_identity = local_identity
        self.remote_identity = remote_identity
        self.state = None
        self.channel = coros.queue()
        self._proc = None
        from sylk.applications.xmppgateway.xmpp import XMPPManager
        self.xmpp_manager = XMPPManager()

    def start(self):
        NotificationCenter().post_notification('XMPPIncomingMucSessionDidStart', sender=self)
        self._proc = proc.spawn(self._run)
        self.state = 'started'

    def end(self):
        self._proc.kill()
        self._proc = None
        NotificationCenter().post_notification('XMPPIncomingMucSessionDidEnd', sender=self, data=NotificationData(originator='local'))
        self.state = 'terminated'

    def send_message(self, sender, body, html_body, message_id=None):
        # TODO: timestamp?
        message = GroupChatMessage(sender, self.remote_identity, body, html_body, id=message_id)
        self.xmpp_manager.send_muc_stanza(message)

    def _run(self):
        notification_center = NotificationCenter()
        while True:
            item = self.channel.wait()
            if isinstance(item, GroupChatMessage):
                notification_center.post_notification('XMPPIncomingMucSessionGotMessage', sender=self, data=NotificationData(message=item))
            elif isinstance(item, MUCAvailabilityPresence):
                if item.available:
                    nickname = item.recipient.uri.resource
                    notification_center.post_notification('XMPPIncomingMucSessionChangedNickname', sender=self, data=NotificationData(stanza=item, nickname=nickname))
                else:
                    notification_center.post_notification('XMPPIncomingMucSessionDidEnd', sender=self, data=NotificationData(originator='local'))
                    self.state = 'terminated'
                    break
        self._proc = None


class XMPPMucSessionManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        self.incoming = {}
        self.outgoing = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='XMPPIncomingMucSessionDidStart')
        notification_center.add_observer(self, name='XMPPIncomingMucSessionDidEnd')

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='XMPPIncomingMucSessionDidStart')
        notification_center.remove_observer(self, name='XMPPIncomingMucSessionDidEnd')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_XMPPIncomingMucSessionDidStart(self, notification):
        muc = notification.sender
        self.incoming[(muc.local_identity.uri, muc.remote_identity.uri)] = muc

    def _NH_XMPPIncomingMucSessionDidEnd(self, notification):
        muc = notification.sender
        del self.incoming[(muc.local_identity.uri, muc.remote_identity.uri)]

