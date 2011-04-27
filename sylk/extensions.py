# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import random

from datetime import datetime

from application.notification import NotificationCenter
from eventlet import api
from msrplib.connect import get_acceptor, get_connector
from msrplib.protocol import URI
from msrplib.session import contains_mime_type
from sipsimple.account import AccountManager
from sipsimple.core import SDPAttribute
from sipsimple.payloads.iscomposing import IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.applications.chat import CPIMMessage
from sipsimple.streams.msrp import ChatStream as _ChatStream, ChatStreamError, MSRPStreamBase, MSRPStreamError, NotificationProxyLogger
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData
from twisted.python.failure import Failure

from sylk.configuration import SIPConfig, MSRPConfig


# We need to match on the only account that will be available
def _always_find_default_account(self, contact_uri):
    return self.default_account
AccountManager.find_account = _always_find_default_account


# We need to be able to set the local identity in the message CPIM envelope
# so that messages appear to be coming from the users themselves, instead of
# just seeying the server identity
registry = MediaStreamRegistry()
for stream_type in registry.stream_types[:]:
    if stream_type is _ChatStream:
        registry.stream_types.remove(stream_type)
        break
del registry

class ChatStream(_ChatStream):
    accept_types = ['message/cpim']
    accept_wrapped_types = ['*']
    chatroom_capabilities = ['private-messages']

    def _create_local_media(self, uri_path):
        local_media = MSRPStreamBase._create_local_media(self, uri_path)
        if self.session.local_focus and self.chatroom_capabilities:
            local_media.attributes.append(SDPAttribute('chatroom', ' '.join(self.chatroom_capabilities)))
        return local_media

    def send_message(self, content, content_type='text/plain', local_identity=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None):
        if self.direction=='recvonly':
            raise ChatStreamError('Cannot send message on recvonly stream')
        message_id = '%x' % random.getrandbits(64)
        if not contains_mime_type(self.accept_wrapped_types, content_type):
            raise ChatStreamError('Invalid content_type for outgoing message: %r' % content_type)
        if not recipients:
            recipients = [self.remote_identity]
        if timestamp is None:
            timestamp = datetime.now()
        # Only use CPIM, it's the only type we accept
        msg = CPIMMessage(content, content_type, sender=local_identity or self.local_identity, recipients=recipients, courtesy_recipients=courtesy_recipients,
                            subject=subject, timestamp=timestamp, required=required, additional_headers=additional_headers)
        self._enqueue_message(message_id, str(msg), 'message/cpim', failure_report='yes', success_report='yes', notify_progress=True)
        return message_id

    def send_composing_indication(self, state, refresh, last_active=None, recipients=None, local_identity=None):
        if self.direction == 'recvonly':
            raise ChatStreamError('Cannot send message on recvonly stream')
        if state not in ('active', 'idle'):
            raise ValueError('Invalid value for composing indication state')
        message_id = '%x' % random.getrandbits(64)
        content = IsComposingMessage(state=State(state), refresh=Refresh(refresh), last_active=LastActive(last_active or datetime.now()), content_type=ContentType('text')).toxml()
        if recipients is None:
            recipients = [self.remote_identity]
        # Only use CPIM, it's the only type we accept
        msg = CPIMMessage(content, IsComposingMessage.content_type, sender=local_identity or self.local_identity, recipients=recipients, timestamp=datetime.now())
        self._enqueue_message(message_id, str(msg), 'message/cpim', failure_report='partial', success_report='no')
        return message_id


# Patch MediaStreamBase initialize method
#   - There is no need for relay support
#   - We need to choose the outbound IP address
@run_in_green_thread
def _initialize(self, session, direction):
    self.greenlet = api.getcurrent()
    notification_center = NotificationCenter()
    notification_center.add_observer(self, sender=self)
    try:
        self.session = session
        outgoing = direction=='outgoing'
        self.transport = self.account.msrp.transport
        if not outgoing and self.transport == 'tls' and None in (self.account.tls_credentials.cert, self.account.tls_credentials.key):
            raise MSRPStreamError("Cannot accept MSRP connection without a TLS certificate")
        logger = NotificationProxyLogger()
        local_uri = URI(host=SIPConfig.local_ip,
                        port=0,
                        use_tls=self.transport=='tls',
                        credentials=self.account.tls_credentials)
        if outgoing:
            if MSRPConfig.use_acm:
                # We start the transport as passive, because we expect the other end to become active. -Saul
                self.msrp_connector = get_acceptor(relay=None, use_acm=True, logger=logger)
                self.local_role = 'actpass'
            else:
                self.msrp_connector = get_connector(relay=None, use_acm=True, logger=logger)
                self.local_role = 'active'
        else:
            if self.remote_role in ('actpass', 'active'):
                self.msrp_connector = get_acceptor(relay=None, use_acm=True, logger=logger)
                self.local_role = 'passive'
            else:
                # Remote role is passive. Not allowed by the draft but play nice for interoperability. -Saul
                self.msrp_connector = get_connector(relay=None, use_acm=True, logger=logger)
                self.local_role = 'active'
        full_local_path = self.msrp_connector.prepare(local_uri)
        self.local_media = self._create_local_media(full_local_path)
    except api.GreenletExit:
        raise
    except Exception, ex:
        ndata = TimestampedNotificationData(context='initialize', failure=Failure(), reason=str(ex))
        notification_center.post_notification('MediaStreamDidFail', self, ndata)
    else:
        notification_center.post_notification('MediaStreamDidInitialize', self, data=TimestampedNotificationData())
    finally:
        if self.msrp_session is None and self.msrp is None and self.msrp_connector is None:
            notification_center.remove_observer(self, sender=self)
        self.greenlet = None

MSRPStreamBase.initialize = _initialize

