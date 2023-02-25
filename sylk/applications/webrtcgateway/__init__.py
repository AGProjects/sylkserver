
import base64
import json
import hashlib
import random
import os
import secrets
import time
import uuid
import urllib.parse
import errno

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import unlink
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, FromHeader, ToHeader, Message, RouteHeader, Header, Route, Request
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.imdn import IMDNDocument
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, Message as SIPMessage
from sipsimple.threading import run_in_twisted_thread, run_in_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import defer, reactor
from zope.interface import implementer

from sylk.applications import SylkApplication
from sylk.configuration import SIPConfig
from sylk.session import IllegalStateError
from sylk.web import server

from .configuration import GeneralConfig
from .logger import log
from .models import sylkrtc
from .storage import TokenStorage, MessageStorage
from .web import WebHandler, AdminWebHandler
from . import push


@implementer(IObserver)
class WebRTCGatewayApplication(SylkApplication):
    def __init__(self):
        self.web_handler = WebHandler()
        self.admin_web_handler = AdminWebHandler()

    def start(self):
        self.web_handler.start()
        self.admin_web_handler.start()
        # Load tokens from the storage
        token_storage = TokenStorage()
        token_storage.load()
        # Setup message storage
        message_storage = MessageStorage()
        message_storage.load()
        self.clean_filetransfers()

    def stop(self):
        self.web_handler.stop()
        self.admin_web_handler.stop()

    @run_in_thread('file-io')
    def clean_filetransfers(self):
        settings = SIPSimpleSettings()
        top = settings.file_transfer.directory.normalized
        removed_dirs = removed_files = 0
        for root, dirs, files in os.walk(top, topdown=False):
            for name in files:
                file = os.path.join(root, name)
                statinfo = os.stat(file)
                current_time = time.time()
                remove_after_days = GeneralConfig.filetransfer_expire_days
                if (statinfo.st_size >= 1024 * 1024 * 50 and statinfo.st_mtime < current_time - 86400 * remove_after_days):
                    log.info(f"[housekeeper] Removing expired filetransfer file: {file}")
                    removed_files += 1
                    unlink(file)
                elif statinfo.st_mtime < current_time - 86400 * 2 * remove_after_days:
                    log.info(f"[housekeeper] Removing expired file transfer file: {file}")
                    removed_files += 1
                    unlink(file)

            for name in dirs:
                dir = os.path.join(root, name)
                try:
                    os.rmdir(dir)
                except OSError as ex:
                    if ex.errno == errno.ENOTEMPTY:
                        pass
                else:
                    removed_dirs += 1
                    log.info(f"[housekeeper] Removing expired file transfer dir {dir}")

        log.info(f"[housekeeper] Removed {removed_files} files, {removed_dirs} directories")
        reactor.callLater(3600, self.clean_filetransfers)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def incoming_session(self, session):
        # TODO: handle diverted sessions?
        log.info('New incoming session {session.call_id} from sip:{uri.user}@{uri.host}'.format(session=session, uri=session.remote_identity.uri))
        transfer_streams = [stream for stream in session.proposed_streams if stream.type == 'file-transfer']

        if not transfer_streams:
            log.info(u'Session rejected: invalid media')
            session.reject(488)
            return

        transfer_stream = transfer_streams[0]

        if transfer_stream.direction == 'sendonly':
            # file transfer 'pull'
            log.info('Session rejected: requested file not found')
            session.reject(404)
            return

        sender = f'{session.remote_identity.uri.user}@{session.remote_identity.uri.host}'
        receiver = f'{session.local_identity.uri.user}@{session.local_identity.uri.host}'

        message_storage = MessageStorage()
        account = defer.maybeDeferred(message_storage.get_account, receiver)
        account.addCallback(lambda result: self._check_receiver_session(result, session, transfer_stream))

        sender_account = defer.maybeDeferred(message_storage.get_account, sender)
        sender_account.addCallback(lambda result: self._check_sender_session(result, session, transfer_stream))

        d1 = defer.DeferredList([account, sender_account], consumeErrors=True)
        d1.addCallback(lambda result: self._handle_lookup_result(result, session, [transfer_stream]))

        NotificationCenter().add_observer(self, sender=session)

    def _encode_and_hash_uri(self, uri):
        return base64.b64encode(hashlib.md5(uri.encode('utf-8')).digest()).rstrip(b'=\n').decode('utf-8')

    def _set_save_directory(self, session, transfer_stream, sender=None, receiver=None):
        transfer_id = str(uuid.uuid4())
        settings = SIPSimpleSettings()
        if receiver is None:
            receiver = f'{session.local_identity.uri.user}@{session.local_identity.uri.host}'
            hashed_receiver = self._encode_and_hash_uri(receiver)
            hashed_sender = self._encode_and_hash_uri(sender)
            prefix = hashed_receiver[:1]
            folder = os.path.join(settings.file_transfer.directory.normalized, prefix, hashed_receiver, hashed_sender, transfer_id)
        else:
            sender = f'{session.remote_identity.uri.user}@{session.remote_identity.uri.host}'
            hashed_receiver = self._encode_and_hash_uri(receiver)
            hashed_sender = self._encode_and_hash_uri(sender)
            prefix = hashed_sender[:1]
            folder = os.path.join(settings.file_transfer.directory.normalized, prefix, hashed_sender, hashed_receiver, transfer_id)

        metadata = sylkrtc.TransferredFile(transfer_id=transfer_id,
                                           sender=sylkrtc.SIPIdentity(uri=sender, display_name=session.remote_identity.display_name),
                                           receiver=sylkrtc.SIPIdentity(uri=receiver, display_name=session.local_identity.display_name),
                                           filename=transfer_stream.file_selector.name,
                                           prefix=prefix,
                                           path=folder,
                                           filesize=transfer_stream.file_selector.size,
                                           timestamp=str(ISOTimestamp.now()))

        transfer_stream.handler.save_directory = folder
        session.metadata = metadata
        log.info('File transfer from %s to %s will be saved to %s/%s' % (metadata.sender.uri, metadata.receiver.uri, metadata.path, metadata.filename))

    def _check_receiver_session(self, account, session, transfer_stream):
        if account is None:
            raise Exception("Receiver account for filetransfer not found")

        self._set_save_directory(session, transfer_stream, receiver=account.account)

    def _check_sender_session(self, account, session, transfer_stream):
        if account is not None:
            return

        self._set_save_directory(session, transfer_stream, sender=account.account)

    def _handle_lookup_result(self, result, session, streams):
        for (success, value) in result:
            if not success:
                self._reject_session(session, value.getErrorMessage())
                return
        self._accept_session(session, streams)

    def _reject_session(self, session, error):
        log.warning(f'File transfer rejected: {error}')
        session.reject(404)

    def _accept_session(self, session, streams):
        try:
            session.accept(streams)
        except IllegalStateError:
            session.reject(500)

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, message_request, data):
        content_type = data.headers.get('Content-Type', Null).content_type
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)

        if Null in (content_type, from_header, to_header):
            message_request.answer(400)
            return

        if content_type == 'message/cpim':
            try:
                cpim_message = CPIMPayload.decode(data.body)
            except (CPIMParserError, UnicodeDecodeError):  # TODO: fix decoding in sipsimple
                log.warning('SIP message from %s to %s rejected: CPIM parse error' % (from_header.uri, '%s@%s' % (to_header.uri.user, to_header.uri.host)))
                message_request.answer(400)
                return
            else:
                content_type = cpim_message.content_type

        log.info('received SIP message (%s) from %s to %s' % (content_type, from_header.uri, '%s@%s' % (to_header.uri.user, to_header.uri.host)))

        message_request.answer(200)

        message_handler = MessageHandler()
        message_handler.incoming_message(data)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        try:
            transfer_stream = next(stream for stream in session.streams if stream.type == 'file-transfer')
        except StopIteration:
            pass
        else:
            transfer_handler = FileTransferHandler()
            transfer_handler.init_incoming(transfer_stream)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        notification.center.remove_observer(self, sender=session)

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        notification.center.remove_observer(self, sender=session)
        log.info('File transfer from %s to %s failed: %s (%s)' % (session.remote_identity.uri, session.local_identity.uri, notification.data.reason, notification.data.failure_reason))


class ParsedSIPMessage(SIPMessage):
    __slots__ = 'message_id', 'disposition', 'destination'

    def __init__(self, content, content_type, sender=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, message_id=None, disposition=None, destination=None):
        super(ParsedSIPMessage, self).__init__(content, content_type, sender, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        self.message_id = message_id
        self.disposition = disposition
        self.destination = destination


@implementer(IObserver)
class ReplicatedMessage(Message):
    def __init__(self, from_header, to_header, route_header, content_type, body, credentials=None, extra_headers=None):
        super(ReplicatedMessage, self).__init__(from_header, to_header, route_header, content_type, body, credentials=None, extra_headers=None)
        self._request = Request("MESSAGE", from_header.uri, from_header, to_header, route_header, credentials=credentials, extra_headers=extra_headers, content_type=content_type, body=body if isinstance(body, bytes) else body.encode())


@implementer(IObserver)
class MessageHandler(object):

    def __init__(self):
        self.message_storage = MessageStorage()
        self.resolver = DNSLookup()
        self.from_header = None
        self.to_header = None
        self.content_type = None
        self.from_sip = None
        self.body = None
        self.parsed_message = None

    def _lookup_sip_target_route(self, uri):
        proxy = GeneralConfig.outbound_sip_proxy
        if proxy is not None:
            sip_uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
        else:
            sip_uri = SIPURI.parse('sip:%s' % uri)

        settings = SIPSimpleSettings()
        try:
            routes = self.resolver.lookup_sip_proxy(sip_uri, settings.sip.transport_list).wait()
        except DNSLookupError as e:
            raise DNSLookupError('DNS lookup error: {exception!s}'.format(exception=e))
        if not routes:
            raise DNSLookupError('DNS lookup error: no results found')

        route = random.choice([r for r in routes if r.transport == routes[0].transport])
        log.debug('DNS lookup for SIP message proxy for {} yielded {}'.format(uri, route))
        return route

    def _parse_message(self):
        cpim_message = None
        if self.content_type == "message/cpim":
            cpim_message = CPIMPayload.decode(self.body)
            body = cpim_message.content if isinstance(cpim_message.content, str) else cpim_message.content.decode()
            content_type = cpim_message.content_type
            sender = cpim_message.sender or self.from_header
            disposition = next(([item.strip() for item in header.value.split(',')] for header in cpim_message.additional_headers if header.name == 'Disposition-Notification'), None)
            message_id = next((header.value for header in cpim_message.additional_headers if header.name == 'Message-ID'), str(uuid.uuid4()))
        else:
            body = self.body.decode('utf-8')
            sender = self.from_header
            disposition = None
            message_id = str(uuid.uuid4())
            content_type = str(self.content_type)

        timestamp = str(cpim_message.timestamp) if cpim_message is not None and cpim_message.timestamp is not None else str(ISOTimestamp.now())
        sender = sylkrtc.SIPIdentity(uri=str(sender.uri), display_name=sender.display_name)
        destination = sylkrtc.SIPIdentity(uri=str(self.to_header.uri), display_name=self.to_header.display_name)

        self.parsed_message = ParsedSIPMessage(body, content_type, sender=sender, disposition=disposition, message_id=message_id, timestamp=timestamp, destination=destination)

    def _send_public_key(self, from_header, to_header, public_key):
        if public_key:
            self.outgoing_message(from_header, public_key, 'text/pgp-public-key', str(to_header))

    def _handle_generate_token(self):
        account = f'{self.from_header.uri.user}@{self.from_header.uri.host}'
        log.info(f'Adding {account} for storing messages')
        self.message_storage.add_account(account)
        token = secrets.token_urlsafe()
        self.outgoing_message(self.from_header.uri, json.dumps({'token': token, 'url': f'{server.url}/webrtcgateway/messages/history/{account}'}), 'application/sylk-api-token')
        self.message_storage.add_account_token(account=account, token=token)

    def _handle_lookup_pgp_key(self):
        account = f'{self.to_header.uri.user}@{self.to_header.uri.host}'
        public_key = self.message_storage.get_public_key(account)
        log.info(f'Public key lookup for {account}')
        if isinstance(public_key, defer.Deferred):
            public_key.addCallback(lambda result: self._send_public_key(self.from_header.uri, self.to_header.uri, result))
        else:
            self._send_public_key(self.from_header.uri, self.to_header.uri, public_key)

    def _handle_message_remove(self):
        account = f'{self.from_header.uri.user}@{self.from_header.uri.host}'
        contact = f'{self.to_header.uri.user}@{self.to_header.uri.host}'
        message_id = self.parsed_message.content

        self.message_storage.removeMessage(account=account, message_id=message_id)

        content = sylkrtc.AccountMessageRemoveEventData(contact=contact, message_id=message_id)
        self.message_storage.add(account=account,
                                 contact=contact,
                                 direction='',
                                 content=json.dumps(content.__data__),
                                 content_type='application/sylk-message-remove',
                                 timestamp=str(ISOTimestamp.now()),
                                 disposition_notification='',
                                 message_id=str(uuid.uuid4()))

        event = sylkrtc.AccountSyncEvent(account=account, type='message', action='remove', content=content)
        self.outgoing_message(self.from_header.uri, json.dumps(content.__data__), 'application/sylk-message-remove', str(self.to_header.uri))
        notification_center = NotificationCenter()
        notification_center.post_notification(name='SIPApplicationGotAccountRemoveMessage', sender=account, data=event)

        def remove_message_from_receiver(msg_id, messages):
            for message in messages:
                if message.message_id == msg_id and message.direction == 'incoming':
                    account = message.account
                    message_id = message.message_id
                    self.message_storage.removeMessage(account=account, message_id=message_id)

                    content = sylkrtc.AccountMessageRemoveEventData(contact=message.contact, message_id=message_id)
                    self.message_storage.add(account=account,
                                             contact=message.contact,
                                             direction='',
                                             content=json.dumps(content.__data__),
                                             content_type='application/sylk-message-remove',
                                             timestamp=str(ISOTimestamp.now()),
                                             disposition_notification='',
                                             message_id=str(uuid.uuid4()))

                    event = sylkrtc.AccountSyncEvent(account=account, type='message', action='remove', content=content)
                    notification_center.post_notification(name='SIPApplicationGotAccountRemoveMessage', sender=account, data=event)
                    log.info("Removed receiver message")
                    break

        messages = self.message_storage[[contact, '']]
        if isinstance(messages, defer.Deferred):
            messages.addCallback(lambda result: remove_message_from_receiver(msg_id=message_id, messages=result))
        else:
            remove_message_from_receiver(msg_id=message_id, messages=messages)

    def _store_message_for_sender(self, account):
        if account is None:
            log.info('not storing %s message from non-existent account %s to %s' % (self.parsed_message.content_type, self.from_header.uri, '%s@%s' % (self.to_header.uri.user, self.to_header.uri.host)))
            return

        log.debug(f"storage is enabled for originator {account.account}")

        message = None
        ignored_content_types = ("application/im-iscomposing+xml", 'text/pgp-public-key', IMDNDocument.content_type)
        if self.parsed_message.content_type in ignored_content_types:
            return

        log.info('storing {content_type} message for account {originator} to {destination.uri}'.format(content_type=self.parsed_message.content_type, originator=account.account, destination=self.parsed_message.destination))

        self.message_storage.add(account=account.account,
                                 contact=f'{self.to_header.uri.user}@{self.to_header.uri.host}',
                                 direction="outgoing",
                                 content=self.parsed_message.content,
                                 content_type=self.parsed_message.content_type,
                                 timestamp=str(self.parsed_message.timestamp),
                                 disposition_notification=self.parsed_message.disposition,
                                 message_id=self.parsed_message.message_id,
                                 state='accepted')

        message = sylkrtc.AccountSyncEvent(account=account.account,
                                           type='message',
                                           action='add',
                                           content=sylkrtc.AccountMessageRequest(
                                               transaction='1',
                                               account=account.account,
                                               uri=f'{self.to_header.uri.user}@{self.to_header.uri.host}',
                                               message_id=self.parsed_message.message_id,
                                               content=self.parsed_message.content,
                                               content_type=self.parsed_message.content_type,
                                               timestamp=str(self.parsed_message.timestamp)
                                           ))

        notification_center = NotificationCenter()
        notification_center.post_notification(name='SIPApplicationGotOutgoingAccountMessage', sender=account.account, data=message)

    def _store_message_for_receiver(self, account):
        if account is None:
            log.info('not storing %s message from %s to non-existent account %s' % (self.parsed_message.content_type, self.from_header.uri, '%s@%s' % (self.to_header.uri.user, self.to_header.uri.host)))
            return

        log.debug(f'processing message from {self.from_header.uri} for account {account.account}')

        message = None
        notification_center = NotificationCenter()

        if self.parsed_message.content_type == "application/im-iscomposing+xml":
            return

        if self.parsed_message.content_type == IMDNDocument.content_type:
            document = IMDNDocument.parse(self.parsed_message.content)
            imdn_message_id = document.message_id.value
            imdn_status = document.notification.status.__str__()
            imdn_datetime = document.datetime.__str__()
            log.info('storing IMDN message ({status}) from {originator.uri}'.format(status=imdn_status, originator=self.parsed_message.sender))
            self.message_storage.update(account=account.account,
                                        state=imdn_status,
                                        message_id=imdn_message_id)

            self.message_storage.update(account=str(self.parsed_message.sender.uri),
                                        state=imdn_status,
                                        message_id=imdn_message_id)

            message = sylkrtc.AccountDispositionNotificationEvent(account=account.account,
                                                                  state=imdn_status,
                                                                  message_id=imdn_message_id,
                                                                  message_timestamp=imdn_datetime,
                                                                  timestamp=str(self.parsed_message.timestamp),
                                                                  code=200,
                                                                  reason='')
            imdn_message_event = message.__data__
            # del imdn_message_event['account']
            ## Maybe prevent multiple imdn rows?
            self.message_storage.add(account=account.account,
                                     contact=self.parsed_message.sender.uri,
                                     direction="incoming",
                                     content=json.dumps(imdn_message_event),
                                     content_type='message/imdn',
                                     timestamp=str(self.parsed_message.timestamp),
                                     disposition_notification='',
                                     message_id=self.parsed_message.message_id,
                                     state='received')

            notification_center.post_notification(name='SIPApplicationGotAccountDispositionNotification',
                                                  sender=account.account,
                                                  data=NotificationData(message=message, sender=self.parsed_message.sender))
        else:
            log.info('storing {content_type} message from {originator.uri} for account {account}'.format(content_type=self.parsed_message.content_type, originator=self.parsed_message.sender, account=account.account))
            self.message_storage.add(account=account.account,
                                     contact=str(self.parsed_message.sender.uri),
                                     direction='incoming',
                                     content=self.parsed_message.content,
                                     content_type=self.parsed_message.content_type,
                                     timestamp=str(self.parsed_message.timestamp),
                                     disposition_notification=self.parsed_message.disposition,
                                     message_id=self.parsed_message.message_id,
                                     state='received')

            message = sylkrtc.AccountMessageEvent(account=account.account,
                                                  sender=self.parsed_message.sender,
                                                  content=self.parsed_message.content,
                                                  content_type=self.parsed_message.content_type,
                                                  timestamp=str(self.parsed_message.timestamp),
                                                  disposition_notification=self.parsed_message.disposition,
                                                  message_id=self.parsed_message.message_id)

            notification_center.post_notification(name='SIPApplicationGotAccountMessage', sender=account.account, data=message)

            if self.parsed_message.content_type == 'text/plain' or self.parsed_message.content_type == 'text/html':
                def get_unread_messages(messages, originator):
                    unread = 1
                    for message in messages:
                        if ((message.content_type == 'text/plain' or message.content_type == 'text/html')
                                and message.direction == 'incoming' and message.contact != account.account
                                and 'display' in message.disposition):
                            unread += 1
                            # log.info(f'{message.disposition} {message.contact} {message.state}')
                    # log.info(f'there are {unread} messages')
                    push.message(originator=originator, destination=account.account, call_id=str(uuid.uuid4()), badge=unread)

                messages = self.message_storage[[account.account, '']]
                if isinstance(messages, defer.Deferred):
                    messages.addCallback(lambda result: get_unread_messages(messages=result, originator=message.sender))
                else:
                    get_unread_messages(messages=messages, originator=message.sender)

    def incoming_message(self, data, content_type=None):
        self.content_type = content_type if content_type is not None else data.headers.get('Content-Type', Null).content_type
        self.from_header = data.headers.get('From', Null)
        self.to_header = data.headers.get('To', Null)
        self.body = data.body
        self.from_sip = data.headers.get('X-Sylk-From-Sip', Null)

        self._parse_message()

        if self.parsed_message.content_type == 'application/sylk-api-token':
            self._handle_generate_token()
            return

        if self.parsed_message.content_type == 'application/sylk-api-pgp-key-lookup':
            self._handle_lookup_pgp_key()
            return

        if self.parsed_message.content_type == 'application/sylk-api-message-remove':
            self._handle_message_remove()
            return

        if self.from_sip is not Null:
            log.debug("message is originating from SIP endpoint")
            sender_account = self.message_storage.get_account(f'{self.from_header.uri.user}@{self.from_header.uri.host}')
            if isinstance(sender_account, defer.Deferred):
                sender_account.addCallback(lambda result: self._store_message_for_sender(result))
            else:
                self._store_message_for_sender(sender_account)

        account = self.message_storage.get_account(f'{self.to_header.uri.user}@{self.to_header.uri.host}')
        if isinstance(account, defer.Deferred):
            account.addCallback(lambda result: self._store_message_for_receiver(result))
        else:
            self._store_message_for_receiver(account)

    @run_in_green_thread
    def _outgoing_message(self, to_uri, from_uri, content, content_type='text/plain', headers=[], route=None, message_type=Message, subscribe=True):
        if not route:
            return

        from_uri = SIPURI.parse('%s' % from_uri)
        to_uri = SIPURI.parse('%s' % to_uri)
        content = content if isinstance(content, bytes) else content.encode()

        message_request = message_type(FromHeader(from_uri),
                                       ToHeader(to_uri),
                                       RouteHeader(route.uri),
                                       content_type,
                                       content,
                                       extra_headers=headers)

        if subscribe:
            notification_center = NotificationCenter()
            notification_center.add_observer(self, sender=message_request)

        message_request.send()

    @run_in_green_thread
    def outgoing_message(self, uri, content, content_type='text/plain', identity=None, extra_headers=[]):
        route = self._lookup_sip_target_route(uri)
        if route:
            if identity is None:
                identity = f'sip:sylkserver@{SIPConfig.local_ip}'

            log.info("sending message from '%s' to '%s' using proxy %s" % (identity, uri, route))
            headers = [Header('X-Sylk-To-Sip', 'yes')] + extra_headers
            self._outgoing_message(uri, identity, content, content_type, headers=headers, route=route)

    @run_in_green_thread
    def outgoing_message_to_self(self, uri, content, content_type='text/plain', identity=None, extra_headers=[]):
        route = Route(address=SIPConfig.local_ip, port=SIPConfig.local_tcp_port, transport='tcp')
        if route:
            if identity is None:
                identity = f'sip:sylkserver@{SIPConfig.local_ip}'

            log.debug("sending message from '%s' to '%s' to self %s" % (identity, uri, route))
            headers = [Header('X-Sylk-From-Sip', 'yes')] + extra_headers
            self._outgoing_message(uri, identity, content, content_type, headers=headers, route=route, subscribe=False)

    @run_in_green_thread
    def outgoing_replicated_message(self, uri, content, content_type='text/plain', identity=None, extra_headers=[]):
        route = self._lookup_sip_target_route(identity)
        if route:
            if identity is None:
                identity = f'sip:sylkserver@{SIPConfig.local_ip}'

            log.info("sending message from '%s' to '%s' using proxy %s" % (identity, uri, route))
            headers = [Header('X-Sylk-To-Sip', 'yes'), Header('X-Replicated-Message', 'yes')] + extra_headers
            self._outgoing_message(uri, identity, content, content_type, headers=headers, route=route, message_type=ReplicatedMessage)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPMessageDidSucceed(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)

        log.info('outgoing message was accepted by remote party')

    def _NH_SIPMessageDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        data = notification.data
        reason = data.reason.decode() if isinstance(data.reason, bytes) else data.reason
        log.warning('could not deliver outgoing message %d %s' % (data.code, reason))


@implementer(IObserver)
class FileTransferHandler(object):
    def __init__(self):
        self.session = None
        self.stream = None
        self.handler = None
        self.direction = None

    def init_incoming(self, stream):
        self.direction = 'incoming'
        self.stream = stream
        self.session = stream.session
        self.handler = stream.handler
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.stream)
        notification_center.add_observer(self, sender=self.handler)

    @run_in_green_thread
    def init_outgoing(self, destination, file):
        self.direction = 'outgoing'

    def _terminate(self, failure_reason=None):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.handler)

        if failure_reason is None:
            if self.direction == 'incoming' and self.stream.direction == 'recvonly':
                if not hasattr(self.session, 'metadata'):
                    return

                metadata = self.session.metadata
                filepath = os.path.join(metadata.path, metadata.filename)
                meta_filepath = os.path.join(metadata.path, f'meta-{metadata.filename}')

                try:
                    with open(meta_filepath, 'w+') as output_file:
                        output_file.write(json.dumps(metadata.__data__))
                except (OSError, IOError):
                    unlink(meta_filepath)
                    log.warning('Could not save metadata %s' % meta_filepath)

                log.info('File transfer finished, saved to %s' % filepath)
                settings = SIPSimpleSettings()
                stripped_path = os.path.relpath(metadata.path, f'{settings.file_transfer.directory.normalized}/{metadata.prefix}')
                file_path = urllib.parse.quote(f'webrtcgateway/filetransfer/{stripped_path}/{metadata.filename}')
                file_url = f'{server.url}/{file_path}'

                payload = 'File transfer available at %s (%s)' % (file_url, self.format_file_size(metadata.filesize))
                message_handler = MessageHandler()
                message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
                message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
                message_handler.outgoing_message_to_self(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
        else:
            pass

        self.session = None
        self.stream = None
        self.handler = None

    @staticmethod
    def format_file_size(size):
        infinite = float('infinity')
        boundaries = [(             1024, '%d bytes',               1),
                      (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                      (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                      (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
        for boundary, format, divisor in boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_MediaStreamDidNotInitialize(self, notification):
        self._terminate(failure_reason=notification.data.reason)

    def _NH_FileTransferHandlerDidEnd(self, notification):
        if self.direction == 'incoming':
            if self.stream.direction == 'sendonly':
                reactor.callLater(3, self.session.end)
            else:
                reactor.callLater(1, self.session.end)
        else:
            self.session.end()
        self._terminate(failure_reason=notification.data.reason)
