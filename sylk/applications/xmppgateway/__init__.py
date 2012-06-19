# Copyright (C) 2012 AG Projects. See LICENSE for details
#

import os

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.core import SIPURI
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage
from sipsimple.streams.applications.chat import CPIMMessage, CPIMParserError
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implements

from sylk.applications import ISylkApplication, SylkApplication, ApplicationLogger
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, generate_sylk_resource, decode_resource
from sylk.applications.xmppgateway.im import SIPMessageSender, SIPMessageError, ChatSessionHandler
from sylk.applications.xmppgateway.presence import S2XPresenceHandler, X2SPresenceHandler
from sylk.applications.xmppgateway.xmpp import XMPPManager
from sylk.applications.xmppgateway.xmpp.session import XMPPChatSession
from sylk.applications.xmppgateway.xmpp.stanzas import ChatMessage, ChatComposingIndication, NormalMessage

log = ApplicationLogger(os.path.dirname(__file__).split(os.path.sep)[-1])


class XMPPGatewayApplication(object):
    __metaclass__ = SylkApplication
    implements(ISylkApplication, IObserver)

    __appname__ = 'xmppgateway'

    def __init__(self):
        self.xmpp_manager = XMPPManager()
        self.pending_sessions = {}
        self.chat_sessions = set()
        self.s2x_presence_subscriptions = {}
        self.x2s_presence_subscriptions = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.xmpp_manager)
        self.xmpp_manager.start()

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.xmpp_manager)
        self.xmpp_manager.stop()

    def incoming_session(self, session):
        log.msg('New incoming session from %s' % session.remote_identity.uri)
        try:
            msrp_stream = (stream for stream in session.proposed_streams if stream.type=='chat').next()
        except StopIteration:
            session.reject(488, 'Only MSRP media is supported')
            return

        # Check domain
        if session.remote_identity.uri.host not in XMPPGatewayConfig.domains:
            session.reject(606, 'Not Acceptable')
            return

        # Get URI representing the SIP side
        contact_uri = session._invitation.remote_contact_header.uri
        if contact_uri.parameters.get('gr') is not None:
            sip_leg_uri = FrozenURI(contact_uri.user, contact_uri.host, contact_uri.parameters.get('gr'))
        else:
            tmp = session.remote_identity.uri
            sip_leg_uri = FrozenURI(tmp.user, tmp.host, generate_sylk_resource())

        # Get URI representing the XMPP side
        request_uri = session._invitation.request_uri
        remote_resource = request_uri.parameters.get('gr', None)
        if remote_resource is not None:
            try:
                remote_resource = decode_resource(remote_resource)
            except (TypeError, UnicodeError):
                pass
        xmpp_leg_uri = FrozenURI(request_uri.user, request_uri.host, remote_resource)

        try:
            handler = self.pending_sessions[(sip_leg_uri, xmpp_leg_uri)]
        except KeyError:
            pass
        else:
            # There is another pending session with same identifiers, can't accept this one
            session.reject(488)
            return

        sip_identity = Identity(sip_leg_uri, session.remote_identity.display_name)
        handler = ChatSessionHandler.new_from_sip_session(sip_identity, session)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=handler)
        key = (sip_leg_uri, xmpp_leg_uri)
        self.pending_sessions[key] = handler

        if xmpp_leg_uri.resource is not None:
            # Incoming session target contained GRUU, so create XMPPChatSession immediately
            xmpp_session = XMPPChatSession(local_identity=handler.sip_identity, remote_identity=Identity(xmpp_leg_uri))
            handler.xmpp_identity = xmpp_session.remote_identity
            handler.xmpp_session = xmpp_session

    def incoming_subscription(self, subscribe_request, data):
        if subscribe_request.event != 'presence':
            subscribe_request.reject(489)
            return

        # Check domain
        remote_identity_uri = data.headers['From'].uri
        if remote_identity_uri.host not in XMPPGatewayConfig.domains:
            subscribe_request.reject(606)
            return

        # Get URI representing the SIP side
        sip_leg_uri = FrozenURI(remote_identity_uri.user, remote_identity_uri.host)

        # Get URI representing the XMPP side
        request_uri = data.request_uri
        xmpp_leg_uri = FrozenURI(request_uri.user, request_uri.host)

        try:
            handler = self.s2x_presence_subscriptions[(sip_leg_uri, xmpp_leg_uri)]
        except KeyError:
            sip_identity = Identity(sip_leg_uri, data.headers['From'].display_name)
            xmpp_identity = Identity(xmpp_leg_uri)
            handler = S2XPresenceHandler(sip_identity, xmpp_identity)
            notification_center = NotificationCenter()
            notification_center.add_observer(self, sender=handler)
            handler.start()
        handler.add_sip_subscription(subscribe_request)

    def incoming_referral(self, refer_request, data):
        refer_request.reject(405)

    def incoming_sip_message(self, message_request, data):
        content_type = data.headers.get('Content-Type', Null)[0]
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        if Null in (content_type, from_header, to_header):
            message_request.answer(400)
            return
        log.msg('New incoming SIP MESSAGE from %s' % from_header.uri)

        # Check domain
        if from_header.uri.host not in XMPPGatewayConfig.domains:
            message_request.answer(606)
            return

        if content_type == 'message/cpim':
            try:
                cpim_message = CPIMMessage.parse(data.body)
            except CPIMParserError:
                message_request.answer(400)
                return
            else:
                body = cpim_message.body
                content_type = cpim_message.content_type
                sender = cpim_message.sender or from_header
                from_uri = sender.uri
        else:
            body = data.body
            from_uri = from_header.uri
        to_uri = str(to_header.uri)
        message_request.answer(200)
        if from_uri.parameters.get('gr', None) is None:
            from_uri = SIPURI.new(from_uri)
            from_uri.parameters['gr'] = generate_sylk_resource()
        sender = Identity(FrozenURI.parse(from_uri))
        recipient = Identity(FrozenURI.parse(to_uri))
        if content_type in ('text/plain', 'text/html'):
            if XMPPGatewayConfig.use_msrp_for_chat:
                message = NormalMessage(sender, recipient, body, content_type, use_receipt=False)
                self.xmpp_manager.send_stanza(message)
            else:
                message = ChatMessage(sender, recipient, body, content_type, use_receipt=False)
                self.xmpp_manager.send_stanza(message)
        elif content_type == IsComposingDocument.content_type:
            if not XMPPGatewayConfig.use_msrp_for_chat:
                try:
                    msg = IsComposingMessage.parse(body)
                except ParserError:
                    pass
                else:
                    state = 'composing' if msg.state == 'active' else 'paused'
                    message = ChatComposingIndication(sender, recipient, state, use_receipt=False)
                    self.xmpp_manager.send_stanza(message)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    # Out of band XMPP stanza handling

    @run_in_green_thread
    def _NH_XMPPGotChatMessage(self, notification):
        # This notification is only processed here untill the ChatSessionHandler
        # has both (SIP and XMPP) sessions established
        message = notification.data.message
        sender = message.sender
        recipient = message.recipient
        if XMPPGatewayConfig.use_msrp_for_chat:
            if recipient.uri.resource is None:
                # If recipient resource is not set the session is started from
                # the XMPP side
                sip_leg_uri = FrozenURI.new(recipient.uri)
                xmpp_leg_uri = FrozenURI.new(sender.uri)
                try:
                    handler = self.pending_sessions[(sip_leg_uri, xmpp_leg_uri)]
                except KeyError:
                    # Not found, need to create a new handler and a outgoing SIP session
                    xmpp_identity = Identity(xmpp_leg_uri)
                    handler = ChatSessionHandler.new_from_xmpp_stanza(xmpp_identity, sip_leg_uri)
                    key = (sip_leg_uri, xmpp_leg_uri)
                    self.pending_sessions[key] = handler
                    notification_center = NotificationCenter()
                    notification_center.add_observer(self, sender=handler)
                handler.xmpp_message_queue.append(message)
            else:
                # Find handler pending XMPP confirmation
                sip_leg_uri = FrozenURI.new(recipient.uri)
                xmpp_leg_uri = FrozenURI(sender.uri.user, sender.uri.host)
                try:
                    handler = self.pending_sessions[(sip_leg_uri, xmpp_leg_uri)]
                except KeyError:
                    # Find handler pending XMPP confirmation
                    sip_leg_uri = FrozenURI(recipient.uri.user, recipient.uri.host)
                    xmpp_leg_uri = FrozenURI.new(sender.uri)
                    try:
                        handler = self.pending_sessions[(sip_leg_uri, xmpp_leg_uri)]
                    except KeyError:
                        # It's a new XMPP session to a full JID, disregard the full JID and start a new SIP session to the bare JID
                        xmpp_identity = Identity(xmpp_leg_uri)
                        handler = ChatSessionHandler.new_from_xmpp_stanza(xmpp_identity, sip_leg_uri)
                        key = (sip_leg_uri, xmpp_leg_uri)
                        self.pending_sessions[key] = handler
                        notification_center = NotificationCenter()
                        notification_center.add_observer(self, sender=handler)
                    handler.xmpp_message_queue.append(message)
                else:
                    # Found handle, create XMPP session and establish session
                    session = XMPPChatSession(local_identity=recipient, remote_identity=sender)
                    handler.xmpp_message_queue.append(message)
                    handler.xmpp_identity = session.remote_identity
                    handler.xmpp_session = session
        else:
            sip_message_sender = SIPMessageSender(message)
            try:
                sip_message_sender.send().wait()
            except SIPMessageError as e:
                # TODO report back an error stanza
                log.error('Error sending SIP MESSAGE: %s' % e)

    @run_in_green_thread
    def _NH_XMPPGotNormalMessage(self, notification):
        message = notification.data.message
        sip_message_sender = SIPMessageSender(message)
        try:
            sip_message_sender.send().wait()
        except SIPMessageError as e:
            # TODO report back an error stanza
            log.error('Error sending SIP MESSAGE: %s' % e)

    @run_in_green_thread
    def _NH_XMPPGotComposingIndication(self, notification):
        composing_indication = notification.data.composing_indication
        sender = composing_indication.sender
        recipient = composing_indication.recipient
        if not XMPPGatewayConfig.use_msrp_for_chat:
            state = 'active' if composing_indication.state == 'composing' else 'idle'
            body = IsComposingMessage(state=state, refresh=composing_indication.interval or 30).toxml()
            message = NormalMessage(sender, recipient, body, IsComposingDocument.content_type)
            sip_message_sender = SIPMessageSender(message)
            try:
                sip_message_sender.send().wait()
            except SIPMessageError as e:
                # TODO report back an error stanza
                log.error('Error sending SIP MESSAGE: %s' % e)

    def _NH_XMPPGotPresenceSubscriptionRequest(self, notification):
        stanza = notification.data.stanza
        # Disregard the resource part, the presence request could be a probe instead of a subscribe
        sender_uri = stanza.sender.uri
        sender_uri_bare = FrozenURI(sender_uri.user, sender_uri.host)
        try:
            handler = self.x2s_presence_subscriptions[(sender_uri_bare, stanza.recipient.uri)]
        except KeyError:
            xmpp_identity = stanza.sender
            xmpp_identity.uri = sender_uri_bare
            sip_identity = stanza.recipient
            handler = X2SPresenceHandler(sip_identity, xmpp_identity)
            notification_center = NotificationCenter()
            notification_center.add_observer(self, sender=handler)
            handler.start()

    # Chat session handling

    def _NH_ChatSessionDidStart(self, notification):
        handler = notification.sender
        log.msg('Chat session established sip:%s <--> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        for k,v in self.pending_sessions.items():
            if v is handler:
                del self.pending_sessions[k]
                break
        self.chat_sessions.add(handler)

    def _NH_ChatSessionDidEnd(self, notification):
        handler = notification.sender
        log.msg('Chat session ended sip:%s <--> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        self.chat_sessions.remove(handler)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=handler)

    def _NH_ChatSessionDidFail(self, notification):
        handler = notification.sender
        uris = None
        for k,v in self.pending_sessions.items():
            if v is handler:
                uris = k
                del self.pending_sessions[k]
                break
        sip_uri, xmpp_uri = uris
        log.msg('Chat session failed sip:%s <--> xmpp:%s' % (sip_uri, xmpp_uri))
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=handler)

    # Presence handling

    def _NH_S2XPresenceHandlerDidStart(self, notification):
        handler = notification.sender
        log.msg('Presence session established sip:%s --> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        self.s2x_presence_subscriptions[(handler.sip_identity.uri, handler.xmpp_identity.uri)] = handler

    def _NH_S2XPresenceHandlerDidEnd(self, notification):
        handler = notification.sender
        log.msg('Presence session ended sip:%s --> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        self.s2x_presence_subscriptions.pop((handler.sip_identity.uri, handler.xmpp_identity.uri), None)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=handler)

    def _NH_X2SPresenceHandlerDidStart(self, notification):
        handler = notification.sender
        log.msg('Presence session established xmpp:%s --> sip:%s' % (handler.xmpp_identity.uri, handler.sip_identity.uri))
        self.x2s_presence_subscriptions[(handler.xmpp_identity.uri, handler.sip_identity.uri)] = handler

    def _NH_X2SPresenceHandlerDidEnd(self, notification):
        handler = notification.sender
        log.msg('Presence session ended xmpp:%s --> sip:%s' % (handler.xmpp_identity.uri, handler.sip_identity.uri))
        self.x2s_presence_subscriptions.pop((handler.xmpp_identity.uri, handler.sip_identity.uri), None)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=handler)


