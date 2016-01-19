
from application.notification import NotificationCenter, NotificationData
from eventlib import api
from msrplib.connect import DirectConnector, DirectAcceptor
from msrplib.protocol import URI
from msrplib.session import contains_mime_type
from msrplib.transport import make_response
from sipsimple.core import SDPAttribute
from sipsimple.payloads.iscomposing import IsComposingDocument, State, LastActive, Refresh, ContentType
from sipsimple.streams.msrp import MSRPStreamBase as _MSRPStreamBase, MSRPStreamError, NotificationProxyLogger
from sipsimple.streams.msrp.chat import ChatStream as _ChatStream, Message, QueuedMessage, CPIMPayload, CPIMParserError
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from sylk.configuration import SIPConfig, ServerConfig


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
    except Exception, e:
        notification_center.post_notification('MediaStreamDidNotInitialize', self, NotificationData(reason=str(e)))
    else:
        self._initialized = True
        notification_center.post_notification('MediaStreamDidInitialize', self)
    finally:
        self.greenlet = None

# Monkey-patch the initialize method
_MSRPStreamBase.initialize = MSRPStreamBase_initialize


class ChatStream(_ChatStream):
    priority = _ChatStream.priority + 1

    accept_types = ['message/cpim']
    accept_wrapped_types = ['*']
    chatroom_capabilities = ['nickname', 'private-messages', 'com.ag-projects.screen-sharing', 'com.ag-projects.zrtp-sas']

    @property
    def private_messages_allowed(self):
        # As a server, we always support sending private messages
        return True

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
        if chunk.size == 0:
            # keep-alive
            self.msrp_session.send_report(chunk, 200, 'OK')
            return
        if self.direction=='sendonly':
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
        message = Message(**payload.__dict__)
        if not contains_mime_type(self.accept_wrapped_types, message.content_type):
            self.msrp_session.send_report(chunk, 415, 'Invalid Content-Type')
            return
        if message.timestamp is None:
            message.timestamp = ISOTimestamp.now()
        if message.sender is None:
            message.sender = self.remote_identity
        private = self.session.remote_focus and len(message.recipients) == 1 and message.recipients[0] != self.remote_identity
        notification_center = NotificationCenter()
        if message.content_type.lower() == IsComposingDocument.content_type:
            data = IsComposingDocument.parse(message.content)
            ndata = NotificationData(state=data.state.value,
                                     refresh=data.refresh.value if data.refresh is not None else 120,
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
    def _send_nickname_response(self, response):
        try:
            self.msrp_session.send_chunk(response)
        except Exception:
            pass

    def accept_nickname(self, chunk):
        if chunk.method != 'NICKNAME':
            raise ValueError('Incorrect chunk method for accept_nickname: %s' % chunk.method)
        response = make_response(chunk, 200, 'OK')
        self._send_nickname_response(response)

    def reject_nickname(self, chunk, code, reason):
        if chunk.method != 'NICKNAME':
            raise ValueError('Incorrect chunk method for accept_nickname: %s' % chunk.method)
        response = make_response(chunk, code, reason)
        self._send_nickname_response(response)

    def send_message(self, content, content_type='text/plain', sender=None, recipients=None, timestamp=None, additional_headers=None, message_id=None, notify_progress=True):
        message = QueuedMessage(content, content_type, sender=sender, recipients=recipients, timestamp=timestamp, additional_headers=additional_headers, id=message_id, notify_progress=notify_progress)
        self._enqueue_message(message)
        return message.id

    def send_composing_indication(self, state, refresh=None, last_active=None, sender=None, recipients=None, message_id=None, notify_progress=False):
        content = IsComposingDocument.create(state=State(state), refresh=Refresh(refresh) if refresh is not None else None, last_active=LastActive(last_active) if last_active is not None else None, content_type=ContentType('text'))
        message = QueuedMessage(content, IsComposingDocument.content_type, sender=sender, recipients=recipients, id=message_id, notify_progress=notify_progress)
        self._enqueue_message(message)
        return message.id

