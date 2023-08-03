
import json
import random
import os
import secrets
import uuid

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import unlink
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, FromHeader, ToHeader, Message, Request, RouteHeader, Route, Header
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.imdn import IMDNDocument
from sipsimple.payloads.rcsfthttp import FTHTTPDocument
from sipsimple.streams.msrp.chat import CPIMPayload, Message as SIPMessage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor, defer
from zope.interface import implementer

from sylk.configuration import SIPConfig
from sylk.web import server

from . import push
from .configuration import GeneralConfig
from .logger import log
from .models import sylkrtc
from .storage import MessageStorage
from .datatypes import FileTransferData


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

        if content_type == FTHTTPDocument.content_type:
            document = FTHTTPDocument.parse(body)
            for info in document:
                if info.type == 'file':
                    transfer_data = FileTransferData(info.file_name.value,
                                                     int(info.file_size.value),
                                                     info.content_type.value,
                                                     message_id,
                                                     sender.uri,
                                                     destination.uri,
                                                     url=info.data.url)
                    metadata = sylkrtc.TransferredFile(**transfer_data.__dict__)
                    body = json.dumps(sylkrtc.FileTransferMessage(**metadata.__data__).__data__)
                    content_type = 'application/sylk-file-transfer'

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
                                 direction='outgoing',
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
                                             direction='incoming',
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
                                               timestamp=str(self.parsed_message.timestamp),
                                               server_generated=True
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
                if not hasattr(self.session, 'transfer_data'):
                    return

                transfer_data = self.session.transfer_data
                metadata = sylkrtc.TransferredFile(**transfer_data.__dict__, hash=self.stream.file_selector.hash)
                meta_filepath = os.path.join(transfer_data.path, f'meta-{metadata.filename}')

                try:
                    with open(meta_filepath, 'w+') as output_file:
                        output_file.write(json.dumps(metadata.__data__))
                except (OSError, IOError):
                    unlink(meta_filepath)
                    log.warning('Could not save metadata %s' % meta_filepath)

                log.info('File transfer finished, saved to %s' % transfer_data.full_path)

                message_handler = MessageHandler()
                cpim_payload = transfer_data.cpim_message_payload(metadata)
                message_handler.outgoing_message_to_self(f'sip:{metadata.receiver.uri}', cpim_payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')

                xml_payload = transfer_data.cpim_rcsfthttp_message_payload(metadata)
                message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', xml_payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')
                message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', xml_payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')

                if not metadata.filename.endswith('.asc'):
                    payload = transfer_data.message_payload
                    message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
                    message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
        else:
            pass

        self.session = None
        self.stream = None
        self.handler = None

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
