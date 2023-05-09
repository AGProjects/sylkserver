
import json
import os

import hashlib
from shutil import copyfileobj
from application.system import makedirs
from sipsimple.streams.msrp.filetransfer import FileSelector

from application.python.types import Singleton
from autobahn.twisted.resource import WebSocketResource
from sipsimple.configuration.settings import SIPSimpleSettings
from twisted.internet import defer, reactor
from twisted.python.failure import Failure
from twisted.web.server import Site
from werkzeug.exceptions import Forbidden, NotFound
from werkzeug.utils import secure_filename

from sylk import __version__ as sylk_version
from sylk.resources import Resources
from sylk.web import File, Klein, StaticFileResource, server

from .configuration import GeneralConfig, JanusConfig
from .datatypes import FileTransferData
from .factory import SylkWebSocketServerFactory
from .janus import JanusBackend
from .logger import log
from .models import sylkrtc
from .protocol import SYLK_WS_PROTOCOL
from .sip_handlers import MessageHandler
from .storage import TokenStorage, MessageStorage


__all__ = 'WebHandler', 'AdminWebHandler'


class FileUploadRequest(object):
    def __init__(self, shared_file, content):
        self.deferred = defer.Deferred()
        self.shared_file = shared_file
        self.content = content
        self.had_error = False


class ApiTokenAuthError(Exception): pass


class WebRTCGatewayWeb(object, metaclass=Singleton):
    app = Klein()

    def __init__(self, ws_factory):
        self._resource = self.app.resource()
        self._ws_resource = WebSocketResource(ws_factory)
        self._ws_factory = ws_factory

    @property
    def resource(self):
        return self._resource

    @app.route('/', branch=True)
    def index(self, request):
        return StaticFileResource(Resources.get('html/webrtcgateway/'))

    @app.route('/ws')
    def ws(self, request):
        return self._ws_resource

    @app.route('/filesharing/<string:conference>/<string:session_id>/<string:filename>', methods=['OPTIONS', 'POST', 'GET'])
    def filesharing(self, request, conference, session_id, filename):
        conference_uri = conference.lower()
        if conference_uri in self._ws_factory.videorooms:
            videoroom = self._ws_factory.videorooms[conference_uri]
            if session_id in videoroom:
                request.setHeader('Access-Control-Allow-Origin', '*')
                request.setHeader('Access-Control-Allow-Headers', 'content-type')
                method = request.method.upper().decode()
                session = videoroom[session_id]
                if method == 'POST':
                    def log_result(result):
                        if isinstance(result, Failure):
                            videoroom.log.warning('{file.uploader.uri} failed to upload {file.filename}: {error}'.format(file=upload_request.shared_file, error=result.value))
                        else:
                            videoroom.log.info('{file.uploader.uri} has uploaded {file.filename}'.format(file=upload_request.shared_file))
                        return result

                    filename = secure_filename(filename)
                    filesize = int(request.getHeader('Content-Length'))
                    shared_file = sylkrtc.SharedFile(filename=filename, filesize=filesize, uploader=dict(uri=session.account.id, display_name=session.account.display_name), session=session_id)
                    session.owner.log.info('wants to upload file {filename} to video room {conference_uri} with session {session_id}'.format(filename=filename, conference_uri=conference_uri, session_id=session_id))
                    upload_request = FileUploadRequest(shared_file, request.content)
                    videoroom.add_file(upload_request)
                    upload_request.deferred.addBoth(log_result)
                    return upload_request.deferred
                elif method == 'GET':
                    filename = secure_filename(filename)
                    session.owner.log.info('wants to download file {filename} from video room {conference_uri} with session {session_id}'.format(filename=filename, conference_uri=conference_uri, session_id=session_id))
                    try:
                        path = videoroom.get_file(filename)
                    except LookupError as e:
                        videoroom.log.warning('{session.account.id} failed to download {filename}: {error}'.format(session=session, filename=filename, error=e))
                        raise NotFound()
                    else:
                        videoroom.log.info('{session.account.id} is downloading {filename}'.format(session=session, filename=filename))
                        request.setHeader('Content-Disposition', 'attachment;filename=%s' % filename)
                        return File(path)
                else:
                    return 'OK'
        raise Forbidden()

    @app.route('/filetransfer/<string:sender>/<string:receiver>/<string:transfer_id>/<string:filename>', methods=['GET', 'POST', 'OPTIONS'])
    def filetransfer(self, request, sender, receiver, transfer_id, filename):
        request.setHeader('Access-Control-Allow-Origin', '*')
        request.setHeader('Access-Control-Allow-Headers', 'content-type')
        method = request.method.upper().decode()

        if method == 'POST':
            ip = request.getClientIP()
            connection_handlers = [connection.connection_handler for connection in self._ws_factory.connections if connection.peer.split(":")[1] == ip]
            sender_connection = next((connection_handler for connection_handler in connection_handlers if sender in connection_handler.accounts_map), False)
            if not sender_connection:
                raise Forbidden

            # TODO: Form support to support extra metadata?
            filename = secure_filename(filename)
            filesize = int(request.getHeader('Content-Length'))
            filetype = request.getHeader('Content-Type') if request.getHeader('Content-Type') else 'application/octet-stream'
            transfer_data = FileTransferData(filename, filesize, filetype, transfer_id, sender, receiver, content=request.content)

            message_storage = MessageStorage()
            account = defer.maybeDeferred(message_storage.get_account, receiver)
            account.addCallback(lambda result: self._check_receiver(result))

            sender_account = defer.maybeDeferred(message_storage.get_account, sender)
            sender_account.addCallback(lambda result: self._check_sender(result, transfer_data))

            d1 = defer.DeferredList([account, sender_account], consumeErrors=True)
            d1.addCallback(lambda result: self._handle_lookup_result(result, transfer_data, sender_connection))
            return d1
        elif method == 'GET':
            settings = SIPSimpleSettings()
            folder = os.path.join(settings.file_transfer.directory.normalized, sender[:1], sender, receiver, transfer_id)
            path = f'{folder}/{filename}'
            log_path = os.path.join(sender, receiver, transfer_id, filename)
            if os.path.exists(path):
                file_size = os.path.getsize(path)
                split_tup = os.path.splitext(path)
                file_extension = split_tup[1]
                render_type = 'inline' if file_extension and file_extension.lower() in ('.jpg', '.png', '.jpeg', '.gif') else 'attachment'
                request.setHeader('Content-Disposition', '%s;filename=%s' % (render_type, filename))
                log.info('Web %s file download %s (%s)' % (render_type, log_path, FileTransferData.format_file_size(file_size)))
                return File(path)
            else:
                log.warning('Download failed, file not found: %s' % (log_path))
                raise NotFound()
        else:
            return 'OK'

    def _check_receiver(self, account):
        if account is None:
            raise Exception("Receiver account for file upload not found")

    def _check_sender(self, account, transfer_data):
        if account is None:
            transfer_data.update_path_for_receiver()
            raise Exception("Sender account for file upload not found")

    def _handle_lookup_result(self, result, transfer_data, connection):
        reject_session = all([success is not True for (success, value) in result])
        if reject_session:
            self._reject_upload("Sender and receiver accounts for file upload were not found")
            return

        log.info('File upload from {sender.uri} to {receiver.uri} will be saved to {path}/{filename}'.format(**transfer_data.__dict__))
        return self._accept_upload(transfer_data, connection)

    def _reject_upload(self, error):
        log.warning(f'File upload rejected: {error}')
        raise NotFound()

    def _accept_upload(self, transfer_data, connection):
        makedirs(transfer_data.path)
        with open(os.path.join(transfer_data.path, transfer_data.filename), 'wb') as output_file:
            copyfileobj(transfer_data.content, output_file)

        part_size = 64 * 1024
        sha1 = hashlib.sha1()

        with open(os.path.join(transfer_data.path, transfer_data.filename), 'rb') as f:
            while True:
                data = f.read(part_size)
                if not data:
                    break
                sha1.update(data)

        file_selector = FileSelector.for_file(os.path.join(transfer_data.path, transfer_data.filename))
        file_selector.hash = sha1

        metadata = sylkrtc.TransferredFile(**transfer_data.__dict__, hash=file_selector.hash)

        meta_filepath = os.path.join(transfer_data.path, f'meta-{metadata.filename}')

        try:
            with open(meta_filepath, 'w+') as output_file:
                output_file.write(json.dumps(metadata.__data__))
        except (OSError, IOError):
            log.warning('Could not save metadata %s' % meta_filepath)

        payload = transfer_data.cpim_message_payload(metadata)

        message_handler = MessageHandler()
        message_handler.outgoing_message_to_self(f'sip:{metadata.receiver.uri}', payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')
        message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')
        message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')

        message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', transfer_data.message_payload, content_type='text/plain', identity=f'sip:{metadata.sender.uri}')
        message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', transfer_data.message_payload, content_type='text/plain', identity=f'sip:{metadata.sender.uri}')
        return "OK"


    def verify_api_token(self, request, account, msg_id, token=None):
        if token:
            auth_headers = request.requestHeaders.getRawHeaders('Authorization', default=None)
            if auth_headers:
                try:
                    method, auth_token = auth_headers[0].split()
                except ValueError:
                    log.warning(f'Authorization headers is not correct for message history request for {account}, it should be in the format: Apikey [TOKEN]')
            else:
                log.warning(f'Authorization headers missing on message history request for {account}')

            if not auth_headers or method != 'Apikey' or auth_token != token:
                log.warning(f'Token authentication error for {account}')
                raise ApiTokenAuthError()
            else:
                log.info(f'Returning message history for {account}')
                return self.get_account_messages(request, account, msg_id)
        else:
            log.warning(f'Token not found for {account}')
            raise ApiTokenAuthError()

    def tokenError(self, error, request):
        raise ApiTokenAuthError()

    def get_account_messages(self, request, account, msg_id=None):
        account = account.lower()
        storage = MessageStorage()
        messages = storage[[account, msg_id]]
        request.setHeader('Content-Type', 'application/json')
        if isinstance(messages, defer.Deferred):
            return messages.addCallback(lambda result:
                                        json.dumps(sylkrtc.MessageHistoryData(account=account, messages=result).__data__))

    @app.handle_errors(ApiTokenAuthError)
    def auth_error(self, request, failure):
        request.setResponseCode(401)
        return b'Unauthorized'

    @app.route('/messages/history/<string:account>', methods=['OPTIONS', 'GET'])
    @app.route('/messages/history/<string:account>/<string:msg_id>', methods=['OPTIONS', 'GET'])
    def messages(self, request, account, msg_id=None):
        storage = MessageStorage()
        token = storage.get_account_token(account)
        if isinstance(token, defer.Deferred):
            token.addCallback(lambda result: self.verify_api_token(request, account, msg_id, result))
            return token
        else:
            return self.verify_api_token(request, account, msg_id, token)


class WebHandler(object):
    def __init__(self):
        self.backend = None
        self.factory = None
        self.resource = None
        self.web = None

    def start(self):
        ws_url = 'ws' + server.url[4:] + '/webrtcgateway/ws'
        self.factory = SylkWebSocketServerFactory(ws_url, protocols=[SYLK_WS_PROTOCOL], server='SylkServer/%s' % sylk_version)
        self.factory.setProtocolOptions(allowedOrigins=GeneralConfig.web_origins,
                                        allowNullOrigin=GeneralConfig.web_origins == ['*'],
                                        autoPingInterval=GeneralConfig.websocket_ping_interval,
                                        autoPingTimeout=GeneralConfig.websocket_ping_interval/2)

        self.web = WebRTCGatewayWeb(self.factory)
        server.register_resource(b'webrtcgateway', self.web.resource)

        log.info('WebSocket handler started at %s' % ws_url)
        log.info('Allowed web origins: %s' % ', '.join(GeneralConfig.web_origins))
        log.info('Allowed SIP domains: %s' % ', '.join(GeneralConfig.sip_domains))
        log.info('Using Janus API: %s' % JanusConfig.api_url)

        self.backend = JanusBackend()
        self.backend.start()

    def stop(self):
        if self.factory is not None:
            for conn in self.factory.connections.copy():
                conn.dropConnection(abort=True)
            self.factory = None
        if self.backend is not None:
            self.backend.stop()
            self.backend = None


# TODO: This implementation is a prototype.  Moving forward it probably makes sense to provide admin API
# capabilities for other applications too.  This could be done in a number of ways:
#
# * On the main web server, under a /admin/ parent route.
# * On a separate web server, which could listen on a different IP and port.
#
# In either case, HTTPS aside, a token based authentication mechanism would be desired.
# Which one is best is not 100% clear at this point.

class AuthError(Exception): pass


class AdminWebHandler(object, metaclass=Singleton):
    app = Klein()

    def __init__(self):
        self.listener = None

    def start(self):
        host, port = GeneralConfig.http_management_interface
        # noinspection PyUnresolvedReferences
        self.listener = reactor.listenTCP(port, Site(self.app.resource()), interface=host)
        log.info('Admin web handler started at http://%s:%d' % (host, port))

    def stop(self):
        if self.listener is not None:
            self.listener.stopListening()
            self.listener = None

    # Admin web API

    def _check_auth(self, request):
        auth_secret = GeneralConfig.http_management_auth_secret
        if auth_secret:
            auth_headers = request.requestHeaders.getRawHeaders('Authorization', default=None)
            if not auth_headers or auth_headers[0] != auth_secret:
                raise AuthError()

    @app.handle_errors(AuthError)
    def auth_error(self, request, failure):
        request.setResponseCode(403)
        return 'Authentication error'

    @app.route('/tokens/<string:account>')
    def get_tokens(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        storage = TokenStorage()
        tokens = storage[account]
        if isinstance(tokens, defer.Deferred):
            return tokens.addCallback(lambda result: json.dumps({'tokens': result}))
        else:
            return json.dumps({'tokens': tokens})

    @app.route('/tokens/<string:account>/<string:device_token>', methods=['DELETE'])
    def process_token(self, request, account, device_token):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        storage = TokenStorage()
        if request.method == 'DELETE':
            storage.remove(account, device_token)
        return json.dumps({'success': True})
