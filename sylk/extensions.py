# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import random

from datetime import datetime

from msrplib.session import contains_mime_type
from sipsimple.account import AccountManager
from sipsimple.core import SDPAttribute
from sipsimple.payloads.iscomposing import IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.applications.chat import CPIMMessage
from sipsimple.streams.msrp import ChatStream as _ChatStream, ChatStreamError, MSRPStreamBase


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
    focus_capabilities = [('chatroom', 'private-messages')]

    def _create_local_media(self, uri_path):
        local_media = MSRPStreamBase._create_local_media(self, uri_path)
        if self.session.local_focus:
            local_media.attributes.extend(SDPAttribute(item[0], item[1]) for item in self.focus_capabilities)
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


