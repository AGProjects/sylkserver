# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage
from sipsimple.streams.applications.chat import CPIMMessage, CPIMParserError
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implements

from sylk.applications import SylkApplication
from sylk.applications.xmppgateway.configuration import XMPPGatewayConfig
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, generate_sylk_resource, decode_resource
from sylk.applications.xmppgateway.im import SIPMessageSender, SIPMessageError, ChatSessionHandler
from sylk.applications.xmppgateway.logger import log
from sylk.applications.xmppgateway.presence import S2XPresenceHandler, X2SPresenceHandler
from sylk.applications.xmppgateway.media import MediaSessionHandler
from sylk.applications.xmppgateway.muc import X2SMucInvitationHandler, S2XMucInvitationHandler, X2SMucHandler
from sylk.applications.xmppgateway.util import format_uri
from sylk.applications.xmppgateway.xmpp import XMPPManager
from sylk.applications.xmppgateway.xmpp.session import XMPPChatSession
from sylk.applications.xmppgateway.xmpp.stanzas import ChatMessage, ChatComposingIndication, NormalMessage


class XMPPGatewayApplication(SylkApplication):
    implements(IObserver)

    def __init__(self):
        self.xmpp_manager = XMPPManager()
        self.pending_sessions = {}
        self.chat_sessions = set()
        self.media_sessions = set()
        self.s2x_muc_sessions = {}
        self.x2s_muc_sessions = {}
        self.s2x_presence_subscriptions = {}
        self.x2s_presence_subscriptions = {}
        self.s2x_muc_add_participant_handlers = {}
        self.x2s_muc_add_participant_handlers = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.xmpp_manager)
        notification_center.add_observer(self, name='JingleSessionNewIncoming')
        self.xmpp_manager.start()

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.xmpp_manager)
        notification_center.add_observer(self, name='JingleSessionNewIncoming')
        self.xmpp_manager.stop()

    def incoming_session(self, session):
        stream_types = set([stream.type for stream in session.proposed_streams])
        if 'chat' in stream_types:
            log.msg('New chat session from %s to %s' % (session.remote_identity.uri, session.local_identity.uri))
            self.incoming_chat_session(session)
        elif 'audio' in stream_types:
            log.msg('New audio session from %s to %s' % (session.remote_identity.uri, session.local_identity.uri))
            self.incoming_media_session(session)
        else:
            log.msg('New session from %s to %s rejected. Unsupported media: %s ' % (session.remote_identity.uri, session.local_identity.uri, stream_types))
            session.reject(488)

    def incoming_chat_session(self, session):
        # Check if this session is really an invitation to add a participant to a conference room / muc
        if session.remote_identity.uri.host in self.xmpp_manager.muc_domains and 'isfocus' in session._invitation.remote_contact_header.parameters:
            try:
                referred_by_uri = SIPURI.parse(session.transfer_info.referred_by)
            except SIPCoreError:
                log.msg("SIP multiparty session invitation %s failed: invalid Referred-By header" % session._invitation.call_id)
                session.reject(488)
                return
            muc_uri = FrozenURI(session.remote_identity.uri.user, session.remote_identity.uri.host)
            inviter_uri = FrozenURI(referred_by_uri.user, referred_by_uri.host)
            recipient_uri = FrozenURI(session.local_identity.uri.user, session.local_identity.uri.host)
            sender = Identity(muc_uri)
            recipient = Identity(recipient_uri)
            inviter = Identity(inviter_uri)
            try:
                handler = self.s2x_muc_add_participant_handlers[(muc_uri, recipient_uri)]
            except KeyError:
                handler = S2XMucInvitationHandler(session, sender, recipient, inviter)
                self.s2x_muc_add_participant_handlers[(muc_uri, recipient_uri)] = handler
                NotificationCenter().add_observer(self, sender=handler)
                handler.start()
            else:
                log.msg("SIP multiparty session invitation %s failed: there is another invitation in progress from %s to %s" % (session._invitation.call_id,
                                                                                                                                format_uri(inviter_uri, 'sip'),
                                                                                                                                format_uri(recipient_uri, 'xmpp')))
                session.reject(480)
            return

        # Check domain
        if session.remote_identity.uri.host not in XMPPGatewayConfig.domains:
            log.msg('Session rejected: From domain is not a local XMPP domain')
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
                remote_resource = None
        xmpp_leg_uri = FrozenURI(request_uri.user, request_uri.host, remote_resource)

        try:
            handler = self.pending_sessions[(sip_leg_uri, xmpp_leg_uri)]
        except KeyError:
            pass
        else:
            # There is another pending session with same identifiers, can't accept this one
            log.msg('Session rejected: other session with same identifiers in progress')
            session.reject(488)
            return

        sip_identity = Identity(sip_leg_uri, session.remote_identity.display_name)
        handler = ChatSessionHandler.new_from_sip_session(sip_identity, session)
        NotificationCenter().add_observer(self, sender=handler)
        key = (sip_leg_uri, xmpp_leg_uri)
        self.pending_sessions[key] = handler

        if xmpp_leg_uri.resource is not None:
            # Incoming session target contained GRUU, so create XMPPChatSession immediately
            xmpp_session = XMPPChatSession(local_identity=handler.sip_identity, remote_identity=Identity(xmpp_leg_uri))
            handler.xmpp_identity = xmpp_session.remote_identity
            handler.xmpp_session = xmpp_session

    def incoming_media_session(self, session):
        if session.remote_identity.uri.host not in self.xmpp_manager.domains|self.xmpp_manager.muc_domains:
            log.msg('Session rejected: From domain is not a local XMPP domain')
            session.reject(403)
            return

        handler = MediaSessionHandler.new_from_sip_session(session)
        if handler is not None:
            NotificationCenter().add_observer(self, sender=handler)

    def incoming_subscription(self, subscribe_request, data):
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        if Null in (from_header, to_header):
            subscribe_request.reject(400)
            return

        log.msg('SIP subscription from %s to %s' % (format_uri(from_header.uri, 'sip'), format_uri(to_header.uri, 'xmpp')))

        if subscribe_request.event != 'presence':
            log.msg('SIP subscription rejected: only presence event is supported')
            subscribe_request.reject(489)
            return

        # Check domain
        remote_identity_uri = data.headers['From'].uri
        if remote_identity_uri.host not in XMPPGatewayConfig.domains:
            log.msg('SIP subscription rejected: From domain is not a local XMPP domain')
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
            self.s2x_presence_subscriptions[(sip_leg_uri, xmpp_leg_uri)] = handler
            NotificationCenter().add_observer(self, sender=handler)
            handler.start()

        handler.add_sip_subscription(subscribe_request)

    def incoming_referral(self, refer_request, data):
        refer_request.reject(405)

    def incoming_message(self, message_request, data):
        content_type = data.headers.get('Content-Type', Null).content_type
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)
        if Null in (content_type, from_header, to_header):
            message_request.answer(400)
            return
        log.msg('New SIP Message from %s to %s' % (from_header.uri, to_header.uri))

        # Check domain
        if from_header.uri.host not in XMPPGatewayConfig.domains:
            log.msg('Message rejected: From domain is not a local XMPP domain')
            message_request.answer(606)
            return

        if content_type == 'message/cpim':
            try:
                cpim_message = CPIMMessage.parse(data.body)
            except CPIMParserError:
                log.msg('Message rejected: CPIM parse error')
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
            if content_type == 'text/plain':
                html_body = None
            else:
                html_body = body
                body = None
            if XMPPGatewayConfig.use_msrp_for_chat:
                message = NormalMessage(sender, recipient, body, html_body, use_receipt=False)
                self.xmpp_manager.send_stanza(message)
            else:
                message = ChatMessage(sender, recipient, body, html_body, use_receipt=False)
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
                    NotificationCenter().add_observer(self, sender=handler)
                handler.enqueue_xmpp_message(message)
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
                        NotificationCenter().add_observer(self, sender=handler)
                    handler.enqueue_xmpp_message(message)
                else:
                    # Found handle, create XMPP session and establish session
                    session = XMPPChatSession(local_identity=recipient, remote_identity=sender)
                    handler.enqueue_xmpp_message(message)
                    handler.xmpp_identity = session.remote_identity
                    handler.xmpp_session = session
        else:
            sip_message_sender = SIPMessageSender(message)
            try:
                sip_message_sender.send().wait()
            except SIPMessageError as e:
                # TODO report back an error stanza
                log.error('Error sending SIP Message: %s' % e)

    @run_in_green_thread
    def _NH_XMPPGotNormalMessage(self, notification):
        message = notification.data.message
        sip_message_sender = SIPMessageSender(message)
        try:
            sip_message_sender.send().wait()
        except SIPMessageError as e:
            # TODO report back an error stanza
            log.error('Error sending SIP Message: %s' % e)

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
                log.error('Error sending SIP Message: %s' % e)

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
            self.x2s_presence_subscriptions[(sender_uri_bare, stanza.recipient.uri)] = handler
            notification.center.add_observer(self, sender=handler)
            handler.start()

    def _NH_XMPPGotMucJoinRequest(self, notification):
        stanza = notification.data.stanza
        muc_uri = FrozenURI(stanza.recipient.uri.user, stanza.recipient.uri.host)
        nickname = stanza.recipient.uri.resource
        try:
            handler = self.x2s_muc_sessions[(stanza.sender.uri, muc_uri)]
        except KeyError:
            xmpp_identity = stanza.sender
            sip_identity = stanza.recipient
            sip_identity.uri = muc_uri
            handler = X2SMucHandler(sip_identity, xmpp_identity, nickname)
            handler._first_stanza = stanza
            notification.center.add_observer(self, sender=handler)
            handler.start()
            # Check if there was a pending join request on the SIP side
            try:
                handler = self.s2x_muc_add_participant_handlers[(muc_uri, FrozenURI(stanza.sender.uri.user, stanza.sender.uri.host))]
            except KeyError:
                pass
            else:
                handler.stop()

    def _NH_XMPPGotMucAddParticipantRequest(self, notification):
        sender = notification.data.sender
        recipient = notification.data.recipient
        participant = notification.data.participant
        muc_uri = FrozenURI(recipient.uri.user, recipient.uri.host)
        sender_uri = FrozenURI(sender.uri.user, sender.uri.host)
        participant_uri = FrozenURI(participant.uri.user, participant.uri.host)
        sender = Identity(sender_uri)
        recipient = Identity(muc_uri)
        participant = Identity(participant_uri)
        try:
            handler = self.x2s_muc_add_participant_handlers[(muc_uri, participant_uri)]
        except KeyError:
            handler = X2SMucInvitationHandler(sender, recipient, participant)
            self.x2s_muc_add_participant_handlers[(muc_uri, participant_uri)] = handler
            notification.center.add_observer(self, sender=handler)
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
        notification.center.remove_observer(self, sender=handler)

    def _NH_ChatSessionDidFail(self, notification):
        handler = notification.sender
        uris = None
        for k,v in self.pending_sessions.items():
            if v is handler:
                uris = k
                del self.pending_sessions[k]
                break
        sip_uri, xmpp_uri = uris
        log.msg('Chat session failed sip:%s <--> xmpp:%s (%s)' % (sip_uri, xmpp_uri, notification.data.reason))
        notification.center.remove_observer(self, sender=handler)

    # Presence handling

    def _NH_S2XPresenceHandlerDidStart(self, notification):
        handler = notification.sender
        log.msg('Presence flow 0x%x established %s --> %s' % (id(handler), format_uri(handler.sip_identity.uri, 'sip'), format_uri(handler.xmpp_identity.uri, 'xmpp')))
        log.msg('%d SIP --> XMPP and %d XMPP --> SIP presence flows are active' % (len(self.s2x_presence_subscriptions.keys()), len(self.x2s_presence_subscriptions.keys())))

    def _NH_S2XPresenceHandlerDidEnd(self, notification):
        handler = notification.sender
        self.s2x_presence_subscriptions.pop((handler.sip_identity.uri, handler.xmpp_identity.uri), None)
        notification.center.remove_observer(self, sender=handler)
        log.msg('Presence flow 0x%x ended %s --> %s' % (id(handler), format_uri(handler.sip_identity.uri, 'sip'), format_uri(handler.xmpp_identity.uri, 'xmpp')))
        log.msg('%d SIP --> XMPP and %d XMPP --> SIP presence flows are active' % (len(self.s2x_presence_subscriptions.keys()), len(self.x2s_presence_subscriptions.keys())))

    def _NH_X2SPresenceHandlerDidStart(self, notification):
        handler = notification.sender
        log.msg('Presence flow 0x%x established %s --> %s' % (id(handler), format_uri(handler.xmpp_identity.uri, 'xmpp'), format_uri(handler.sip_identity.uri, 'sip')))
        log.msg('%d SIP --> XMPP and %d XMPP --> SIP presence flows are active' % (len(self.s2x_presence_subscriptions.keys()), len(self.x2s_presence_subscriptions.keys())))

    def _NH_X2SPresenceHandlerDidEnd(self, notification):
        handler = notification.sender
        self.x2s_presence_subscriptions.pop((handler.xmpp_identity.uri, handler.sip_identity.uri), None)
        notification.center.remove_observer(self, sender=handler)
        log.msg('Presence flow 0x%x ended %s --> %s' % (id(handler), format_uri(handler.xmpp_identity.uri, 'xmpp'), format_uri(handler.sip_identity.uri, 'sip')))
        log.msg('%d SIP --> XMPP and %d XMPP --> SIP presence flows are active' % (len(self.s2x_presence_subscriptions.keys()), len(self.x2s_presence_subscriptions.keys())))

    # MUC handling

    def _NH_X2SMucHandlerDidStart(self, notification):
        handler = notification.sender
        log.msg('Multiparty session established xmpp:%s --> sip:%s' % (handler.xmpp_identity.uri, handler.sip_identity.uri))
        self.x2s_muc_sessions[(handler.xmpp_identity.uri, handler.sip_identity.uri)] = handler

    def _NH_X2SMucHandlerDidEnd(self, notification):
        handler = notification.sender
        log.msg('Multiparty session ended xmpp:%s --> sip:%s' % (handler.xmpp_identity.uri, handler.sip_identity.uri))
        self.x2s_muc_sessions.pop((handler.xmpp_identity.uri, handler.sip_identity.uri), None)
        notification.center.remove_observer(self, sender=handler)

    def _NH_X2SMucInvitationHandlerDidStart(self, notification):
        handler = notification.sender
        sender_uri = handler.sender.uri
        muc_uri = handler.recipient.uri
        participant_uri = handler.participant.uri
        log.msg('%s invited %s to multiparty chat %s' % (format_uri(sender_uri, 'xmpp'), format_uri(participant_uri), format_uri(muc_uri, 'sip')))

    def _NH_X2SMucInvitationHandlerDidEnd(self, notification):
        handler = notification.sender
        sender_uri = handler.sender.uri
        muc_uri = handler.recipient.uri
        participant_uri = handler.participant.uri
        log.msg('%s added %s to multiparty chat %s' % (format_uri(sender_uri, 'xmpp'), format_uri(participant_uri), format_uri(muc_uri, 'sip')))
        del self.x2s_muc_add_participant_handlers[(muc_uri, participant_uri)]
        notification.center.remove_observer(self, sender=handler)

    def _NH_X2SMucInvitationHandlerDidFail(self, notification):
        handler = notification.sender
        sender_uri = handler.sender.uri
        muc_uri = handler.recipient.uri
        participant_uri = handler.participant.uri
        log.msg('%s could not add %s to multiparty chat %s: %s' % (format_uri(sender_uri, 'xmpp'), format_uri(participant_uri), format_uri(muc_uri, 'sip'), notification.data.failure))
        del self.x2s_muc_add_participant_handlers[(muc_uri, participant_uri)]
        notification.center.remove_observer(self, sender=handler)

    def _NH_S2XMucInvitationHandlerDidStart(self, notification):
        handler = notification.sender
        muc_uri = handler.sender.uri
        inviter_uri = handler.inviter.uri
        recipient_uri = handler.recipient.uri
        log.msg("%s invited %s to multiparty chat %s" % (format_uri(inviter_uri, 'sip'), format_uri(recipient_uri, 'xmpp'), format_uri(muc_uri, 'sip')))

    def _NH_S2XMucInvitationHandlerDidEnd(self, notification):
        handler = notification.sender
        muc_uri = handler.sender.uri
        inviter_uri = handler.inviter.uri
        recipient_uri = handler.recipient.uri
        log.msg('%s added %s to multiparty chat %s' % (format_uri(inviter_uri, 'sip'), format_uri(recipient_uri, 'xmpp'), format_uri(muc_uri, 'sip')))
        del self.s2x_muc_add_participant_handlers[(muc_uri, recipient_uri)]
        notification.center.remove_observer(self, sender=handler)

    def _NH_S2XMucInvitationHandlerDidFail(self, notification):
        handler = notification.sender
        muc_uri = handler.sender.uri
        inviter_uri = handler.inviter.uri
        recipient_uri = handler.recipient.uri
        log.msg('%s could not add %s to multiparty chat %s: %s' % (format_uri(inviter_uri, 'sip'), format_uri(recipient_uri, 'xmpp'), format_uri(muc_uri, 'sip'), str(notification.data.failure)))
        del self.s2x_muc_add_participant_handlers[(muc_uri, recipient_uri)]
        notification.center.remove_observer(self, sender=handler)

    # Media sessions

    def _NH_JingleSessionNewIncoming(self, notification):
        session = notification.sender
        handler = MediaSessionHandler.new_from_jingle_session(session)
        if handler is not None:
            notification.center.add_observer(self, sender=handler)

    def _NH_MediaSessionHandlerDidStart(self, notification):
        handler = notification.sender
        log.msg('Media session started sip:%s <--> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        self.media_sessions.add(handler)

    def _NH_MediaSessionHandlerDidEnd(self, notification):
        handler = notification.sender
        log.msg('Media session ended sip:%s <--> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        self.media_sessions.remove(handler)
        notification.center.remove_observer(self, sender=handler)

    def _NH_MediaSessionHandlerDidFail(self, notification):
        handler = notification.sender
        log.msg('Media session failed sip:%s <--> xmpp:%s' % (handler.sip_identity.uri, handler.xmpp_identity.uri))
        notification.center.remove_observer(self, sender=handler)

