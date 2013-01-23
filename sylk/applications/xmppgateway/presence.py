# Copyright (C) 2012 AG Projects. See LICENSE for details
#

import hashlib
import os
import random

from application.notification import IObserver, NotificationCenter
from application.python import Null, limit
from application.python.descriptor import WriteOnceAttribute
from eventlib import coros, proc
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import Engine, SIPURI, SIPCoreError
from sipsimple.core import ContactHeader, FromHeader, RouteHeader, ToHeader
from sipsimple.core import Subscription
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads import pidf, rpid, caps
from sipsimple.payloads import ParserError
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import Command, run_in_green_thread
from sipsimple.util import ISOTimestamp
from time import time
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import ApplicationLogger
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, encode_resource
from sylk.applications.xmppgateway.util import format_uri
from sylk.applications.xmppgateway.xmpp.stanzas import AvailabilityPresence
from sylk.applications.xmppgateway.xmpp.subscription import XMPPSubscription, XMPPIncomingSubscription
from sylk.configuration import SIPConfig

log = ApplicationLogger(os.path.dirname(__file__).split(os.path.sep)[-1])


__all__ = ['S2XPresenceHandler', 'X2SPresenceHandler']


class S2XPresenceHandler(object):
    implements(IObserver)

    sip_identity = WriteOnceAttribute()
    xmpp_identity = WriteOnceAttribute()

    def __init__(self, sip_identity, xmpp_identity):
        self.ended = False
        self._sip_subscriptions = []
        self._stanza_cache = {}
        self._pidf = None
        self._xmpp_subscription = None
        self.sip_identity = sip_identity
        self.xmpp_identity = xmpp_identity

    def start(self):
        notification_center = NotificationCenter()
        self._xmpp_subscription = XMPPSubscription(local_identity=self.sip_identity, remote_identity=self.xmpp_identity)
        notification_center.add_observer(self, sender=self._xmpp_subscription)
        self._xmpp_subscription.start()
        notification_center.post_notification('S2XPresenceHandlerDidStart', sender=self)

    def end(self):
        if self.ended:
            return
        notification_center = NotificationCenter()
        if self._xmpp_subscription is not None:
            notification_center.remove_observer(self, sender=self._xmpp_subscription)
            self._xmpp_subscription.end()
            self._xmpp_subscription = None
        while self._sip_subscriptions:
            subscription = self._sip_subscriptions.pop()
            notification_center.remove_observer(self, sender=subscription)
            try:
                subscription.end()
            except SIPCoreError:
                pass
        self.ended = True
        notification_center.post_notification('S2XPresenceHandlerDidEnd', sender=self)

    def add_sip_subscription(self, subscription):
        self._sip_subscriptions.append(subscription)
        NotificationCenter().add_observer(self, sender=subscription)
        if self._xmpp_subscription.state == 'active':
            pidf_doc = self._pidf
            content_type = pidf.PIDFDocument.content_type if pidf_doc is not None else None
            subscription.accept(content_type, pidf_doc)
        else:
            subscription.accept_pending()
        log.msg('SIP subscription from %s to %s added to presence flow 0x%x (%d subs)' % (format_uri(self.sip_identity.uri, 'sip'), format_uri(self.xmpp_identity.uri, 'xmpp'), id(self), len(self._sip_subscriptions)))

    def _build_pidf(self):
        if not self._stanza_cache:
            self._pidf = None
            return None
        pidf_doc = pidf.PIDF(str(self.xmpp_identity))
        uri = self._stanza_cache.iterkeys().next()
        person = pidf.Person("PID-%s" % hashlib.md5("%s@%s" % (uri.user, uri.host)).hexdigest())
        person.activities = rpid.Activities()
        pidf_doc.add(person)
        for stanza in self._stanza_cache.itervalues():
            if not stanza.available:
                status = pidf.Status('closed')
                status.extended = 'offline'
            else:
                status = pidf.Status('open')
                if stanza.show == 'away':
                    status.extended = 'away'
                    if 'away' not in person.activities:
                        person.activities.add('away')
                elif stanza.show == 'xa':
                    status.extended = 'away'
                    if 'away' not in person.activities:
                        person.activities.add('away')
                elif stanza.show == 'dnd':
                    status.extended = 'busy'
                    if 'busy' not in person.activities:
                        person.activities.add('busy')
                else:
                    status.extended = 'available'
            if stanza.sender.uri.resource:
                resource = encode_resource(stanza.sender.uri.resource)
            else:
                # Workaround for clients not sending the resource under certain (unknown) circumstances
                resource = hashlib.md5("%s@%s" % (uri.user, uri.host)).hexdigest()
            service_id = "SID-%s" % resource
            sip_uri = stanza.sender.uri.as_sip_uri()
            sip_uri.parameters['gr'] = resource
            sip_uri.parameters['xmpp'] = None
            contact = pidf.Contact(str(sip_uri))
            service = pidf.Service(service_id, status=status, contact=contact)
            service.add(pidf.DeviceID(resource))
            service.device_info = pidf.DeviceInfo(resource, description=stanza.sender.uri.resource)
            service.timestamp = pidf.ServiceTimestamp(stanza.timestamp)
            service.capabilities = caps.ServiceCapabilities(text=True, message=True)
            for lang, note in stanza.statuses.iteritems():
                service.notes.add(pidf.PIDFNote(note, lang=lang))
            pidf_doc.add(service)
        if not person.activities:
            person.activities = None
        self._pidf = pidf_doc.toxml()
        return self._pidf

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_twisted_thread
    def _NH_SIPIncomingSubscriptionDidEnd(self, notification):
        subscription = notification.sender
        notification.center.remove_observer(self, sender=subscription)
        self._sip_subscriptions.remove(subscription)
        if not self._sip_subscriptions:
            self.end()
        log.msg('SIP subscription from %s to %s removed from presence flow 0x%x (%d subs)' % (format_uri(self.sip_identity.uri, 'sip'), format_uri(self.xmpp_identity.uri, 'xmpp'), id(self), len(self._sip_subscriptions)))

    def _NH_SIPIncomingSubscriptionNotifyDidFail(self, notification):
        log.msg('Sending SIP NOTIFY failed from %s to %s for presence flow 0x%x: %s (%s)' % (format_uri(self.xmpp_identity.uri, 'xmpp'), format_uri(self.sip_identity.uri, 'sip'), id(self), notification.data.code, notification.data.reason))

    def _NH_XMPPSubscriptionChangedState(self, notification):
        if notification.data.prev_state == 'subscribe_sent' and notification.data.state == 'active':
            pidf_doc = self._pidf
            content_type = pidf.PIDFDocument.content_type if pidf_doc is not None else None
            for subscription in (subscription for subscription in self._sip_subscriptions if subscription.state == 'pending'):
                subscription.accept(content_type, pidf_doc)

    def _NH_XMPPSubscriptionGotNotify(self, notification):
        stanza = notification.data.presence
        self._stanza_cache[stanza.sender.uri] = stanza
        stanza.timestamp = ISOTimestamp.now()    # TODO: mirror the one in the stanza, if present
        pidf_doc = self._build_pidf()
        log.msg('Got XMPP NOTIFY from %s to %s for presence flow 0x%x' % (format_uri(self.xmpp_identity.uri, 'xmpp'), format_uri(self.sip_identity.uri, 'sip'), id(self)))
        for subscription in self._sip_subscriptions:
            try:
                subscription.push_content(pidf.PIDFDocument.content_type, pidf_doc)
            except SIPCoreError, e:
                log.msg('Failed to send SIP NOTIFY from %s to %s for presence flow 0x%x: %s' % (format_uri(self.xmpp_identity.uri, 'xmpp'), format_uri(self.sip_identity.uri, 'sip'), id(self), e))
        if not stanza.available:
            # Only inform once about this device being unavailable
            del self._stanza_cache[stanza.sender.uri]

    def _NH_XMPPSubscriptionDidFail(self, notification):
        notification.center.remove_observer(self, sender=self._xmpp_subscription)
        self._xmpp_subscription = None
        self.end()

    _NH_XMPPSubscriptionDidEnd = _NH_XMPPSubscriptionDidFail


class InterruptSubscription(Exception): pass

class TerminateSubscription(Exception): pass

class SubscriptionError(Exception):
    def __init__(self, error, timeout, refresh_interval=None, fatal=False):
        self.error = error
        self.refresh_interval = refresh_interval
        self.timeout = timeout
        self.fatal = fatal

class SIPSubscriptionDidFail(Exception):
    def __init__(self, data):
        self.data = data

class X2SPresenceHandler(object):
    implements(IObserver)

    sip_identity = WriteOnceAttribute()
    xmpp_identity = WriteOnceAttribute()

    def __init__(self, sip_identity, xmpp_identity):
        self.ended = False
        self.sip_identity = sip_identity
        self.xmpp_identity = xmpp_identity
        self.subscribed = False
        self._command_proc = None
        self._command_channel = coros.queue()
        self._data_channel = coros.queue()
        self._sip_subscription = None
        self._sip_subscription_proc = None
        self._sip_subscription_timer = None
        self._xmpp_subscription = None

    def start(self):
        notification_center = NotificationCenter()
        self._xmpp_subscription = XMPPIncomingSubscription(local_identity=self.sip_identity, remote_identity=self.xmpp_identity)
        notification_center.add_observer(self, sender=self._xmpp_subscription)
        self._xmpp_subscription.start()
        self._command_proc = proc.spawn(self._run)
        self._subscribe_sip()
        notification_center.post_notification('X2SPresenceHandlerDidStart', sender=self)

    def end(self):
        if self.ended:
            return
        notification_center = NotificationCenter()
        if self._xmpp_subscription is not None:
            notification_center.remove_observer(self, sender=self._xmpp_subscription)
            self._xmpp_subscription.end()
            self._xmpp_subscription = None
        if self._sip_subscription:
            self._unsubscribe_sip()
        self.ended = True
        notification_center.post_notification('X2SPresenceHandlerDidEnd', sender=self)

    @run_in_green_thread
    def _subscribe_sip(self):
        command = Command('subscribe')
        self._command_channel.send(command)

    @run_in_green_thread
    def _unsubscribe_sip(self):
        command = Command('unsubscribe')
        self._command_channel.send(command)
        command.wait()
        self._command_proc.kill()
        self._command_proc = None

    def _run(self):
        while True:
            command = self._command_channel.wait()
            handler = getattr(self, '_CH_%s' % command.name)
            handler(command)

    def _CH_subscribe(self, command):
        if self._sip_subscription_timer is not None and self._sip_subscription_timer.active():
            self._sip_subscription_timer.cancel()
        self._sip_subscription_timer = None
        if self._sip_subscription_proc is not None:
            subscription_proc = self._sip_subscription_proc
            subscription_proc.kill(InterruptSubscription)
            subscription_proc.wait()
        self._sip_subscription_proc = proc.spawn(self._sip_subscription_handler, command)

    def _CH_unsubscribe(self, command):
        # Cancel any timer which would restart the subscription process
        if self._sip_subscription_timer is not None and self._sip_subscription_timer.active():
            self._sip_subscription_timer.cancel()
        self._sip_subscription_timer = None
        if self._sip_subscription_proc is not None:
            subscription_proc = self._sip_subscription_proc
            subscription_proc.kill(TerminateSubscription)
            subscription_proc.wait()
            self._sip_subscription_proc = None
        command.signal()

    def _process_pidf(self, body):
        try:
            pidf_doc = pidf.PIDF.parse(body)
        except ParserError, e:
            log.warn('Error parsing PIDF document: %s' % e)
            return
        # Build XML stanzas out of PIDF documents
        try:
            person = (p for p in pidf_doc.persons).next()
        except StopIteration:
            person = None
        for service in pidf_doc.services:
            sip_contact = self.sip_identity.uri.as_sip_uri()
            if service.device_info is not None:
                sip_contact.parameters['gr'] = 'urn:uuid:%s' % service.device_info.id
            else:
                sip_contact.parameters['gr'] = service.id
            sender = Identity(FrozenURI.parse(sip_contact))
            if service.status.extended is not None:
                available = service.status.extended != 'offline'
            else:
                available = service.status.basic == 'open'
            stanza = AvailabilityPresence(sender, self.xmpp_identity, available)
            for note in service.notes:
                stanza.statuses[note.lang] = note
            if service.status.extended is not None:
                if service.status.extended == 'away':
                    stanza.show = 'away'
                elif service.status.extended == 'busy':
                    stanza.show = 'dnd'
            elif person is not None and person.activities is not None:
                activities = set(list(person.activities))
                if 'away' in activities:
                    stanza.show = 'away'
                elif set(('holiday', 'vacation')).intersection(activities):
                    stanza.show = 'xa'
                elif 'busy' in activities:
                    stanza.show = 'dnd'
            self._xmpp_subscription.send_presence(stanza)

    def _sip_subscription_handler(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        account = AccountManager().sylkserver_account
        refresh_interval =  getattr(command, 'refresh_interval', None) or account.sip.subscribe_interval

        try:
            # Lookup routes
            if account.sip.outbound_proxy is not None:
                uri = SIPURI(host=account.sip.outbound_proxy.host,
                             port=account.sip.outbound_proxy.port,
                             parameters={'transport': account.sip.outbound_proxy.transport})
            else:
                uri = SIPURI(host=self.sip_identity.uri.as_sip_uri().host)
            lookup = DNSLookup()
            try:
                routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
            except DNSLookupError, e:
                timeout = random.uniform(15, 30)
                raise SubscriptionError(error='DNS lookup failed: %s' % e, timeout=timeout)

            timeout = time() + 30
            for route in routes:
                remaining_time = timeout - time()
                if remaining_time > 0:
                    transport = route.transport
                    parameters = {} if transport=='udp' else {'transport': transport}
                    contact_uri = SIPURI(user=account.contact.username, host=SIPConfig.local_ip.normalized, port=getattr(Engine(), '%s_port' % transport), parameters=parameters)
                    subscription_uri = self.sip_identity.uri.as_sip_uri()
                    subscription = Subscription(subscription_uri, FromHeader(self.xmpp_identity.uri.as_sip_uri()),
                                                ToHeader(subscription_uri),
                                                ContactHeader(contact_uri),
                                                'presence',
                                                RouteHeader(route.uri),
                                                refresh=refresh_interval)
                    notification_center.add_observer(self, sender=subscription)
                    try:
                        subscription.subscribe(timeout=limit(remaining_time, min=1, max=5))
                    except SIPCoreError:
                        notification_center.remove_observer(self, sender=subscription)
                        raise SubscriptionError(error='Internal error', timeout=5)
                    self._sip_subscription = subscription
                    try:
                        while True:
                            notification = self._data_channel.wait()
                            if notification.sender is subscription and notification.name == 'SIPSubscriptionDidStart':
                                break
                    except SIPSubscriptionDidFail, e:
                        notification_center.remove_observer(self, sender=subscription)
                        self._sip_subscription = None
                        if e.data.code == 407:
                            # Authentication failed, so retry the subscription in some time
                            raise SubscriptionError(error='Authentication failed', timeout=random.uniform(60, 120))
                        elif e.data.code == 403:
                            # Forbidden
                            raise SubscriptionError(error='Forbidden', timeout=None, fatal=True)
                        elif e.data.code == 423:
                            # Get the value of the Min-Expires header
                            if e.data.min_expires is not None and e.data.min_expires > refresh_interval:
                                interval = e.data.min_expires
                            else:
                                interval = None
                            raise SubscriptionError(error='Interval too short', timeout=random.uniform(60, 120), refresh_interval=interval)
                        elif e.data.code in (405, 406, 489):
                            raise SubscriptionError(error='Method or event not supported', timeout=None, fatal=True)
                        elif e.data.code == 1400:
                            raise SubscriptionError(error=e.data.reason, timeout=None, fatal=True)
                        else:
                            # Otherwise just try the next route
                            continue
                    else:
                        self.subscribed = True
                        command.signal()
                        break
            else:
                # There are no more routes to try, give up
                raise SubscriptionError(error='No more routes to try', timeout=None, fatal=True)
            # At this point it is subscribed. Handle notifications and ending/failures.
            try:
                while True:
                    notification = self._data_channel.wait()
                    if notification.sender is not self._sip_subscription:
                        continue
                    if self._xmpp_subscription is None:
                        continue
                    if notification.name == 'SIPSubscriptionGotNotify':
                        if notification.data.event == 'presence':
                            subscription_state = notification.data.headers.get('Subscription-State').state
                            if subscription_state == 'active' and self._xmpp_subscription.state != 'active':
                                self._xmpp_subscription.accept()
                            elif subscription_state == 'pending' and self._xmpp_subscription.state == 'active':
                                # The state went from active to pending, hide the presence state?
                                pass
                            if notification.data.body:
                                log.msg('Got SIP NOTIFY from %s to %s' % (format_uri(self.sip_identity.uri, 'sip'), format_uri(self.xmpp_identity.uri, 'xmpp')))
                                self._process_pidf(notification.data.body)
                    elif notification.name == 'SIPSubscriptionDidEnd':
                        break
            except SIPSubscriptionDidFail, e:
                if e.data.code == 0 and e.data.reason == 'rejected':
                    self._xmpp_subscription.reject()
                else:
                    self._command_channel.send(Command('subscribe'))
            notification_center.remove_observer(self, sender=self._sip_subscription)
        except InterruptSubscription, e:
            if not self.subscribed:
                command.signal(e)
            if self._sip_subscription is not None:
                notification_center.remove_observer(self, sender=self._sip_subscription)
                try:
                    self._sip_subscription.end(timeout=2)
                except SIPCoreError:
                    pass
        except TerminateSubscription, e:
            if not self.subscribed:
                command.signal(e)
            if self._sip_subscription is not None:
                try:
                    self._sip_subscription.end(timeout=2)
                except SIPCoreError:
                    pass
                else:
                    try:
                        while True:
                            notification = self._data_channel.wait()
                            if notification.sender is self._sip_subscription and notification.name == 'SIPSubscriptionDidEnd':
                                break
                    except SIPSubscriptionDidFail:
                        pass
                finally:
                    notification_center.remove_observer(self, sender=self._sip_subscription)
        except SubscriptionError, e:
            if not e.fatal:
                self._sip_subscription_timer = reactor.callLater(e.timeout, self._command_channel.send, Command('subscribe', command.event, refresh_interval=e.refresh_interval))
        finally:
            self.subscribed = False
            self._sip_subscription = None
            self._sip_subscription_proc = None
            reactor.callLater(0, self.end)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSubscriptionDidStart(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSubscriptionDidEnd(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSubscriptionDidFail(self, notification):
        self._data_channel.send_exception(SIPSubscriptionDidFail(notification.data))

    def _NH_SIPSubscriptionGotNotify(self, notification):
        self._data_channel.send(notification)

    def _NH_XMPPIncomingSubscriptionGotUnsubscribe(self, notification):
        self.end()

    def _NH_XMPPIncomingSubscriptionGotSubscribe(self, notification):
        if self._sip_subscription is not None and self._sip_subscription.state.lower() == 'active':
            self._xmpp_subscription.accept()

    _NH_XMPPIncomingSubscriptionGotProbe = _NH_XMPPIncomingSubscriptionGotSubscribe


