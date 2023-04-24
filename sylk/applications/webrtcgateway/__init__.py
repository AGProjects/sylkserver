
import json
import os
import time
import uuid
import errno

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.system import unlink
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError
from sipsimple.threading import run_in_twisted_thread, run_in_thread
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import defer, reactor
from zope.interface import implementer

from sylk.applications import SylkApplication
from sylk.session import IllegalStateError

from .configuration import GeneralConfig
from .datatypes import FileTransferData
from .handler import MessageHandler
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

        file_selector = transfer_stream.file_selector
        transfer_data = FileTransferData(file_selector.name,
                                         file_selector.size,
                                         file_selector.type,
                                         str(uuid.uuid4()),
                                         sender,
                                         receiver)
        session.transfer_data = transfer_data

        message_storage = MessageStorage()
        account = defer.maybeDeferred(message_storage.get_account, receiver)
        account.addCallback(lambda result: self._check_receiver_session(result))

        sender_account = defer.maybeDeferred(message_storage.get_account, sender)
        sender_account.addCallback(lambda result: self._check_sender_session(result, session))

        d1 = defer.DeferredList([account, sender_account], consumeErrors=True)
        d1.addCallback(lambda result: self._handle_lookup_result(result, session, transfer_stream))

        NotificationCenter().add_observer(self, sender=session)

    def _check_receiver_session(self, account):
        if account is None:
            raise Exception("Receiver account for filetransfer not found")

    def _check_sender_session(self, account, session):
        if account is None:
            session.transfer_data.update_path_for_receiver()
            raise Exception("Sender account for filetransfer not found")

    def _handle_lookup_result(self, result, session, stream):
        reject_session = all([success is not True for (success, value) in result])
        if reject_session:
            self._reject_session(session, "Sender and receiver accounts for filetransfer were not found")
            return

        stream.handler.save_directory = session.transfer_data.path
        log.info('File transfer from {sender.uri} to {receiver.uri} will be saved to {path}/{filename}'.format(**session.transfer_data.__dict__))
        self._accept_session(session, [stream])

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

                payload = 'File transfer available at %s (%s)' % (transfer_data.url, transfer_data.formatted_file_size)
                message_handler = MessageHandler()
                message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
                message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
                message_handler.outgoing_message_to_self(f'sip:{metadata.receiver.uri}', payload, identity=f'sip:{metadata.sender.uri}')
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
