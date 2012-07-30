# Copyright (C) 2012 AG Projects. See LICENSE for details
#

import os
import uuid

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.descriptor import WriteOnceAttribute
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.core import ContactHeader, FromHeader, ToHeader
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.streams.msrp import ChatStreamError
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData
from zope.interface import implements

from sylk.applications import ApplicationLogger
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, encode_resource
from sylk.applications.xmppgateway.xmpp import XMPPManager
from sylk.applications.xmppgateway.xmpp.session import XMPPIncomingMucSession
from sylk.applications.xmppgateway.xmpp.stanzas import MUCAvailabilityPresence, MUCErrorPresence, STANZAS_NS
from sylk.extensions import ChatStream
from sylk.session import ServerSession

log = ApplicationLogger(os.path.dirname(__file__).split(os.path.sep)[-1])


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
        notification_center.post_notification('X2SMucHandlerDidStart', sender=self, data=TimestampedNotificationData())
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
        notification_center.post_notification('X2SMucHandlerDidEnd', sender=self, data=TimestampedNotificationData())

    @run_in_green_thread
    def _start_sip_session(self):
        notification_center = NotificationCenter()
        # self.xmpp_identity is our local identity
        from_uri = self.xmpp_identity.uri.as_sip_uri()
        del from_uri.parameters['gr']    # no GRUU in From header
        contact_uri = self.xmpp_identity.uri.as_sip_uri()
        contact_uri.parameters['gr'] = encode_resource(contact_uri.parameters['gr'].decode('utf-8'))
        to_uri = self.sip_identity.uri.as_sip_uri()
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        account = AccountManager().default_account
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
            notification_center.post_notification('ChatSessionDidFail', sender=self, data=TimestampedNotificationData())
            return
        self._msrp_stream = ChatStream(account)
        route = routes.pop(0)
        from_header = FromHeader(from_uri)
        to_header = ToHeader(to_uri)
        contact_header = ContactHeader(contact_uri)
        self._sip_session = ServerSession(account)
        notification_center.add_observer(self, sender=self._sip_session)
        notification_center.add_observer(self, sender=self._msrp_stream)
        self._sip_session.connect(from_header, to_header, contact_header=contact_header, routes=[route], streams=[self._msrp_stream])

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        log.msg("SIP session (MUC) %s started" % notification.sender._invitation.call_id)
        if not self._sip_session.remote_focus or not self._msrp_stream.nickname_allowed:
            self.end()
            return
        message_id = self._msrp_stream.set_local_nickname(self.nickname)
        self._pending_nicknames_map[message_id] = (self.nickname, self._first_stanza)
        self._first_stanza = None

    def _NH_SIPSessionDidEnd(self, notification):
        log.msg("SIP session (MUC) %s ended" % notification.sender._invitation.call_id)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._sip_session)
        notification_center.remove_observer(self, sender=self._msrp_stream)
        self._sip_session = None
        self._msrp_stream = None
        self.end()

    def _NH_SIPSessionDidFail(self, notification):
        log.msg("SIP session (MUC) %s failed" % notification.sender._invitation.call_id)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._sip_session)
        notification_center.remove_observer(self, sender=self._msrp_stream)
        self._sip_session = None
        self._msrp_stream = None
        self.end()

    def _NH_SIPSessionGotProposal(self, notification):
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
            body = message.body
        else:
            html_body = message.body
            body = None
        resource = message.sender.display_name or str(message.sender.uri)
        sender = Identity(FrozenURI(self.sip_identity.uri.user, self.sip_identity.uri.host, resource))
        self._xmpp_muc_session.send_message(sender, body, html_body, message_id=str(uuid.uuid4()))
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
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self._xmpp_muc_session)
        self._xmpp_muc_session = None
        self.end()

    def _NH_XMPPIncomingMucSessionGotMessage(self, notification):
        if not self._sip_session:
            return
        message = notification.data.message
        sender_uri = message.sender.uri.as_sip_uri()
        del sender_uri.parameters['gr']    # no GRUU in CPIM From header
        sender = CPIMIdentity(sender_uri, display_name=self.nickname)
        message_id = self._msrp_stream.send_message(message.body, 'text/plain', local_identity=sender)
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

