
import os
import time
import errno
import re

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import unlink
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError
from sipsimple.threading import run_in_thread
from twisted.internet import defer, reactor
from zope.interface import implementer

from sylk.applications import SylkApplication
from sylk.payloads import conference_info_extensions as _conference_payloads  # noqa: F401 -- registers conference-info+xml extensions on import so this app can parse them even if the conference application is not loaded. Lives under sylk.payloads (not sylk.applications.conference) so importing it does NOT pull in ConferenceApplication, whose metaclass would otherwise auto-register and auto-start it even when 'conference' is in [Server] disabled_applications.
from sylk.session import IllegalStateError

from .audio_level_udp import AudioLevelUDPClient
from .configuration import GeneralConfig
from .datatypes import FileTransferData
from .sip_backends.sipsimple import SIPSimpleBackend, contact_token
from .sip_handlers import FileTransferHandler, MessageHandler
from .logger import log, QUIET_CONTENT_TYPES
from .storage import TokenStorage, MessageStorage
from .web import WebHandler, AdminWebHandler


# A file transfer INVITE is accepted only once, should the same INVITE
# (same Call-ID and From tag) reach this application more than once. The
# copies forked to the contacts of the sipsimple backend are refused before
# (see incoming_session).
FILE_TRANSFER_FORK_WINDOW = 300  # seconds a file transfer INVITE is remembered


_CALL_STATES = ('accepted', 'established', 'local-updating', 'remote-updating')


def _in_audio_call(account, peer):
    """True if a device of account (user@host) has an audio call with peer (user@host), whatever its SIP backend"""
    from .factory import SylkWebSocketServerFactory
    from .handler import _aor_text
    peer = peer.lower()
    for protocol in list(SylkWebSocketServerFactory.connections):
        handler = getattr(protocol, 'connection_handler', None)
        if handler is None or account not in handler.accounts_map:
            continue
        for session_info in handler.sip_sessions:
            if (session_info.state in _CALL_STATES and 'audio' in session_info.media and session_info.account is not None
                    and session_info.account.id == account and session_info.remote_identity is not None
                    and _aor_text(session_info.remote_identity.uri).lower() == peer):
                return True
    return False


def _aor(uri):
    user, host = uri.user, uri.host
    return '{}@{}'.format(user.decode() if isinstance(user, bytes) else user, host.decode() if isinstance(host, bytes) else host)


def _invite_key(session):
    # forks of an INVITE share the Call-ID and the From tag
    invitation = session._invitation
    from_header = getattr(invitation, 'from_header', None)
    if from_header is None:
        from_header = (getattr(session, 'request_headers', None) or {}).get('From', None)
    tag = getattr(from_header, 'tag', None)
    if isinstance(tag, bytes):
        tag = tag.decode()
    return invitation.call_id, tag


@implementer(IObserver)
class WebRTCGatewayApplication(SylkApplication):
    def __init__(self):
        self.web_handler = WebHandler()
        self.admin_web_handler = AdminWebHandler()
        self.audio_level_client = AudioLevelUDPClient()
        self._file_transfer_invites = set()  # (Call-ID, From tag) of the file transfer INVITEs handled, see _invite_key

    def start(self):
        log.info(f'Using application directory {GeneralConfig.application_dir}')
        backend_map = GeneralConfig.sip_backend_accounts
        log.info('SIP backend: {} (default){}{}'.format(
            GeneralConfig.sip_backend,
            ', per account: {}'.format(', '.join('%s:%s' % item for item in sorted(list(backend_map.accounts.items()) + list(backend_map.domains.items())))) if backend_map else '',
            ', clients may override' if GeneralConfig.sip_backend_override else ''))
        self.web_handler.start()
        self.admin_web_handler.start()
        # Real-time audio-level UDP client. Subscribes to the conference
        # focus's UDP server when a chat session's NOTIFY reveals its
        # audio_levels_udp_endpoint; dispatches incoming levels to
        # local WebSocket sessions in the matching videoroom.
        self.audio_level_client.start()
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
        self.audio_level_client.stop()

    @run_in_thread('file-io')
    def clean_filetransfers(self):
        top = GeneralConfig.file_transfer_dir.normalized
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
        if contact_token(session.request_uri) is not None:
            # sent to the contact of a WebRTC client account using the sipsimple SIP backend
            if self._is_file_transfer(session):
                # The proxy forks a file transfer to the registered contacts and to
                # SylkServer itself, which receives it on behalf of all the devices
                # (also the offline ones): the copies sent to the contacts are refused.
                log.info('Refusing file transfer {session.call_id} sent to a WebRTC contact, SylkServer receives it for the account'.format(session=session))
                session.reject(486)
                return
            SIPSimpleBackend.incoming_session(session)
            return
        # An MSRP file transfer sent to an account: the file is stored and
        # announced to the receiver exactly like one uploaded over the web API.
        # A file from a sender that is not a local account is only accepted
        # during an audio call between them (see _handle_lookup_result), and
        # only once if the same INVITE arrives again (see _invite_key).
        # TODO: handle diverted sessions?
        log.info('New incoming session {session.call_id} from sip:{uri.user}@{uri.host}'.format(session=session, uri=session.remote_identity.uri))
        transfer_streams = [stream for stream in session.proposed_streams if stream.type == 'file-transfer']
        if not transfer_streams:
            session.reject(488)
            return

        key = _invite_key(session)
        if key in self._file_transfer_invites:
            log.info('Rejecting file transfer {session.call_id} from sip:{uri.user}@{uri.host}, a fork of an INVITE already handled'.format(session=session, uri=session.remote_identity.uri))
            session.reject(486)
            return
        self._file_transfer_invites.add(key)
        reactor.callLater(FILE_TRANSFER_FORK_WINDOW, self._file_transfer_invites.discard, key)

        transfer_stream = transfer_streams[0]

        sender = f'{session.remote_identity.uri.user}@{session.remote_identity.uri.host}'
        receiver = f'{session.local_identity.uri.user}@{session.local_identity.uri.host}'

        file_selector = transfer_stream.file_selector
        if transfer_stream.direction == 'sendonly':
            transfer_data = FileTransferData(file_selector.name,
                                             file_selector.size,
                                             file_selector.type,
                                             transfer_stream.transfer_id,
                                             receiver,
                                             sender)
            session.transfer_data = transfer_data
            message_storage = MessageStorage()
            account = defer.maybeDeferred(message_storage.get_account, receiver)
            account.addCallback(lambda result: self._check_receiver_session(result))

            sender_account = defer.maybeDeferred(message_storage.get_account, sender)
            sender_account.addCallback(lambda result: self._check_sender_session(result, session))

            d1 = defer.DeferredList([account, sender_account], consumeErrors=True)
            d1.addCallback(lambda result: self._handle_pull_transfer(result, session, transfer_stream))
            return

        transfer_data = FileTransferData(file_selector.name,
                                         file_selector.size,
                                         file_selector.type,
                                         transfer_stream.transfer_id,
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

    @staticmethod
    def _is_file_transfer(session):
        # a file transfer session: a file-transfer stream and no RTP media
        if not any(stream.type == 'file-transfer' for stream in session.proposed_streams):
            return False
        remote_sdp = session._invitation.sdp.proposed_remote
        media_types = {media.media.decode() if isinstance(media.media, bytes) else media.media for media in remote_sdp.media if media.port != 0}
        return not media_types & {'audio', 'video'}

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

        sender_is_local = result[1][0] is True  # the sender account was found (see _check_sender_session)
        if not sender_is_local:
            sender, receiver = _aor(session.remote_identity.uri), _aor(session.local_identity.uri)
            if not _in_audio_call(receiver, sender):
                # a file from outside is only accepted from someone the receiver is in an audio call with
                log.info('Rejecting file transfer {session.call_id} from {sender} to {receiver}, no audio call between them'.format(session=session, sender=sender, receiver=receiver))
                session.reject(403, 'No Call In Progress')
                return

        stream.handler.save_directory = session.transfer_data.path
        log.info('File transfer from {sender.uri} to {receiver.uri} will be saved to {path}/{filename}'.format(**session.transfer_data.__dict__))
        self._accept_session(session, [stream])

    def _handle_pull_transfer(self, result, session, stream):
        reject_session = all([success is not True for (success, value) in result])
        if reject_session:
            self._reject_session(session, "Sender and receiver accounts for filetransfer were not found")
            return

        transfer_data = session.transfer_data
        original_filename = transfer_data.filename

        if not os.path.exists(transfer_data.full_path):
            filename, ext = os.path.splitext(transfer_data.filename)
            filename = re.sub(r"-[0-9]$", '', filename)
            transfer_data.filename = f'{filename}{ext}'

        if not os.path.exists(transfer_data.full_path):
            transfer_data.filename = original_filename
            transfer_data.update_path_for_receiver()

        if not os.path.exists(transfer_data.full_path):
            filename, ext = os.path.splitext(transfer_data.filename)
            filename = re.sub(r"-[0-9]$", '', filename)
            transfer_data.filename = f'{filename}{ext}'

        if os.path.exists(transfer_data.full_path):
            meta_filepath = os.path.join(transfer_data.path, f'meta-{transfer_data.filename}')
            import json
            from sipsimple.streams.msrp.filetransfer import FileSelector
            try:
                with open(meta_filepath, 'r') as meta_file:
                    metadata = json.load(meta_file)
            except (OSError, IOError,ValueError):
                log.warning('Could load metadata %s' % meta_filepath)
                session.reject(404)
                return
            if stream.file_selector.hash == metadata['hash']:
                file_selector_disk = FileSelector.for_file(os.path.join(transfer_data.path, transfer_data.filename))
                stream.file_selector = file_selector_disk
                NotificationCenter().add_observer(self, sender=session)
                self._accept_session(session, [stream])
                return
        else:
            log.warning(f'File transfer rejected, {original_filename} file is not found')
            session.reject(500)

    def _reject_session(self, session, error):
        log.warning(f'File transfer rejected: {error}')
        session.reject(404)

    def _accept_session(self, session, streams):
        try:
            session.accept(streams)
        except IllegalStateError:
            session.reject(500)

    def incoming_publish(self, publish_request, data):
        # Handle the XCAP server's xcap-diff PUBLISH: when a user's addressbook
        # document changes on XCAP, the XCAP/presence side PUBLISHes an
        # 'xcap-diff' event to us. We answer it and notify every online device
        # of that account to re-fetch its addressbook (so changes made on one
        # device, or by the server, propagate to all the user's devices).
        headers = data.headers

        def _hval(h):
            return str(getattr(h, 'body', h)).strip() if h is not None else None

        event_header = headers.get('Event')
        event = getattr(event_header, 'event', None) or (_hval(event_header) or '').split(';', 1)[0].strip() or None
        if event is None and event_header is not None:
            event = _hval(event_header)
        if not event or 'xcap-diff' not in str(event).lower():
            log.info('rejecting PUBLISH with unsupported event %r' % event)
            publish_request.answer(489)  # Bad Event
            return

        request_uri = data.request_uri
        if request_uri is None or request_uri.user is None:
            publish_request.answer(400)
            return

        account = '%s@%s' % (request_uri.user, request_uri.host)
        publish_request.answer(200)
        log.info('received xcap-diff PUBLISH for %s — notifying online devices to refresh addressbook' % account)
        NotificationCenter().post_notification(name='SIPApplicationGotAddressbookUpdate',
                                               sender=account,
                                               data=NotificationData())

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, message_request, data):
        if contact_token(data.request_uri) is not None:
            # A MESSAGE the proxy forked to the contact of a WebRTC client account
            # using the sipsimple SIP backend. Messages reach the clients through
            # the message storage, from the copy sent to this server, so like the
            # Janus SIP plugin does for its contacts, accept it and drop it.
            log.debug('dropping SIP MESSAGE sent to WebRTC client contact %s' % data.request_uri)
            message_request.answer(200)
            return
        content_type = data.headers.get('Content-Type', Null).content_type
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)

        if Null in (content_type, from_header, to_header):
            message_request.answer(400)
            return

        from_account = '%s@%s' % (from_header.uri.user, from_header.uri.host)
        to_account = '%s@%s' % (to_header.uri.user, to_header.uri.host)

        if not data.body:
            log.warning('SIP message from %s to %s rejected: empty body' % (from_account, to_account))
            message_request.answer(400)
            return

        message_id = None
        if content_type == 'message/cpim':
            try:
                cpim_message = CPIMPayload.decode(data.body)
            except (CPIMParserError, UnicodeDecodeError):  # TODO: fix decoding in sipsimple
                log.warning('SIP message from %s to %s rejected: CPIM parse error' % (from_account, to_account))
                message_request.answer(400)
                return
            else:
                content_type = cpim_message.content_type
                message_id = next((header.value for header in cpim_message.additional_headers if header.name == 'Message-ID'), None)

        # Loop mitigation: drop self-addressed control messages where From == To
        # and the originator is the local sylkserver identity. These happen
        # when an outgoing reply (typically application/sylk-api-token) is
        # looped back to us by the proxy, which would otherwise re-trigger the
        # same handler in an infinite cycle.
        if from_header.uri.user == 'sylkserver' and from_account == to_account:
            log.warning('dropping looped SIP message (%s) from %s to %s — sylkserver self-addressed' %
                        (content_type, from_account, to_account))
            message_request.answer(200)
            return

        log_message = log.debug if content_type in QUIET_CONTENT_TYPES else log.info
        if message_id is not None:
            log_message('received SIP %s message %s from %s to %s' % (content_type, message_id, from_account, to_account))
        else:
            log_message('received SIP %s message from %s to %s' % (content_type, from_account, to_account))

        if content_type == 'application/sylk-api-pgp-key-lookup':
            self._answer_public_key_lookup(message_request, data, to_account)
            return

        message_request.answer(200)

        message_handler = MessageHandler()
        message_handler.incoming_message(data)

    def _answer_public_key_lookup(self, message_request, data, account):
        """A public key lookup is answered with what the server keeps, so a client can tell
        "no key" from "no answer" (it must not generate a new key for an account that has one):
          200                 a key is kept; it follows as a text/pgp-public-key message
          404 No Public Key   no key is kept for the account
          500                 the key storage could not be read
        """
        def answer(public_key):
            if public_key:
                message_request.answer(200)
                MessageHandler().incoming_message(data)     # sends the key, as before
            else:
                log.info('Public key lookup for %s: no public key' % account)
                message_request.answer(404, reason='No Public Key')

        def failed(failure):
            log.warning('Public key lookup for %s failed: %s' % (account, failure.getErrorMessage()))
            message_request.answer(500)

        try:
            public_key = MessageStorage().get_public_key(account)
        except Exception as e:
            log.warning('Public key lookup for %s failed: %s' % (account, e))
            message_request.answer(500)
            return
        if isinstance(public_key, defer.Deferred):
            public_key.addCallbacks(answer, failed)
        else:
            answer(public_key)

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
