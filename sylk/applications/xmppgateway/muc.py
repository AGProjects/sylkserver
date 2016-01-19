
import random
import uuid

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null, limit
from application.python.descriptor import WriteOnceAttribute
from eventlib import coros, proc
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import Engine, SIPURI, SIPCoreError, Referral, sip_status_messages
from sipsimple.core import ContactHeader, FromHeader, ToHeader, ReferToHeader, RouteHeader
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.msrp.chat import ChatStreamError, ChatIdentity
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from time import time
from twisted.internet import reactor
from zope.interface import implements

from sylk.accounts import DefaultAccount
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, encode_resource
from sylk.applications.xmppgateway.logger import log
from sylk.applications.xmppgateway.xmpp import XMPPManager
from sylk.applications.xmppgateway.xmpp.session import XMPPIncomingMucSession
from sylk.applications.xmppgateway.xmpp.stanzas import MUCAvailabilityPresence, MUCErrorPresence, OutgoingInvitationMessage, STANZAS_NS
from sylk.configuration import SIPConfig
from sylk.session import Session


class ReferralError(Exception):
    def __init__(self, error, code=0):
        self.error = error
        self.code = code


class SIPReferralDidFail(Exception):
    def __init__(self, data):
        self.data = data


class MucInvitationFailure(object):
    def __init__(self, code, reason):
        self.code = code
        self.reason = reason
    def __str__(self):
        return '%s (%s)' % (self.code, self.reason)


class X2SMucInvitationHandler(object):
    implements(IObserver)

    def __init__(self, sender, recipient, participant):
        self.sender = sender
        self.recipient = recipient
        self.participant = participant
        self.active = False
        self.route = None
        self._channel = coros.queue()
        self._referral = None
        self._failure = None

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='NetworkConditionsDidChange')
        proc.spawn(self._run)
        notification_center.post_notification('X2SMucInvitationHandlerDidStart', sender=self)

    def _run(self):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        sender_uri = self.sender.uri.as_sip_uri()
        recipient_uri = self.recipient.uri.as_sip_uri()
        participant_uri = self.participant.uri.as_sip_uri()

        try:
            # Lookup routes
            account = DefaultAccount()
            if account.sip.outbound_proxy is not None and account.sip.outbound_proxy.transport in settings.sip.transport_list:
                uri = SIPURI(host=account.sip.outbound_proxy.host, port=account.sip.outbound_proxy.port, parameters={'transport': account.sip.outbound_proxy.transport})
            elif account.sip.always_use_my_proxy:
                uri = SIPURI(host=account.id.domain)
            else:
                uri = SIPURI.new(recipient_uri)
            lookup = DNSLookup()
            try:
                routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
            except DNSLookupError, e:
                timeout = random.uniform(15, 30)
                raise ReferralError(error='DNS lookup failed: %s' % e)

            timeout = time() + 30
            for route in routes:
                self.route = route
                remaining_time = timeout - time()
                if remaining_time > 0:
                    transport = route.transport
                    parameters = {} if transport=='udp' else {'transport': transport}
                    contact_uri = SIPURI(user=account.contact.username, host=SIPConfig.local_ip.normalized, port=getattr(Engine(), '%s_port' % transport), parameters=parameters)
                    refer_to_header = ReferToHeader(str(participant_uri))
                    refer_to_header.parameters['method'] = 'INVITE'
                    referral = Referral(recipient_uri, FromHeader(sender_uri),
                                        ToHeader(recipient_uri),
                                        refer_to_header,
                                        ContactHeader(contact_uri),
                                        RouteHeader(route.uri),
                                        account.credentials)
                    notification_center.add_observer(self, sender=referral)
                    try:
                        referral.send_refer(timeout=limit(remaining_time, min=1, max=5))
                    except SIPCoreError:
                        notification_center.remove_observer(self, sender=referral)
                        timeout = 5
                        raise ReferralError(error='Internal error')
                    self._referral = referral
                    try:
                        while True:
                            notification = self._channel.wait()
                            if notification.name == 'SIPReferralDidStart':
                                break
                    except SIPReferralDidFail, e:
                        notification_center.remove_observer(self, sender=referral)
                        self._referral = None
                        if e.data.code in (403, 405):
                            raise ReferralError(error=sip_status_messages[e.data.code], code=e.data.code)
                        else:
                            # Otherwise just try the next route
                            continue
                    else:
                        break
            else:
                self.route = None
                raise ReferralError(error='No more routes to try')
            # At this point it is subscribed. Handle notifications and ending/failures.
            try:
                self.active = True
                while True:
                    notification = self._channel.wait()
                    if notification.name == 'SIPReferralDidEnd':
                        break
            except SIPReferralDidFail, e:
                notification_center.remove_observer(self, sender=self._referral)
                raise ReferralError(error=e.data.reason, code=e.data.code)
            else:
                notification_center.remove_observer(self, sender=self._referral)
            finally:
                self.active = False
        except ReferralError, e:
            self._failure = MucInvitationFailure(e.code, e.error)
        finally:
            notification_center.remove_observer(self, name='NetworkConditionsDidChange')
            self._referral = None
            if self._failure is not None:
                notification_center.post_notification('X2SMucInvitationHandlerDidFail', sender=self, data=NotificationData(failure=self._failure))
            else:
                notification_center.post_notification('X2SMucInvitationHandlerDidEnd', sender=self)

    def _refresh(self):
        account = DefaultAccount()
        transport = self.route.transport
        parameters = {} if transport=='udp' else {'transport': transport}
        contact_uri = SIPURI(user=account.contact.username, host=SIPConfig.local_ip.normalized, port=getattr(Engine(), '%s_port' % transport), parameters=parameters)
        contact_header = ContactHeader(contact_uri)
        self._referral.refresh(contact_header=contact_header, timeout=2)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPReferralDidStart(self, notification):
        self._channel.send(notification)

    def _NH_SIPReferralDidEnd(self, notification):
        self._channel.send(notification)

    def _NH_SIPReferralDidFail(self, notification):
        self._channel.send_exception(SIPReferralDidFail(notification.data))

    def _NH_SIPReferralGotNotify(self, notification):
        self._channel.send(notification)

    def _NH_NetworkConditionsDidChange(self, notification):
        if self.active:
            self._refresh()


class S2XMucInvitationHandler(object):
    implements(IObserver)

    def __init__(self, session, sender, recipient, inviter):
        self.session = session
        self.sender = sender
        self.recipient = recipient
        self.inviter = inviter
        self._timer = None
        self._failure = None

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)
        stanza = OutgoingInvitationMessage(self.sender, self.recipient, self.inviter, id='MUC.'+uuid.uuid4().hex)
        xmpp_manager = XMPPManager()
        xmpp_manager.send_muc_stanza(stanza)
        self._timer = reactor.callLater(90, self._timeout)
        notification_center.post_notification('S2XMucInvitationHandlerDidStart', sender=self)

    def stop(self):
        if self._timer is not None and self._timer.active():
            self._timer.cancel()
        self._timer = None
        notification_center = NotificationCenter()
        if self.session is not None:
            notification_center.remove_observer(self, sender=self.session)
            reactor.callLater(5, self._end_session, self.session)
            self.session = None
        if self._failure is not None:
            notification_center.post_notification('S2XMucInvitationHandlerDidFail', sender=self, data=NotificationData(failure=self._failure))
        else:
            notification_center.post_notification('S2XMucInvitationHandlerDidEnd', sender=self)

    def _end_session(self, session):
        try:
            session.end(480)
        except Exception:
            pass

    def _timeout(self):
        NotificationCenter().remove_observer(self, sender=self.session)
        try:
            self.session.end(408)
        except Exception:
            pass
        self.session = None
        self._failure = MucInvitationFailure('Timeout', 408)
        self.stop()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidFail(self, notification):
        notification.center.remove_observer(self, sender=self.session)
        self.session = None
        self._failure = MucInvitationFailure(notification.data.reason or notification.data.failure_reason, notification.data.code)
        self.stop()


class X2SMucHandler(object):
    implements(IObserver)

    sip_identity = WriteOnceAttribute()
    xmpp_identity = WriteOnceAttribute()

    def __init__(self, sip_identity, xmpp_identity, nickname):
        self.sip_identity = sip_identity
        self.xmpp_identity = xmpp_identity
        self.nickname = nickname
        self._xmpp_muc_session = None
        self._sip_session = None
        self._msrp_stream = None
        self._first_stanza = None
        self._pending_nicknames_map = {}    # map message ID of MSRP NICKNAME chunk to corresponding stanza
        self._pending_messages_map = {}     # map message ID of MSRP SEND chunk to corresponding stanza
        self._participants = set()          # set of (URI, nickname) tuples
        self.ended = False

    def start(self):
        notification_center = NotificationCenter()
        self._xmpp_muc_session = XMPPIncomingMucSession(local_identity=self.sip_identity, remote_identity=self.xmpp_identity)
        notification_center.add_observer(self, sender=self._xmpp_muc_session)
        self._xmpp_muc_session.start()
        notification_center.post_notification('X2SMucHandlerDidStart', sender=self)
        self._start_sip_session()

    def end(self):
        if self.ended:
            return
        notification_center = NotificationCenter()
        if self._xmpp_muc_session is not None:
            notification_center.remove_observer(self, sender=self._xmpp_muc_session)
            # Send indication that the user has been kicked from the room
            sender = Identity(FrozenURI(self.sip_identity.uri.user, self.sip_identity.uri.host, self.nickname))
            stanza = MUCAvailabilityPresence(sender, self.xmpp_identity, available=False)
            stanza.jid = self.xmpp_identity
            stanza.muc_statuses.append('307')
            xmpp_manager = XMPPManager()
            xmpp_manager.send_muc_stanza(stanza)
            self._xmpp_muc_session.end()
            self._xmpp_muc_session = None
        if self._sip_session is not None:
            notification_center.remove_observer(self, sender=self._sip_session)
            self._sip_session.end()
            self._sip_session = None
        self.ended = True
        notification_center.post_notification('X2SMucHandlerDidEnd', sender=self)

    @run_in_green_thread
    def _start_sip_session(self):
        # self.xmpp_identity is our local identity
        from_uri = self.xmpp_identity.uri.as_sip_uri()
        del from_uri.parameters['gr']    # no GRUU in From header
        contact_uri = self.xmpp_identity.uri.as_sip_uri()
        contact_uri.parameters['gr'] = encode_resource(contact_uri.parameters['gr'].decode('utf-8'))
        to_uri = self.sip_identity.uri.as_sip_uri()
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        account = DefaultAccount()
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
            self.end()
            return
        self._msrp_stream = MediaStreamRegistry().get('chat')()
        route = routes.pop(0)
        from_header = FromHeader(from_uri)
        to_header = ToHeader(to_uri)
        contact_header = ContactHeader(contact_uri)
        self._sip_session = Session(account)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self._sip_session)
        notification_center.add_observer(self, sender=self._msrp_stream)
        self._sip_session.connect(from_header, to_header, contact_header=contact_header, route=route, streams=[self._msrp_stream])

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        log.msg("SIP multiparty session %s started" % self._sip_session.call_id)
        if not self._sip_session.remote_focus or not self._msrp_stream.nickname_allowed:
            self.end()
            return
        message_id = self._msrp_stream.set_local_nickname(self.nickname)
        self._pending_nicknames_map[message_id] = (self.nickname, self._first_stanza)
        self._first_stanza = None

    def _NH_SIPSessionDidEnd(self, notification):
        log.msg("SIP multiparty session %s ended" % self._sip_session.call_id)
        notification.center.remove_observer(self, sender=self._sip_session)
        notification.center.remove_observer(self, sender=self._msrp_stream)
        self._sip_session = None
        self._msrp_stream = None
        self.end()

    def _NH_SIPSessionDidFail(self, notification):
        log.msg("SIP multiparty session %s failed" % self._sip_session.call_id)
        notification.center.remove_observer(self, sender=self._sip_session)
        notification.center.remove_observer(self, sender=self._msrp_stream)
        self._sip_session = None
        self._msrp_stream = None
        self.end()

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            self._sip_session.reject_proposal()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        self._sip_session.reject_transfer(403)

    def _NH_SIPSessionGotConferenceInfo(self, notification):
        # Translate to XMPP payload
        xmpp_manager = XMPPManager()
        own_uri = FrozenURI(self.xmpp_identity.uri.user, self.xmpp_identity.uri.host)
        conference_info = notification.data.conference_info
        new_participants = set()
        for user in conference_info.users:
            user_uri = FrozenURI.parse(user.entity if user.entity.startswith(('sip:', 'sips:')) else 'sip:'+user.entity)
            nickname = user.display_text.value if user.display_text else user.entity
            new_participants.add((user_uri, nickname))
        # Remove participants that are no longer in the room
        for uri, nickname in self._participants - new_participants:
            sender = Identity(FrozenURI(self.sip_identity.uri.user, self.sip_identity.uri.host, nickname))
            stanza = MUCAvailabilityPresence(sender, self.xmpp_identity, available=False)
            xmpp_manager.send_muc_stanza(stanza)
        # Send presence for current participants
        for uri, nickname in new_participants:
            if uri == own_uri:
                continue
            sender = Identity(FrozenURI(self.sip_identity.uri.user, self.sip_identity.uri.host, nickname))
            stanza = MUCAvailabilityPresence(sender, self.xmpp_identity, available=True)
            stanza.jid = Identity(uri)
            xmpp_manager.send_muc_stanza(stanza)
        self._participants = new_participants
        # Send own status last
        sender = Identity(FrozenURI(self.sip_identity.uri.user, self.sip_identity.uri.host, self.nickname))
        stanza = MUCAvailabilityPresence(sender, self.xmpp_identity, available=True)
        stanza.jid = self.xmpp_identity
        stanza.muc_statuses.append('110')
        xmpp_manager.send_muc_stanza(stanza)

    def _NH_ChatStreamGotMessage(self, notification):
        # Notification is sent by the MSRP stream
        if not self._xmpp_muc_session:
            return
        message = notification.data.message
        content_type = message.content_type.lower()
        if content_type not in ('text/plain', 'text/html'):
            return
        if content_type == 'text/plain':
            html_body = None
            body = message.content
        else:
            html_body = message.content
            body = None
        resource = message.sender.display_name or str(message.sender.uri)
        sender = Identity(FrozenURI(self.sip_identity.uri.user, self.sip_identity.uri.host, resource))
        self._xmpp_muc_session.send_message(sender, body, html_body, message_id='MUC.'+uuid.uuid4().hex)
        self._msrp_stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')

    def _NH_ChatStreamDidSetNickname(self, notification):
        # Notification is sent by the MSRP stream
        nickname, stanza = self._pending_nicknames_map.pop(notification.data.message_id)
        self.nickname = nickname

    def _NH_ChatStreamDidNotSetNickname(self, notification):
        # Notification is sent by the MSRP stream
        nickname, stanza = self._pending_nicknames_map.pop(notification.data.message_id)
        error_stanza = MUCErrorPresence.from_stanza(stanza, 'cancel', [('conflict', STANZAS_NS)])
        xmpp_manager = XMPPManager()
        xmpp_manager.send_muc_stanza(error_stanza)

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        # Echo back the message to the sender
        stanza = self._pending_messages_map.pop(notification.data.message_id)
        stanza.sender, stanza.recipient = stanza.recipient, stanza.sender
        stanza.sender.uri = FrozenURI(stanza.sender.uri.user, stanza.sender.uri.host, self.nickname)
        xmpp_manager = XMPPManager()
        xmpp_manager.send_muc_stanza(stanza)

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        self._pending_messages_map.pop(notification.data.message_id)

    def _NH_XMPPIncomingMucSessionDidEnd(self, notification):
        notification.center.remove_observer(self, sender=self._xmpp_muc_session)
        self._xmpp_muc_session = None
        self.end()

    def _NH_XMPPIncomingMucSessionGotMessage(self, notification):
        if not self._sip_session:
            return
        message = notification.data.message
        sender_uri = message.sender.uri.as_sip_uri()
        del sender_uri.parameters['gr']    # no GRUU in CPIM From header
        sender = ChatIdentity(sender_uri, display_name=self.nickname)
        message_id = self._msrp_stream.send_message(message.body, 'text/plain', sender=sender)
        self._pending_messages_map[message_id] = message
        # Message will be echoed back to the sender on ChatStreamDidDeliverMessage

    def _NH_XMPPIncomingMucSessionChangedNickname(self, notification):
        if not self._sip_session:
            return
        nickname = notification.data.nickname
        try:
            message_id = self._msrp_stream.set_local_nickname(nickname)
        except ChatStreamError:
            return
        self._pending_nicknames_map[message_id] = (nickname, notification.data.stanza)

