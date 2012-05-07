# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import random

from datetime import datetime
from dateutil.tz import tzlocal

from application.notification import NotificationCenter
from msrplib.protocol import URI
from msrplib.session import contains_mime_type
from sipsimple.account import AccountManager
from sipsimple.core import SDPAttribute
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.applications.chat import CPIMMessage, CPIMParserError
from sipsimple.streams.msrp import ChatStream as _ChatStream, ChatStreamError, MSRPStreamBase
from sipsimple.util import TimestampedNotificationData

from sylk.configuration import SIPConfig
from sylk.session import ServerSession


# We need to match on the only account that will be available
def _always_find_default_account(self, contact_uri):
    return self.default_account
AccountManager.find_account = _always_find_default_account


# Patch sipsimple.session to use ServerSession instead
import sipsimple.session
sipsimple.session.Session = ServerSession


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
    chatroom_capabilities = ['private-messages', 'com.ag-projects.screen-sharing']

    @property
    def local_uri(self):
        return URI(host=SIPConfig.local_ip, port=0, use_tls=self.transport=='tls', credentials=self.account.tls_credentials)

    def _create_local_media(self, uri_path):
        local_media = MSRPStreamBase._create_local_media(self, uri_path)
        if self.session.local_focus and self.chatroom_capabilities:
            local_media.attributes.append(SDPAttribute('chatroom', ' '.join(self.chatroom_capabilities)))
        return local_media

    def _handle_SEND(self, chunk):
        # This ChatStream doesn't send MSRP REPORT chunks automatically, the developer needs to manually send them
        if self.direction=='sendonly':
            self.msrp_session.send_report(chunk, 413, 'Unwanted Message')
            return
        if not chunk.data:
            self.msrp_session.send_report(chunk, 200, 'OK')
            return
        if chunk.segment is not None:
            self.incoming_queue.setdefault(chunk.message_id, []).append(chunk.data)
            if chunk.final:
                chunk.data = ''.join(self.incoming_queue.pop(chunk.message_id))
            else:
                self.msrp_session.send_report(chunk, 200, 'OK')
                return
        if chunk.content_type.lower() == 'message/cpim':
            try:
                message = CPIMMessage.parse(chunk.data)
            except CPIMParserError:
                self.msrp_session.send_report(chunk, 400, 'CPIM Parser Error')
                return
            else:
                if message.timestamp is None:
                    message.timestamp = datetime.now(tzlocal())
                if message.sender is None:
                    message.sender = self.remote_identity
                private = self.session.remote_focus and len(message.recipients) == 1 and message.recipients[0] != self.remote_identity
        else:
            self.msrp_session.send_report(chunk, 415, 'Invalid Content-Type')
            return
        # TODO: check wrapped content-type and issue a report if it's invalid
        notification_center = NotificationCenter()
        if message.content_type.lower() == IsComposingDocument.content_type:
            data = IsComposingDocument.parse(message.body)
            ndata = TimestampedNotificationData(state=data.state.value,
                                                refresh=data.refresh.value if data.refresh is not None else None,
                                                content_type=data.content_type.value if data.content_type is not None else None,
                                                last_active=data.last_active.value if data.last_active is not None else None,
                                                sender=message.sender, recipients=message.recipients, private=private, chunk=chunk)
            notification_center.post_notification('ChatStreamGotComposingIndication', self, ndata)
        else:
            notification_center.post_notification('ChatStreamGotMessage', self, TimestampedNotificationData(message=message, private=private, chunk=chunk))

    def send_message(self, content, content_type='text/plain', local_identity=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, message_id=None, notify_progress=True, success_report='yes', failure_report='yes'):
        if self.direction=='recvonly':
            raise ChatStreamError('Cannot send message on recvonly stream')
        if message_id is None:
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
        self._enqueue_message(str(message_id), str(msg), 'message/cpim', failure_report=failure_report, success_report=success_report, notify_progress=notify_progress)
        return message_id

    def send_composing_indication(self, state, refresh, last_active=None, recipients=None, local_identity=None, message_id=None, notify_progress=False, success_report='no', failure_report='partial'):
        if self.direction == 'recvonly':
            raise ChatStreamError('Cannot send message on recvonly stream')
        if state not in ('active', 'idle'):
            raise ValueError('Invalid value for composing indication state')
        if message_id is None:
            message_id = '%x' % random.getrandbits(64)
        content = IsComposingMessage(state=State(state), refresh=Refresh(refresh), last_active=LastActive(last_active or datetime.now()), content_type=ContentType('text')).toxml()
        if recipients is None:
            recipients = [self.remote_identity]
        # Only use CPIM, it's the only type we accept
        msg = CPIMMessage(content, IsComposingDocument.content_type, sender=local_identity or self.local_identity, recipients=recipients, timestamp=datetime.now())
        self._enqueue_message(str(message_id), str(msg), 'message/cpim', failure_report='partial', success_report='no')
        return message_id


