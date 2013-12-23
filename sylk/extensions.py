# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import random

from application.notification import NotificationCenter, NotificationData
from eventlib import api
from msrplib.connect import DirectConnector, DirectAcceptor
from msrplib.protocol import URI
from msrplib.session import contains_mime_type
from msrplib.transport import make_response
from sipsimple.core import SDPAttribute
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.streams.applications.chat import CPIMMessage, CPIMParserError
from sipsimple.streams.msrp import ChatStream as _ChatStream, FileTransferStream as _FileTransferStream
from sipsimple.streams.msrp import ChatStreamError, MSRPStreamError, NotificationProxyLogger
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.python.failure import Failure

from sylk.configuration import SIPConfig, ServerConfig


class MSRPStreamMixin(object):

    @run_in_green_thread
    def initialize(self, session, direction):
        self.greenlet = api.getcurrent()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)
        try:
            self.session = session
            self.transport = self.session.account.msrp.transport
            outgoing = direction=='outgoing'
            logger = NotificationProxyLogger()
            if self.session.account.msrp.connection_model == 'relay':
                if not outgoing and self.remote_role in ('actpass', 'passive'):
                    # 'passive' not allowed by the RFC but play nice for interoperability. -Saul
                    self.msrp_connector = DirectConnector(logger=logger, use_sessmatch=True)
                    self.local_role = 'active'
                elif not outgoing:
                    if self.transport=='tls' and None in (self.session.account.tls_credentials.cert, self.session.account.tls_credentials.key):
                        raise MSRPStreamError("Cannot accept MSRP connection without a TLS certificate")
                    self.msrp_connector = DirectAcceptor(logger=logger)
                    self.local_role = 'passive'
                else:
                    # outgoing
                    self.msrp_connector = DirectConnector(logger=logger, use_sessmatch=True)
                    self.local_role = 'active'
            else:
                if not outgoing and self.remote_role in ('actpass', 'passive'):
                    # 'passive' not allowed by the RFC but play nice for interoperability. -Saul
                    self.msrp_connector = DirectConnector(logger=logger, use_sessmatch=True)
                    self.local_role = 'active'
                else:
                    if not outgoing and self.transport=='tls' and None in (self.session.account.tls_credentials.cert, self.session.account.tls_credentials.key):
                        raise MSRPStreamError("Cannot accept MSRP connection without a TLS certificate")
                    self.msrp_connector = DirectAcceptor(logger=logger, use_sessmatch=True)
                    self.local_role = 'actpass' if outgoing else 'passive'
            full_local_path = self.msrp_connector.prepare(self.local_uri)
            self.local_media = self._create_local_media(full_local_path)
        except api.GreenletExit:
            raise
        except Exception, ex:
            ndata = NotificationData(context='initialize', failure=Failure(), reason=str(ex))
            notification_center.post_notification('MediaStreamDidFail', self, ndata)
        else:
            notification_center.post_notification('MediaStreamDidInitialize', self)
        finally:
            if self.msrp_session is None and self.msrp is None and self.msrp_connector is None:
                notification_center.remove_observer(self, sender=self)
            self.greenlet = None


class ChatStream(MSRPStreamMixin, _ChatStream):
    priority = 2

    accept_types = ['message/cpim']
    accept_wrapped_types = ['*']
    chatroom_capabilities = ['nickname', 'private-messages', 'com.ag-projects.screen-sharing']

    @property
    def local_uri(self):
        return URI(host=SIPConfig.local_ip.normalized, port=0, use_tls=self.transport=='tls', credentials=self.session.account.tls_credentials)

    def _create_local_media(self, uri_path):
        local_media = super(ChatStream, self)._create_local_media(uri_path)
        if self.session.local_focus and self.chatroom_capabilities:
            caps = self.chatroom_capabilities[:]
            if ServerConfig.enable_bonjour:
                caps.remove('private-messages')
            local_media.attributes.append(SDPAttribute('chatroom', ' '.join(caps)))
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
        if chunk.content_type.lower() != 'message/cpim':
            self.msrp_session.send_report(chunk, 415, 'Invalid Content-Type')
            return
        try:
            message = CPIMMessage.parse(chunk.data)
        except CPIMParserError:
            self.msrp_session.send_report(chunk, 400, 'CPIM Parser Error')
            return
        else:
            if message.timestamp is None:
                message.timestamp = ISOTimestamp.now()
            if message.sender is None:
                message.sender = self.remote_identity
            private = self.session.remote_focus and len(message.recipients) == 1 and message.recipients[0] != self.remote_identity
        # TODO: check wrapped content-type and issue a report if it's invalid
        notification_center = NotificationCenter()
        if message.content_type.lower() == IsComposingDocument.content_type:
            data = IsComposingDocument.parse(message.body)
            ndata = NotificationData(state=data.state.value,
                                                refresh=data.refresh.value if data.refresh is not None else None,
                                                content_type=data.content_type.value if data.content_type is not None else None,
                                                last_active=data.last_active.value if data.last_active is not None else None,
                                                sender=message.sender, recipients=message.recipients, private=private, chunk=chunk)
            notification_center.post_notification('ChatStreamGotComposingIndication', self, ndata)
        else:
            notification_center.post_notification('ChatStreamGotMessage', self, NotificationData(message=message, private=private, chunk=chunk))

    def _handle_NICKNAME(self, chunk):
        nickname = chunk.headers['Use-Nickname'].decoded
        NotificationCenter().post_notification('ChatStreamGotNicknameRequest', self, NotificationData(nickname=nickname, chunk=chunk))

    @run_in_green_thread
    def _send_response(self, response):
        try:
            self.msrp_session.send_chunk(response)
        except Exception:
            pass

    def accept_nickname(self, chunk):
        if chunk.method != 'NICKNAME':
            raise ValueError('Incorrect chunk method for accept_nickname: %s' % chunk.method)
        response = make_response(chunk, 200, 'OK')
        self._send_response(response)

    def reject_nickname(self, chunk, code, reason):
        if chunk.method != 'NICKNAME':
            raise ValueError('Incorrect chunk method for accept_nickname: %s' % chunk.method)
        response = make_response(chunk, code, reason)
        self._send_response(response)

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
            timestamp = ISOTimestamp.now()
        # Only use CPIM, it's the only type we accept
        msg = CPIMMessage(content, content_type, sender=local_identity or self.local_identity, recipients=recipients, courtesy_recipients=courtesy_recipients,
                            subject=subject, timestamp=timestamp, required=required, additional_headers=additional_headers)
        self._enqueue_message(str(message_id), str(msg), 'message/cpim', failure_report=failure_report, success_report=success_report, notify_progress=notify_progress)
        return message_id

    def send_composing_indication(self, state, refresh=None, last_active=None, recipients=None, local_identity=None, message_id=None, notify_progress=False, success_report='no', failure_report='partial'):
        if self.direction == 'recvonly':
            raise ChatStreamError('Cannot send message on recvonly stream')
        if state not in ('active', 'idle'):
            raise ValueError('Invalid value for composing indication state')
        if message_id is None:
            message_id = '%x' % random.getrandbits(64)
        content = IsComposingMessage.create(state=State(state), refresh=Refresh(refresh) is refresh is not None else None, last_active=LastActive(last_active) if last_active is not None else None, content_type=ContentType('text'))
        if recipients is None:
            recipients = [self.remote_identity]
        # Only use CPIM, it's the only type we accept
        msg = CPIMMessage(content, IsComposingDocument.content_type, sender=local_identity or self.local_identity, recipients=recipients, timestamp=ISOTimestamp.now())
        self._enqueue_message(str(message_id), str(msg), 'message/cpim', failure_report='partial', success_report='no')
        return message_id


class FileTransferStream(MSRPStreamMixin, _FileTransferStream):
    priority = 11

    def initialize(self, session, direction):
        if self.direction == 'sendonly' and self.file_selector.fd is None:
            NotificationCenter().post_notification('MediaStreamDidFail', sender=self, data=NotificationData(context='initialize', failure=None, reason='file descriptor not specified'))
            return
        super(FileTransferStream, self).initialize(session, direction)

