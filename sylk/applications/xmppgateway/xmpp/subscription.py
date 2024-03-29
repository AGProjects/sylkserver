
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.descriptor import WriteOnceAttribute
from application.python.types import Singleton
from eventlib import coros, proc
from zope.interface import implementer

from sylk.applications.xmppgateway.xmpp.stanzas import SubscriptionPresence, ProbePresence, AvailabilityPresence


__all__ = 'XMPPSubscription', 'XMPPIncomingSubscription', 'XMPPSubscriptionManager'


class XMPPSubscription(object):
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

    @property
    def state(self):
        return self.__dict__['state']

    @state.setter
    def state(self, new_state):
        prev_state = self.__dict__.get('state', None)
        self.__dict__['state'] = new_state
        if prev_state != new_state:
            NotificationCenter().post_notification('XMPPSubscriptionChangedState', sender=self, data=NotificationData(prev_state=prev_state, state=new_state))

    def start(self):
        NotificationCenter().post_notification('XMPPSubscriptionDidStart', sender=self)
        self._proc = proc.spawn(self._run)
        self.subscribe()

    def end(self):
        if self.state == 'terminated':
            return
        self._proc.kill()
        self._proc = None
        NotificationCenter().post_notification('XMPPSubscriptionDidEnd', sender=self, data=NotificationData(originator='local'))
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
                    self.state = 'active'
                notification_center.post_notification('XMPPSubscriptionGotNotify', sender=self, data=NotificationData(presence=item))
            elif isinstance(item, SubscriptionPresence):
                if self.state == 'subscribe_sent' and item.type == 'subscribed':
                    self.state = 'active'
                elif item.type == 'unsubscribed':
                    prev_state = self.state
                    self.state = 'terminated'
                    if prev_state in ('active', 'unsubscribe_sent'):
                        notification_center.post_notification('XMPPSubscriptionDidEnd', sender=self)
                    else:
                        notification_center.post_notification('XMPPSubscriptionDidFail', sender=self)
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
        from sylk.applications.xmppgateway.xmpp import XMPPManager
        self.xmpp_manager = XMPPManager()

    @property
    def state(self):
        return self.__dict__['state']

    @state.setter
    def state(self, new_state):
        prev_state = self.__dict__.get('state', None)
        self.__dict__['state'] = new_state
        if prev_state != new_state:
            NotificationCenter().post_notification('XMPPIncomingSubscriptionChangedState', sender=self, data=NotificationData(prev_state=prev_state, state=new_state))

    def start(self):
        NotificationCenter().post_notification('XMPPIncomingSubscriptionDidStart', sender=self)
        self._proc = proc.spawn(self._run)

    def end(self):
        if self.state == 'terminated':
            return
        self.state = 'terminated'
        self._proc.kill()
        self._proc = None
        NotificationCenter().post_notification('XMPPIncomingSubscriptionDidEnd', sender=self, data=NotificationData(originator='local'))

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
                    notification_center.post_notification('XMPPIncomingSubscriptionGotSubscribe', sender=self)
                elif item.type == 'unsubscribe':
                    self.state = 'terminated'
                    notification_center = NotificationCenter()
                    notification_center.post_notification('XMPPIncomingSubscriptionGotUnsubscribe', sender=self)
                    notification_center.post_notification('XMPPIncomingSubscriptionDidEnd', sender=self, data=NotificationData(originator='local'))
                    break
            elif isinstance(item, ProbePresence):
                notification_center = NotificationCenter()
                notification_center.post_notification('XMPPIncomingSubscriptionGotProbe', sender=self)
        self._proc = None


@implementer(IObserver)
class XMPPSubscriptionManager(object, metaclass=Singleton):

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

