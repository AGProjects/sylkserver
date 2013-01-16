# Copyright (C) 2012 AG Projects. See LICENSE for details
#

import os

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.descriptor import WriteOnceAttribute
from collections import deque
from eventlib import coros
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.core import ContactHeader, FromHeader, RouteHeader, ToHeader
from sipsimple.core import Message as SIPMessageRequest
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread, run_in_waitable_green_thread
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import ApplicationLogger
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, generate_sylk_resource, encode_resource
from sylk.applications.xmppgateway.xmpp import XMPPManager
from sylk.applications.xmppgateway.xmpp.session import XMPPChatSession
from sylk.applications.xmppgateway.xmpp.stanzas import ChatMessage
from sylk.extensions import ChatStream
from sylk.session import Session

log = ApplicationLogger(os.path.dirname(__file__).split(os.path.sep)[-1])


__all__ = ['ChatSessionHandler', 'SIPMessageSender', 'SIPMessageError']


SESSION_TIMEOUT = XMPPGatewayConfig.sip_session_timeout


class ChatSessionHandler(object):
    implements(IObserver)

    sip_identity = WriteOnceAttribute()
    xmpp_identity = WriteOnceAttribute()

    def __init__(self):
        self.started = False
        self.ended = False
        self.sip_session = None
        self.msrp_stream = None
        self._sip_session_timer = None

        self.use_receipts = False
        self.xmpp_session = None
        self._xmpp_message_queue = deque()

        self._pending_msrp_chunks = {}
        self._pending_xmpp_stanzas = {}

    def _set_started(self, value):
        old_value = self.__dict__.get('started', False)
        self.__dict__['started'] = value
        if not old_value and value:
            NotificationCenter().post_notification('ChatSessionDidStart', sender=self)
            self._send_queued_messages()
    def _get_started(self):
        return self.__dict__['started']
    started = property(_get_started, _set_started)
    del _get_started, _set_started

    def _set_xmpp_session(self, session):
        self.__dict__['xmpp_session'] = session
        if session is not None:
            # Reet SIP session timer in case it's active
            if self._sip_session_timer is not None and self._sip_session_timer.active():
                self._sip_session_timer.reset(SESSION_TIMEOUT)
            NotificationCenter().add_observer(self, sender=session)
            session.start()
            # Reet SIP session timer in case it's active
            if self._sip_session_timer is not None and self._sip_session_timer.active():
                self._sip_session_timer.reset(SESSION_TIMEOUT)
    def _get_xmpp_session(self):
        return self.__dict__['xmpp_session']
    xmpp_session = property(_get_xmpp_session, _set_xmpp_session)
    del _get_xmpp_session, _set_xmpp_session

    @classmethod
    def new_from_sip_session(cls, sip_identity, session):
        instance = cls()
        instance.sip_identity = sip_identity
        instance._start_incoming_sip_session(session)
        return instance

    @classmethod
    def new_from_xmpp_stanza(cls, xmpp_identity, recipient):
        instance = cls()
        instance.xmpp_identity = xmpp_identity
        instance._start_outgoing_sip_session(recipient)
        return instance

    @run_in_green_thread
    def _start_incoming_sip_session(self, session):
        self.sip_session = session
        self.msrp_stream = (stream for stream in session.proposed_streams if stream.type=='chat').next()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.sip_session)
        notification_center.add_observer(self, sender=self.msrp_stream)
        self.sip_session.accept([self.msrp_stream])

    @run_in_green_thread
    def _start_outgoing_sip_session(self, target_uri):
        notification_center = NotificationCenter()
        # self.xmpp_identity is our local identity
        from_uri = self.xmpp_identity.uri.as_sip_uri()
        del from_uri.parameters['gr']    # no GRUU in From header
        contact_uri = self.xmpp_identity.uri.as_sip_uri()
        contact_uri.parameters['gr'] = encode_resource(contact_uri.parameters['gr'].decode('utf-8'))
        to_uri = target_uri.as_sip_uri()
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        account = AccountManager().sylkserver_account
        if account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host,
                         port=account.sip.outbound_proxy.port,
                         parameters={'transport': account.sip.outbound_proxy.transport})
        else:
            uri = to_uri
        try:
            routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
        except DNSLookupError:
            log.warning('DNS lookup error while looking for %s proxy' % uri)
            notification_center.post_notification('ChatSessionDidFail', sender=self, data=NotificationData(reason='DNS lookup error'))
            return
        self.msrp_stream = ChatStream()
        route = routes.pop(0)
        from_header = FromHeader(from_uri)
        to_header = ToHeader(to_uri)
        contact_header = ContactHeader(contact_uri)
        self.sip_session = Session(account)
        notification_center.add_observer(self, sender=self.sip_session)
        notification_center.add_observer(self, sender=self.msrp_stream)
        self.sip_session.connect(from_header, to_header, contact_header=contact_header, routes=[route], streams=[self.msrp_stream])

    def end(self):
        if self.ended:
            return
        if self._sip_session_timer is not None and self._sip_session_timer.active():
            self._sip_session_timer.cancel()
        self._sip_session_timer = None
        notification_center = NotificationCenter()
        if self.sip_session is not None:
            notification_center.remove_observer(self, sender=self.sip_session)
            notification_center.remove_observer(self, sender=self.msrp_stream)
            self.sip_session.end()
            self.sip_session = None
            self.msrp_stream = None
        if self.xmpp_session is not None:
            notification_center.remove_observer(self, sender=self.xmpp_session)
            self.xmpp_session.end()
            self.xmpp_session = None
        self.ended = True
        if self.started:
            notification_center.post_notification('ChatSessionDidEnd', sender=self)
        else:
            notification_center.post_notification('ChatSessionDidFail', sender=self, data=NotificationData(reason='Ended before actually started'))

    def enqueue_xmpp_message(self, message):
        if self.started:
            raise RuntimeError('session is already started')
        self._xmpp_message_queue.append(message)

    def _send_queued_messages(self):
        if self._xmpp_message_queue:
            while self._xmpp_message_queue:
                message = self._xmpp_message_queue.popleft()
                if message.body is None:
                    continue
                if not message.use_receipt:
                    success_report = 'no'
                    failure_report = 'no'
                else:
                    success_report = 'yes'
                    failure_report = 'yes'
                sender_uri = message.sender.uri.as_sip_uri()
                sender_uri.parameters['gr'] = encode_resource(sender_uri.parameters['gr'].decode('utf-8'))
                sender = CPIMIdentity(sender_uri)
                self.msrp_stream.send_message(message.body, 'text/plain', local_identity=sender, message_id=message.id, notify_progress=True, success_report=success_report, failure_report=failure_report)
            self.msrp_stream.send_composing_indication('idle', 30, local_identity=sender)

    def _inactivity_timeout(self):
        log.msg("Ending SIP session %s due to inactivity" % self.sip_session._invitation.call_id)
        self.sip_session.end()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        log.msg("SIP session %s started" % notification.sender._invitation.call_id)
        self._sip_session_timer = reactor.callLater(SESSION_TIMEOUT, self._inactivity_timeout)

        if self.sip_session.direction == 'outgoing':
            # Time to set sip_identity and create the XMPPChatSession
            contact_uri = self.sip_session._invitation.remote_contact_header.uri
            if contact_uri.parameters.get('gr') is not None:
                sip_leg_uri = FrozenURI(contact_uri.user, contact_uri.host, contact_uri.parameters.get('gr'))
            else:
                tmp = self.sip_session.remote_identity.uri
                sip_leg_uri = FrozenURI(tmp.user, tmp.host, generate_sylk_resource())
            self.sip_identity = Identity(sip_leg_uri, self.sip_session.remote_identity.display_name)
            session = XMPPChatSession(local_identity=self.sip_identity, remote_identity=self.xmpp_identity)
            self.xmpp_session = session
            # Session is now established on both ends
            self.started = True
            # Try to wakeup XMPP clients
            self.xmpp_session.send_composing_indication('active')
            self.xmpp_session.send_message(' ', 'text/plain')
        else:
            if self.xmpp_session is not None:
                # Session is now established on both ends
                self.started = True
                # Try to wakeup XMPP clients
                self.xmpp_session.send_composing_indication('active')
                self.xmpp_session.send_message(' ', 'text/plain')
            else:
                # Try to wakeup XMPP clients
                sender = self.sip_identity
                tmp = self.sip_session.local_identity.uri
                recipient_uri = FrozenURI(tmp.user, tmp.host)
                recipient = Identity(recipient_uri)
                xmpp_manager = XMPPManager()
                xmpp_manager.send_stanza(ChatMessage(sender, recipient, ' ', 'text/plain'))
                # Send queued messages
                self._send_queued_messages()

    def _NH_SIPSessionDidEnd(self, notification):
        log.msg("SIP session %s ended" % notification.sender._invitation.call_id)
        notification.center.remove_observer(self, sender=self.sip_session)
        notification.center.remove_observer(self, sender=self.msrp_stream)
        self.sip_session = None
        self.msrp_stream = None
        self.end()

    def _NH_SIPSessionDidFail(self, notification):
        log.msg("SIP session %s failed" % notification.sender._invitation.call_id)
        notification.center.remove_observer(self, sender=self.sip_session)
        notification.center.remove_observer(self, sender=self.msrp_stream)
        self.sip_session = None
        self.msrp_stream = None
        self.end()

    def _NH_SIPSessionGotProposal(self, notification):
        self.sip_session.reject_proposal()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        self.sip_session.reject_transfer(403)

    def _NH_ChatStreamGotMessage(self, notification):
        # Notification is sent by the MSRP stream
        message = notification.data.message
        content_type = message.content_type.lower()
        if content_type not in ('text/plain', 'text/html'):
            return
        if content_type == 'text/plain':
            html_body = None
            body = message.body
        else:
            html_body = message.body
            body = None
        if self._sip_session_timer is not None and self._sip_session_timer.active():
            self._sip_session_timer.reset(SESSION_TIMEOUT)
        chunk = notification.data.chunk
        if self.started:
            self.xmpp_session.send_message(body, html_body, message_id=chunk.message_id)
            if self.use_receipts:
                self._pending_msrp_chunks[chunk.message_id] = chunk
            else:
                self.msrp_stream.msrp_session.send_report(chunk, 200, 'OK')
        else:
            sender = self.sip_identity
            recipient_uri = FrozenURI.parse(message.recipients[0].uri)
            recipient = Identity(recipient_uri, message.recipients[0].display_name)
            xmpp_manager = XMPPManager()
            xmpp_manager.send_stanza(ChatMessage(sender, recipient, body, html_body))
            self.msrp_stream.msrp_session.send_report(chunk, 200, 'OK')

    def _NH_ChatStreamGotComposingIndication(self, notification):
        # Notification is sent by the MSRP stream
        if self._sip_session_timer is not None and self._sip_session_timer.active():
            self._sip_session_timer.reset(SESSION_TIMEOUT)
        if not self.started:
            return
        state = None
        if notification.data.state == 'active':
            state = 'composing'
        elif notification.data.state == 'idle':
            state = 'paused'
        if state is not None:
            self.xmpp_session.send_composing_indication(state)

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        if self.started:
            message = self._pending_xmpp_stanzas.pop(notification.data.message_id, None)
            if message is not None:
                self.xmpp_session.send_receipt_acknowledgement(message.id)

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        if self.started:
            message = self._pending_xmpp_stanzas.pop(notification.data.message_id, None)
            if message is not None:
                self.xmpp_session.send_error(message, 'TODO', [])    # TODO

    def _NH_XMPPChatSessionDidStart(self, notification):
        if self.sip_session is not None:
            # Session is now established on both ends
            self.started = True

    def _NH_XMPPChatSessionDidEnd(self, notification):
        notification.center.remove_observer(self, sender=self.xmpp_session)
        self.xmpp_session = None
        self.end()

    def _NH_XMPPChatSessionGotMessage(self, notification):
        if self.sip_session is None or self.sip_session.state != 'connected':
            self._xmpp_message_queue.append(notification.data.message)
            return
        if self._sip_session_timer is not None and self._sip_session_timer.active():
            self._sip_session_timer.reset(SESSION_TIMEOUT)
        message = notification.data.message
        sender_uri = message.sender.uri.as_sip_uri()
        del sender_uri.parameters['gr']    # no GRUU in CPIM From header
        sender = CPIMIdentity(sender_uri)
        self.use_receipts = message.use_receipt
        if not message.use_receipt:
            success_report = 'no'
            failure_report = 'no'
        else:
            success_report = 'yes'
            failure_report = 'yes'
            self._pending_xmpp_stanzas[message.id] = message
        # Prefer plaintext
        self.msrp_stream.send_message(message.body, 'text/plain', local_identity=sender, message_id=message.id, notify_progress=True, success_report=success_report, failure_report=failure_report)
        self.msrp_stream.send_composing_indication('idle', 30, local_identity=sender)

    def _NH_XMPPChatSessionGotComposingIndication(self, notification):
        if self.sip_session is None or self.sip_session.state != 'connected':
            return
        if self._sip_session_timer is not None and self._sip_session_timer.active():
            self._sip_session_timer.reset(SESSION_TIMEOUT)
        message = notification.data.message
        state = None
        if message.state == 'composing':
            state = 'active'
        elif message.state == 'paused':
            state = 'idle'
        if state is not None:
            sender_uri = message.sender.uri.as_sip_uri()
            del sender_uri.parameters['gr']    # no GRUU in CPIM From header
            sender = CPIMIdentity(sender_uri)
            self.msrp_stream.send_composing_indication(state, 30, local_identity=sender)
            if message.use_receipt:
                self.xmpp_session.send_receipt_acknowledgement(message.id)

    def _NH_XMPPChatSessionDidDeliverMessage(self, notification):
        chunk = self._pending_msrp_chunks.pop(notification.data.message_id, None)
        if chunk is not None:
            self.msrp_stream.msrp_session.send_report(chunk, 200, 'OK')

    def _NH_XMPPChatSessionDidNotDeliverMessage(self, notification):
        chunk = self._pending_msrp_chunks.pop(notification.data.message_id, None)
        if chunk is not None:
            self.msrp_stream.msrp_session.send_report(chunk, notification.data.code, notification.data.reason)


def chunks(text, size):
    for i in xrange(0, len(text), size):
        yield text[i:i+size]

class SIPMessageError(Exception):
    def __init__(self, code, reason):
        Exception.__init__(self, reason)
        self.code = code
        self.reason = reason

class SIPMessageSender(object):
    implements(IObserver)

    def __init__(self, message):
        # TODO: sometimes we may want to send it to the GRUU, for example when a XMPP client
        # replies to one of our messages. MESSAGE requests don't need a Contact header, though
        # so how should we communicate our GRUU to the recipient?
        self.from_uri = message.sender.uri.as_sip_uri()
        self.from_uri.parameters.pop('gr', None)    # No GRUU in From header
        self.to_uri = message.recipient.uri.as_sip_uri()
        self.to_uri.parameters.pop('gr', None)      # Don't send it to the GRUU
        self.body = message.body
        self.content_type = 'text/plain'
        self._requests = set()
        self._channel = coros.queue()

    @run_in_waitable_green_thread
    def send(self):
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        account = AccountManager().sylkserver_account
        if account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host,
                         port=account.sip.outbound_proxy.port,
                         parameters={'transport': account.sip.outbound_proxy.transport})
        else:
            uri = self.to_uri
        try:
            routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
        except DNSLookupError:
            msg = 'DNS lookup error while looking for %s proxy' % uri
            log.warning(msg)
            raise SIPMessageError(0, msg)
        else:
            route = routes.pop(0)
            from_header = FromHeader(self.from_uri)
            to_header = ToHeader(self.to_uri)
            route_header = RouteHeader(route.uri)
            notification_center = NotificationCenter()
            for chunk in chunks(self.body, 1000):
                request = SIPMessageRequest(from_header, to_header, route_header, self.content_type, self.body)
                notification_center.add_observer(self, sender=request)
                self._requests.add(request)
                request.send()
            error = None
            count = len(self._requests)
            while count > 0:
                notification = self._channel.wait()
                if notification.name == 'SIPMessageDidFail':
                    error = (notification.data.code, notification.data.reason)
                count -= 1
            self._requests.clear()
            if error is not None:
                raise SIPMessageError(*error)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPMessageDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self._channel.send(notification)

    def _NH_SIPMessageDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self._channel.send(notification)


