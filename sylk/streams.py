import random
from collections import defaultdict
from functools import partial

from application.notification import NotificationCenter, NotificationData
from eventlib import api
from eventlib.coros import queue
from eventlib.proc import spawn, ProcExit
from msrplib.connect import DirectConnector, DirectAcceptor
from msrplib.protocol import URI, FailureReportHeader, SuccessReportHeader, UseNicknameHeader
from msrplib.session import contains_mime_type, MSRPSession
from msrplib.transport import make_response
from sipsimple.core import SDPAttribute
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingDocument, State, LastActive, Refresh, ContentType
from sipsimple.streams import InvalidStreamError, UnknownStreamError
from sipsimple.streams.msrp import MSRPStreamBase as _MSRPStreamBase, MSRPStreamError, NotificationProxyLogger
from sipsimple.streams.msrp.chat import ChatStream as _ChatStream, ChatStreamError, ChatIdentity, Message, QueuedMessage, CPIMPayload, CPIMParserError
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from sylk.configuration import SIPConfig


@run_in_green_thread
def MSRPStreamBase_initialize(self, session, direction):
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
        full_local_path = self.msrp_connector.prepare(local_uri=URI(host=SIPConfig.local_ip.normalized, port=0, use_tls=self.transport=='tls', credentials=self.session.account.tls_credentials))
        self.local_media = self._create_local_media(full_local_path)
    except Exception as e:
        notification_center.post_notification('MediaStreamDidNotInitialize', self, NotificationData(reason=str(e)))
    else:
        self._initialized = True
        notification_center.post_notification('MediaStreamDidInitialize', self)
    finally:
        self.greenlet = None

# Monkey-patch the initialize method (needed because we want every MSRP based stream to behave this way, including file transfers)
#
_MSRPStreamBase.initialize = MSRPStreamBase_initialize


class ChatStream(_MSRPStreamBase):
    type = 'chat'
    priority = _ChatStream.priority + 1
    msrp_session_class = MSRPSession
    media_type = 'message'
    accept_types = ['message/cpim']
    accept_wrapped_types = ['*']
    supported_chatroom_capabilities = ['nickname', 'private-messages', 'com.ag-projects.screen-sharing', 'com.ag-projects.zrtp-sas']

    def __init__(self):
        super(ChatStream, self).__init__(direction='sendrecv')
        self.message_queue = queue()
        self.sent_messages = set()
        self.incoming_queue = defaultdict(list)
        self.message_queue_thread = None

    @classmethod
    def new_from_sdp(cls, session, remote_sdp, stream_index):
        remote_stream = remote_sdp.media[stream_index]
        if remote_stream.media != 'message':
            raise UnknownStreamError
        expected_transport = 'TCP/TLS/MSRP' if session.account.msrp.transport=='tls' else 'TCP/MSRP'
        if remote_stream.transport != expected_transport:
            raise InvalidStreamError("expected %s transport in chat stream, got %s" % (expected_transport, remote_stream.transport))
        if remote_stream.formats != ['*']:
            raise InvalidStreamError("wrong format list specified")
        stream = cls()
        stream.remote_role = remote_stream.attributes.getfirst('setup', 'active')
        if remote_stream.direction != 'sendrecv':
            raise InvalidStreamError("Unsupported direction for chat stream: %s" % remote_stream.direction)
        remote_accept_types = remote_stream.attributes.getfirst('accept-types')
        if remote_accept_types is None:
            raise InvalidStreamError("remote SDP media does not have 'accept-types' attribute")
        if not any(contains_mime_type(cls.accept_types, mime_type) for mime_type in remote_accept_types.split()):
            raise InvalidStreamError("no compatible media types found")
        return stream

    @property
    def local_identity(self):
        try:
            return ChatIdentity(self.session.local_identity.uri, self.session.account.display_name)
        except AttributeError:
            return None

    @property
    def remote_identity(self):
        try:
            return ChatIdentity(self.session.remote_identity.uri, self.session.remote_identity.display_name)
        except AttributeError:
            return None

    @property
    def private_messages_allowed(self):
        return 'private-messages' in self.chatroom_capabilities

    @property
    def nickname_allowed(self):
        return 'nickname' in self.chatroom_capabilities

    @property
    def chatroom_capabilities(self):
        try:
            if self.session.local_focus:
                return ' '.join(self.local_media.attributes.getall('chatroom')).split()
            elif self.session.remote_focus:
                return ' '.join(self.remote_media.attributes.getall('chatroom')).split()
        except AttributeError:
            pass
        return []

    def _NH_MediaStreamDidStart(self, notification):
        self.message_queue_thread = spawn(self._message_queue_handler)

    def _NH_MediaStreamDidNotInitialize(self, notification):
        message_queue, self.message_queue = self.message_queue, queue()
        while message_queue:
            message = message_queue.wait()
            if message.notify_progress:
                data = NotificationData(message_id=message.id, message=None, code=0, reason='Stream was closed')
                notification.center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)

    def _NH_MediaStreamDidEnd(self, notification):
        if self.message_queue_thread is not None:
            self.message_queue_thread.kill()
        else:
            message_queue, self.message_queue = self.message_queue, queue()
            while message_queue:
                message = message_queue.wait()
                if message.notify_progress:
                    data = NotificationData(message_id=message.id, message=None, code=0, reason='Stream ended')
                    notification.center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)

    def _create_local_media(self, uri_path):
        local_media = super(ChatStream, self)._create_local_media(uri_path)
        if self.session.local_focus and self.supported_chatroom_capabilities:
            local_media.attributes.append(SDPAttribute('chatroom', ' '.join(self.supported_chatroom_capabilities)))
        return local_media

    def _handle_REPORT(self, chunk):
        # in theory, REPORT can come with Byte-Range which would limit the scope of the REPORT to the part of the message.
        if chunk.message_id in self.sent_messages:
            self.sent_messages.remove(chunk.message_id)
            notification_center = NotificationCenter()
            data = NotificationData(message_id=chunk.message_id, message=chunk, code=chunk.status.code, reason=chunk.status.comment)
            if chunk.status.code == 200:
                notification_center.post_notification('ChatStreamDidDeliverMessage', sender=self, data=data)
            else:
                notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)

    def _handle_SEND(self, chunk):
        # This ChatStream doesn't send MSRP REPORT chunks automatically, the developer needs to manually send them
        if chunk.size == 0:
            # keep-alive
            self.msrp_session.send_report(chunk, 200, 'OK')
            return
        if self.direction == 'sendonly':
            self.msrp_session.send_report(chunk, 413, 'Unwanted Message')
            return
        if chunk.content_type.lower() != 'message/cpim':
            self.incoming_queue.pop(chunk.message_id, None)
            self.msrp_session.send_report(chunk, 415, 'Invalid Content-Type')
            return
        if chunk.contflag == '#':
            self.incoming_queue.pop(chunk.message_id, None)
            self.msrp_session.send_report(chunk, 200, 'OK')
            return
        elif chunk.contflag == '+':
            self.incoming_queue[chunk.message_id].append(chunk.data)
            self.msrp_session.send_report(chunk, 200, 'OK')
            return
        else:
            data = ''.join(self.incoming_queue.pop(chunk.message_id, [])) + chunk.data
        try:
            payload = CPIMPayload.decode(data)
        except CPIMParserError:
            self.msrp_session.send_report(chunk, 400, 'CPIM Parser Error')
            return
        message = Message(**{name: getattr(payload, name) for name in Message.__slots__})
        if not contains_mime_type(self.accept_wrapped_types, message.content_type):
            self.msrp_session.send_report(chunk, 415, 'Invalid Content-Type')
            return
        if message.timestamp is None:
            message.timestamp = ISOTimestamp.now()
        if message.sender is None:
            message.sender = self.remote_identity
        if payload.charset is not None:
            message.content = message.content.decode(payload.charset)
        private = self.session.remote_focus and len(message.recipients) == 1 and message.recipients[0] != self.remote_identity
        notification_center = NotificationCenter()
        if message.content_type.lower() == IsComposingDocument.content_type:
            try:
                data = IsComposingDocument.parse(message.content)
            except ParserError as e:
                self.msrp_session.send_report(chunk, 400, str(e))
                return
            ndata = NotificationData(state=data.state.value,
                                     refresh=data.refresh.value if data.refresh is not None else 120,
                                     content_type=data.content_type.value if data.content_type is not None else None,
                                     last_active=data.last_active.value if data.last_active is not None else None,
                                     sender=message.sender,
                                     recipients=message.recipients,
                                     private=private,
                                     chunk=chunk)
            notification_center.post_notification('ChatStreamGotComposingIndication', self, ndata)
        else:
            ndata = NotificationData(message=message,
                                     private=private,
                                     chunk=chunk)
            notification_center.post_notification('ChatStreamGotMessage', self, ndata)

    def _handle_NICKNAME(self, chunk):
        nickname = chunk.headers['Use-Nickname'].decoded
        NotificationCenter().post_notification('ChatStreamGotNicknameRequest', self, NotificationData(nickname=nickname, chunk=chunk))

    def _on_transaction_response(self, message_id, response):
        if message_id in self.sent_messages and response.code != 200:
            self.sent_messages.remove(message_id)
            data = NotificationData(message_id=message_id, message=response, code=response.code, reason=response.comment)
            NotificationCenter().post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)

    def _on_nickname_transaction_response(self, message_id, response):
        notification_center = NotificationCenter()
        if response.code == 200:
            notification_center.post_notification('ChatStreamDidSetNickname', sender=self, data=NotificationData(message_id=message_id, response=response))
        else:
            notification_center.post_notification('ChatStreamDidNotSetNickname', sender=self, data=NotificationData(message_id=message_id, message=response, code=response.code, reason=response.comment))

    def _message_queue_handler(self):
        notification_center = NotificationCenter()
        try:
            while True:
                message = self.message_queue.wait()
                if self.msrp_session is None:
                    if message.notify_progress:
                        data = NotificationData(message_id=message.id, message=None, code=0, reason='Stream ended')
                        notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)
                    break
                try:
                    if isinstance(message.content, unicode):
                        message.content = message.content.encode('utf8')
                        charset = 'utf8'
                    else:
                        charset = None
                    message.sender = message.sender or self.local_identity
                    message.recipients = message.recipients or [self.remote_identity]
                    message.timestamp = message.timestamp or ISOTimestamp.now()
                    payload = CPIMPayload(charset=charset, **{name: getattr(message, name) for name in Message.__slots__})
                except ChatStreamError as e:
                    if message.notify_progress:
                        data = NotificationData(message_id=message.id, message=None, code=0, reason=e.args[0])
                        notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)
                    continue
                else:
                    content, content_type = payload.encode()

                message_id = message.id
                notify_progress = message.notify_progress
                report = 'yes' if notify_progress else 'no'

                chunk = self.msrp_session.make_message(content, content_type=content_type, message_id=message_id)
                chunk.add_header(FailureReportHeader(report))
                chunk.add_header(SuccessReportHeader(report))

                try:
                    self.msrp_session.send_chunk(chunk, response_cb=partial(self._on_transaction_response, message_id))
                except Exception as e:
                    if notify_progress:
                        data = NotificationData(message_id=message_id, message=None, code=0, reason=str(e))
                        notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)
                except ProcExit:
                    if notify_progress:
                        data = NotificationData(message_id=message_id, message=None, code=0, reason='Stream ended')
                        notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)
                    raise
                else:
                    if notify_progress:
                        self.sent_messages.add(message_id)
                        notification_center.post_notification('ChatStreamDidSendMessage', sender=self, data=NotificationData(message=chunk))
        finally:
            self.message_queue_thread = None
            while self.sent_messages:
                message_id = self.sent_messages.pop()
                data = NotificationData(message_id=message_id, message=None, code=0, reason='Stream ended')
                notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)
            message_queue, self.message_queue = self.message_queue, queue()
            while message_queue:
                message = message_queue.wait()
                if message.notify_progress:
                    data = NotificationData(message_id=message.id, message=None, code=0, reason='Stream ended')
                    notification_center.post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)

    @run_in_twisted_thread
    def _enqueue_message(self, message):
        if self._done:
            if message.notify_progress:
                data = NotificationData(message_id=message.id, message=None, code=0, reason='Stream ended')
                NotificationCenter().post_notification('ChatStreamDidNotDeliverMessage', sender=self, data=data)
        else:
            self.message_queue.send(message)

    @run_in_green_thread
    def _send_nickname_response(self, chunk, code, reason):
        response = make_response(chunk, code, reason)
        try:
            self.msrp_session.send_chunk(response)
        except Exception:
            pass

    def accept_nickname(self, chunk):
        if chunk.method != 'NICKNAME':
            raise ValueError('Incorrect chunk method for accept_nickname: %s' % chunk.method)
        self._send_nickname_response(chunk, 200, 'OK')

    def reject_nickname(self, chunk, code, reason):
        if chunk.method != 'NICKNAME':
            raise ValueError('Incorrect chunk method for accept_nickname: %s' % chunk.method)
        self._send_nickname_response(chunk, code, reason)

    def send_message(self, content, content_type='text/plain', sender=None, recipients=None, timestamp=None, additional_headers=None, message_id=None, notify_progress=True):
        message = QueuedMessage(content, content_type, sender=sender, recipients=recipients, timestamp=timestamp, additional_headers=additional_headers, id=message_id, notify_progress=notify_progress)
        self._enqueue_message(message)
        return message.id

    def send_composing_indication(self, state, refresh=None, last_active=None, sender=None, recipients=None, message_id=None, notify_progress=False):
        content = IsComposingDocument.create(state=State(state), refresh=Refresh(refresh) if refresh is not None else None, last_active=LastActive(last_active) if last_active is not None else None, content_type=ContentType('text'))
        message = QueuedMessage(content, IsComposingDocument.content_type, sender=sender, recipients=recipients, id=message_id, notify_progress=notify_progress)
        self._enqueue_message(message)
        return message.id

    @run_in_green_thread
    def _set_local_nickname(self, nickname, message_id):
        if self.msrp_session is None:
            # should we generate ChatStreamDidNotSetNickname here?
            return
        chunk = self.msrp.make_request('NICKNAME')
        chunk.add_header(UseNicknameHeader(nickname or u''))
        try:
            self.msrp_session.send_chunk(chunk, response_cb=partial(self._on_nickname_transaction_response, message_id))
        except Exception as e:
            self._failure_reason = str(e)
            NotificationCenter().post_notification('MediaStreamDidFail', sender=self, data=NotificationData(context='sending', reason=self._failure_reason))

    def set_local_nickname(self, nickname):
        if not self.nickname_allowed:
            raise ChatStreamError('Setting nickname is not supported')
        message_id = '%x' % random.getrandbits(64)
        self._set_local_nickname(nickname, message_id)
        return message_id
