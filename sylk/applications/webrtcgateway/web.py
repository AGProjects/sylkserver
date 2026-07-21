
import base64
import datetime
import hashlib
import hmac
import json
import math
import mimetypes
import os
import secrets
import time
from shutil import copyfileobj, rmtree

from application.notification import IObserver, NotificationCenter
from application.python.types import Singleton
from application.system import makedirs
from autobahn.twisted.resource import WebSocketResource
from sipsimple.streams.msrp.filetransfer import FileSelector
from sipsimple.threading import run_in_thread
from sipsimple.threading.green import call_in_green_thread
from twisted.internet import defer, reactor, threads
from twisted.internet.ssl import DefaultOpenSSLContextFactory
from twisted.python.failure import Failure
from twisted.web.server import Site, NOT_DONE_YET
from zope.interface import implementer
from werkzeug.exceptions import Forbidden, NotFound
from werkzeug.utils import safe_join, secure_filename

from sylk import __version__ as sylk_version
from sylk.configuration import WebServerConfig
from sylk.resources import Resources
from sylk.web import (File, Klein, StaticFileResource,
                      TrackedUploadHTTPChannel, server)

from .audio_level_udp import AudioLevelUDPClient
from .configuration import (BUILTIN_WEBSERVER, CassandraConfig,
                            FileStorageConfig, GeneralConfig, JanusConfig)
from .datatypes import FileTransferData
from .factory import SylkWebSocketServerFactory
from .janus import JanusBackend
from .logger import log
from .metrics import Metrics
from .models import sylkrtc
from .protocol import SYLK_WS_PROTOCOL
from .sip_handlers import MessageHandler
from .storage import CASSANDRA_MODULES_AVAILABLE, MessageStorage, TokenStorage

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

    # Admin/management API mounted on the built-in web server. Only active
    # when http_management_interface or https_management_interface is set
    # to 'builtin_webserver' (and authentication is configured) — see
    # AdminWebHandler.start. Two routes are needed: the branch route does
    # not match the bare '/admin/' URL (werkzeug's path converter requires
    # a non-empty remainder), which would otherwise fall through to the
    # static-files catch-all instead of the admin UI.

    @app.route('/admin/')
    def admin_index(self, request):
        resource = AdminWebHandler().builtin_resource
        if resource is None:
            raise NotFound()
        return resource

    @app.route('/admin', branch=True)
    def admin(self, request):
        resource = AdminWebHandler().builtin_resource
        if resource is None:
            raise NotFound()
        return resource

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
                        request.setHeader('Content-Disposition', 'attachment;filename="%s"' % filename)
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
            filename = secure_filename(filename)
            transfer_id = secure_filename(transfer_id)
            if not filename or not transfer_id:
                raise Forbidden

            ip = request.getClientIP()
            connection_handlers = [connection.connection_handler for connection in self._ws_factory.connections if connection.peer.split(":")[1] == ip]
            sender_connection = next((connection_handler for connection_handler in connection_handlers if sender in connection_handler.accounts_map), False)
            if not sender_connection:
                raise Forbidden

            # TODO: Form support to support extra metadata?

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
            folder = safe_join(GeneralConfig.file_transfer_dir.normalized, sender[:1], sender, receiver, transfer_id)
            if not folder:
                raise Forbidden

            path = safe_join(folder, filename)
            log_path = os.path.join(sender, receiver, transfer_id, filename)

            if not path or not os.path.exists(path):
                log.warning('Download failed, file not found: %s' % (log_path))
                raise NotFound()

            _, file_extension = os.path.splitext(path)
            render_type = 'inline' if file_extension.lower() in ('.jpg', '.png', '.jpeg', '.gif', '.mov', '.mp4', '.webm') else 'attachment'
            if render_type == 'inline':
                mime_type, encoding = mimetypes.guess_type(path)
                if mime_type:
                    request.setHeader("Content-Type", mime_type)
                else:
                    request.setHeader("Content-Type", "application/octet-stream")

            request.setHeader('Content-Disposition', '%s;filename="%s"' % (render_type, filename))
            file_size = os.path.getsize(path)
            log.info('Web %s file download %s (%s)' % (render_type, log_path, FileTransferData.format_file_size(file_size)))
            return File(path)
        else:
            return 'OK'

    @app.route('/filetransfer/cancel/<string:transfer_id>', methods=['POST', 'GET'])
    def cancel_upload(self, request, transfer_id):
        log.info(f'Cancel upload {transfer_id}')
        channel = TrackedUploadHTTPChannel.active_uploads.get(transfer_id)
        if channel:
            try:
                channel.transport.abortConnection()
            except AttributeError:
                pass
            TrackedUploadHTTPChannel.active_uploads.pop(transfer_id, None)
            log.debug(f'Cancelled upload {transfer_id}')
            return f"Cancelled upload {transfer_id}\n"
        else:
            return f"No active upload with transfer_id {transfer_id}\n"

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
        with open(transfer_data.full_path, 'wb') as output_file:
            copyfileobj(transfer_data.content, output_file)

        part_size = 64 * 1024
        sha1 = hashlib.sha1()

        with open(transfer_data.full_path, 'rb') as f:
            while True:
                data = f.read(part_size)
                if not data:
                    break
                sha1.update(data)

        file_selector = FileSelector.for_file(transfer_data.full_path)
        file_selector.hash = sha1

        metadata = sylkrtc.TransferredFile(**transfer_data.__dict__, hash=file_selector.hash)

        meta_filepath = os.path.join(transfer_data.path, f'meta-{metadata.filename}')

        try:
            with open(meta_filepath, 'w+') as output_file:
                output_file.write(json.dumps(metadata.__data__))
        except (OSError, IOError):
            log.warning('Could not save metadata %s' % meta_filepath)

        message_handler = MessageHandler()
        payload = transfer_data.cpim_message_payload(metadata)
        message_handler.outgoing_message_to_self(f'sip:{metadata.receiver.uri}', payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')

        xml_payload = transfer_data.cpim_rcsfthttp_message_payload(metadata)
        message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', xml_payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')
        message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', xml_payload, content_type='message/cpim', identity=f'sip:{metadata.sender.uri}')

        if not metadata.filename.endswith('.asc'):
            message_handler.outgoing_replicated_message(f'sip:{metadata.receiver.uri}', transfer_data.message_payload, content_type='text/plain', identity=f'sip:{metadata.sender.uri}')
            message_handler.outgoing_message(f'sip:{metadata.receiver.uri}', transfer_data.message_payload, content_type='text/plain', identity=f'sip:{metadata.sender.uri}')

        return "OK"

    def verify_api_token(self, request, account, msg_id, token=None):
        # print(msg_id)
        # return self.get_account_messages(request, account)
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
                return self.get_account_messages(request, account, msg_id)
        else:
            log.warning(f'Token not found for {account}')
            raise ApiTokenAuthError()

    def tokenError(self, error, request):
        raise ApiTokenAuthError()

    def get_account_messages(self, request, account, msg_id=None):
        # Demoted to .debug — fired on every history pull from every
        # client and was originally added while wiring up the message
        # storage endpoint. Re-enable by flipping to .info when
        # troubleshooting the history HTTP path.
        log.debug(f'Returning message history for {account}')
        account = account.lower()
        storage = MessageStorage()
        messages = storage[[account, msg_id]]
        request.setHeader('Content-Type', 'application/json')
        if isinstance(messages, defer.Deferred):
            return messages.addCallback(lambda result:
                                        json.dumps(sylkrtc.MessageHistoryData(account=account, messages=result[:5000]).__data__))

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

        Metrics().start()

    def stop(self):
        if self.factory is not None:
            for conn in self.factory.connections.copy():
                conn.dropConnection(abort=True)
            self.factory = None
        if self.backend is not None:
            self.backend.stop()
            self.backend = None
        Metrics().stop()


ADMIN_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SylkServer WebRTC Gateway Application Admin frontend</title>
<style>
  :root {
    --bg: #0f172a; --panel: #ffffff; --muted: #64748b; --border: #e2e8f0;
    --accent: #2563eb; --accent-d: #1d4ed8; --ok: #16a34a; --row: #f8fafc;
    --danger: #dc2626; --text: #0f172a;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--text); background:#f1f5f9; }
  header.topbar { background: var(--bg); color:#fff; padding:14px 22px; display:flex; align-items:center; gap:14px; }
  header.topbar h1 { font-size:16px; font-weight:600; margin:0; letter-spacing:.2px; }
  header.topbar .dot { width:9px; height:9px; border-radius:50%; background:var(--ok); box-shadow:0 0 0 3px rgba(22,163,74,.25); }
  header.topbar .spacer { flex:1; }
  header.topbar .who { font-size:13px; color:#cbd5e1; }
  .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(230px, 1fr)); gap:14px; margin-bottom:14px; }
  .stat { padding:18px 20px; }
  .stat .lbl { font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:var(--muted); margin-bottom:8px; }
  .stat .num { font-size:26px; font-weight:700; line-height:1.1; }
  .stat .sub { font-size:12px; color:var(--muted); margin-top:8px; line-height:1.5; }
  .stat .err { color:var(--danger); font-size:13px; }
  nav.tabs { background:#1e293b; padding:0 22px; display:flex; gap:6px; }
  nav.tabs button { background:none; border:0; color:#94a3b8; font-size:14px; font-weight:600; padding:12px 16px; cursor:pointer; border-bottom:3px solid transparent; }
  nav.tabs button:hover { color:#e2e8f0; }
  nav.tabs button.active { color:#fff; border-bottom-color:var(--accent); }
  .btn { border:0; border-radius:7px; padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; }
  .btn-light { background:#1e293b; color:#e2e8f0; }
  .btn-light:hover { background:#334155; }
  .btn-accent { background:var(--accent); color:#fff; }
  .btn-accent:hover { background:var(--accent-d); }
  main { max-width:1080px; margin:26px auto; padding:0 22px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:12px; box-shadow:0 1px 2px rgba(15,23,42,.04); }
  .toolbar { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
  .toolbar h2 { font-size:18px; margin:0; }
  .toolbar .count { font-size:13px; color:var(--muted); background:#e2e8f0; padding:3px 9px; border-radius:20px; }
  .toolbar .spacer { flex:1; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:12px 16px; font-size:14px; }
  thead th { font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:var(--muted); border-bottom:1px solid var(--border); }
  tbody tr { border-top:1px solid var(--border); cursor:pointer; }
  tbody tr:hover { background:var(--row); }
  td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color:#334155; }
  .pill { display:inline-block; min-width:22px; text-align:center; background:#eff6ff; color:var(--accent-d); font-weight:600; border-radius:20px; padding:2px 10px; font-size:13px; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
  .chev { color:#94a3b8; }
  /* login */
  .login-wrap { min-height:70vh; display:flex; align-items:center; justify-content:center; }
  .login-card { width:340px; padding:28px; }
  .login-card h2 { margin:0 0 4px; font-size:20px; }
  .login-card p { margin:0 0 20px; color:var(--muted); font-size:13px; }
  .field { margin-bottom:14px; }
  .field label { display:block; font-size:12px; font-weight:600; color:#475569; margin-bottom:6px; }
  .field input { width:100%; padding:10px 12px; border:1px solid var(--border); border-radius:8px; font-size:14px; }
  .field input:focus { outline:0; border-color:var(--accent); box-shadow:0 0 0 3px rgba(37,99,235,.15); }
  .err { color:var(--danger); font-size:13px; margin-top:6px; min-height:18px; }
  .full { width:100%; }
  /* drawer */
  .overlay { position:fixed; inset:0; background:rgba(15,23,42,.45); display:none; }
  .overlay.open { display:block; }
  .drawer { position:fixed; top:0; right:0; height:100%; width:min(860px,96vw); background:#fff; box-shadow:-8px 0 30px rgba(0,0,0,.18); transform:translateX(100%); transition:transform .2s ease; display:flex; flex-direction:column; }
  .overlay.open .drawer { transform:translateX(0); }
  .drawer header { padding:20px 26px; border-bottom:1px solid var(--border); display:flex; align-items:flex-start; gap:10px; }
  .drawer header .x { margin-left:auto; cursor:pointer; border:0; background:none; font-size:24px; color:#94a3b8; line-height:1; }
  .drawer header h3 { margin:0; font-size:17px; word-break:break-all; }
  .drawer header .sub { font-size:13px; color:var(--muted); margin-top:4px; }
  .drawer .body { padding:0; overflow:auto; flex:1; }
  .drawer .body th, .drawer .body td { padding:14px 26px; }
  .hint { font-size:12px; color:var(--muted); padding:14px 26px 20px; }
  .badge { display:inline-block; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; padding:2px 8px; border-radius:5px; }
  .badge-webrtc { background:#eef2ff; color:#4338ca; }
  .badge-sip { background:#ecfdf5; color:#047857; }
  .badge-bridge { background:#fef3c7; color:#92400e; }
  .badge-muted { background:#fef2f2; color:#b91c1c; margin-left:6px; }
  .pbtn { border:1px solid var(--border); background:#fff; border-radius:7px; padding:6px 12px; font-size:13px; font-weight:600; cursor:pointer; color:#334155; }
  .pbtn:hover { background:#f1f5f9; }
  .pbtn-kick { border-color:#fecaca; color:var(--danger); }
  .pbtn-kick:hover { background:#fef2f2; }
  .pbtn:disabled { opacity:.5; cursor:default; }
  .actions { display:flex; gap:8px; justify-content:flex-end; }
  .lvl-cell { width:150px; }
  .meter { width:130px; height:9px; background:#e2e8f0; border-radius:5px; overflow:hidden; }
  .meter > i { display:block; height:100%; width:0%; border-radius:5px; background:linear-gradient(90deg,#22c55e 0%,#22c55e 55%,#eab308 78%,#ef4444 100%); transition:width .1s linear; }
  .meter.stale { opacity:.35; }
  .meter.nopid { opacity:.25; }
  .lvl-num { font-size:11px; color:#94a3b8; margin-top:3px; font-family:ui-monospace,Menlo,monospace; }
  tr.speaking { background:#f0fdf4; }
  tr.speaking td:first-child { box-shadow:inset 3px 0 0 #22c55e; }
  .spkdot { display:inline-block; width:7px; height:7px; border-radius:50%; background:#cbd5e1; margin-left:6px; vertical-align:middle; }
  tr.speaking .spkdot { background:#22c55e; box-shadow:0 0 0 3px rgba(34,197,94,.25); }
  .toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%); background:#0f172a; color:#fff; padding:10px 18px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; }
  .toast.show { opacity:.95; }
  @media (max-width:640px){ th.hide, td.hide { display:none; } }
</style>
</head>
<body>
<div id="app"></div>

<div class="overlay" id="overlay">
  <aside class="drawer">
    <header>
      <div>
        <h3 id="d-uri">—</h3>
        <div class="sub" id="d-sub"></div>
      </div>
      <button class="x" onclick="closeDrawer()">&times;</button>
    </header>
    <div class="body" id="d-body"></div>
  </aside>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let refreshTimer = null, pollTimer = null, evtSource = null, currentRoom = null, audioTimer = null;

async function api(path, opts) {
  const o = Object.assign({ credentials: 'same-origin', headers: {} }, opts || {});
  const r = await fetch(path, o);
  return r;
}

function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function stripSip(u) {
  return (u == null ? '' : String(u)).replace(/^sips?:/i, '');
}

/* ---------- views ---------- */
function showLogin(configured) {
  stopLive();
  $('app').innerHTML = `
    <div class="login-wrap">
      <div class="card login-card">
        <h2>WebRTC Gateway</h2>
        <p>${configured ? 'Sign in' : 'Admin login is not configured on this server.'}</p>
        <form id="loginForm" action="login" method="post" autocomplete="on" ${configured ? '' : 'style="opacity:.5;pointer-events:none"'}>
          <div class="field"><label>Username</label><input id="u" name="username" autocomplete="username" autofocus></div>
          <div class="field"><label>Password</label><input id="p" name="password" type="password" autocomplete="current-password"></div>
          <button class="btn btn-accent full" type="submit">Sign in</button>
          <div class="err" id="loginErr"></div>
        </form>
      </div>
    </div>`;
  if (configured) {
    $('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      $('loginErr').textContent = '';
      const r = await api('login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: $('u').value, password: $('p').value }),
      });
      if (r.ok) {
        // Ask the browser's password manager to save the credentials.
        // A fetch login has no navigation for the heuristic to catch, so
        // we store explicitly via the Credential Management API (works in
        // a secure context: HTTPS, or localhost).
        if (window.PasswordCredential) {
          try {
            await navigator.credentials.store(new PasswordCredential({
              id: $('u').value, password: $('p').value, name: $('u').value,
            }));
          } catch (e) {}
        }
        boot();
      }
      else {
        const j = await r.json().catch(() => ({}));
        $('loginErr').textContent = j.error === 'invalid credentials' ? 'Invalid username or password.' : (j.error || 'Login failed.');
      }
    });
  }
}

const VIEWS = ['accounts', 'endpoints', 'sessions', 'conferences', 'messages', 'media'];
const DEFAULT_VIEW = VIEWS[0];
let currentView = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : DEFAULT_VIEW;

function switchView(v) {
  if (VIEWS.indexOf(v) === -1) v = DEFAULT_VIEW;
  currentView = v;
  try { history.replaceState(null, '', '#' + v); } catch (e) {}
  if (!$('tab-conferences')) return;   // not on the dashboard (login view)
  VIEWS.forEach(name => {
    $('tab-' + name).classList.toggle('active', name === v);
    $('view-' + name).style.display = name === v ? '' : 'none';
  });
  if (v !== 'conferences') closeDrawer();
  if (v === 'conferences') loadRooms();
  else if (v === 'sessions') loadSessions();
  else if (v === 'endpoints') loadEndpoints();
  else if (v === 'accounts') loadAccounts();
  else if (v === 'media') loadMedia();
  // 'messages' is fully on-demand (count / dump buttons), nothing to preload
  loadCharts(v);
}

const CHARTS = {
  endpoints: ['chart-endpoints', [['connections', '#2563eb', 'connections']], 'Connections per day'],
  accounts: ['chart-accounts', [['registrations', '#2563eb', 'registrations'], ['accounts', '#16a34a', 'unique accounts']], 'Registrations / unique accounts per day'],
  sessions: ['chart-sessions', [['sessions_audio', '#2563eb', 'audio'], ['sessions_video', '#7c3aed', 'video']], 'Sessions per day (audio / video)', 'sessions'],
  conferences: ['chart-conferences', [['conferences', '#2563eb', 'conferences']], 'Conferences per day'],
  messages: ['chart-messages', [['messages', '#2563eb', 'messages']], 'Messages per day'],
};
let metricsCache = { ts: 0, data: null };

async function loadCharts(view) {
  const cfg = CHARTS[view];
  if (!cfg || !$(cfg[0])) return;
  if (!metricsCache.data || Date.now() - metricsCache.ts > 60000) {
    let j = null;
    try {
      const r = await api('metrics/daily?days=30');
      if (r.status === 403) { boot(); return; }
      j = await r.json();
    } catch (e) {}
    if (!j) return;
    metricsCache = { ts: Date.now(), data: j };
  }
  const metrics = metricsCache.data.metrics || {};
  const series = cfg[1].map(([metric, color, label]) => ({ label: label || metric, color, points: metrics[metric] || [] }));
  const totalPoints = cfg[3] ? metrics[cfg[3]] : null;
  renderBarChart($(cfg[0]), series, cfg[2], totalPoints);
}

function renderBarChart(el, series, title, totalPoints) {
  const n = (series[0].points || []).length;
  if (!n) { el.style.display = 'none'; return; }
  el.style.display = '';
  const max = Math.max(1, ...series.flatMap(s => s.points.map(p => p.value)));
  const total = (totalPoints || series[0].points).reduce((a, p) => a + p.value, 0);
  const compact = v => v >= 10000 ? Math.round(v / 1000) + 'k' : v >= 1000 ? (v / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(v);
  const groups = [];
  for (let i = 0; i < n; i++) {
    const day = series[0].points[i].day;
    const label = day.slice(0, 4) + '-' + day.slice(4, 6) + '-' + day.slice(6);
    const tip = label + ' — ' + series.map(s => `${s.points[i].value} ${s.label}`).join(', ');
    const bars = series.map(s => {
      const v = s.points[i].value;
      const h = Math.round(v / max * 100);
      return `<div style="flex:1;height:${v ? Math.max(3, h) : 0}%;background:${s.color};border-radius:2px 2px 0 0;min-height:${v ? '2px' : '0'}"></div>`;
    }).join('');
    const values = series.map(s => s.points[i].value);
    const num = values.some(v => v)
      ? values.map((v, si) => `<span style="color:${series.length > 1 ? series[si].color : '#64748b'}">${compact(v)}</span>`).join('<br>')
      : '';
    groups.push(`<div title="${esc(tip)}" style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;min-width:0">
      <div style="font-size:9px;text-align:center;font-family:ui-monospace,Menlo,monospace;line-height:1.25;margin-bottom:2px;white-space:nowrap;overflow:hidden">${num}</div>
      <div style="display:flex;align-items:flex-end;gap:1px;height:90px">${bars}</div>
    </div>`);
  }
  const first = series[0].points[0].day, last = series[0].points[n - 1].day;
  const fmtDay = d => d.slice(4, 6) + '/' + d.slice(6);
  const legend = series.length > 1
    ? '<span style="margin-left:auto">' + series.map(s =>
        `<span style="font-size:11px;color:#64748b;margin-left:12px"><span style="display:inline-block;width:8px;height:8px;background:${s.color};border-radius:2px;margin-right:4px"></span>${esc(s.label)}</span>`).join('') + '</span>'
    : '';
  el.innerHTML = `
    <div style="display:flex;align-items:baseline">
      <div style="font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)">${esc(title)}</div>
      <span style="font-size:11px;color:#94a3b8;margin-left:10px">${total.toLocaleString()} in 30 days</span>
      ${legend}
    </div>
    <div style="display:flex;gap:2px;align-items:flex-end;margin-top:10px">${groups.join('')}</div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-top:4px">
      <span>${fmtDay(first)}</span><span>${fmtDay(last)}</span>
    </div>`;
}

function showDashboard(username) {
  $('app').innerHTML = `
    <header class="topbar">
      <span class="dot"></span>
      <h1>SylkServer WebRTC Gateway Application Admin frontend</h1>
      <span class="spacer"></span>
      <span class="who">${esc(username || '')}</span>
      <button class="btn btn-light" onclick="logout()">Sign out</button>
    </header>
    <nav class="tabs">
      <button id="tab-accounts" onclick="switchView('accounts')">Accounts</button>
      <button id="tab-endpoints" onclick="switchView('endpoints')">End-points</button>
      <button id="tab-sessions" onclick="switchView('sessions')">Sessions</button>
      <button id="tab-conferences" onclick="switchView('conferences')">Conferences</button>
      <button id="tab-messages" onclick="switchView('messages')">Messages</button>
      <button id="tab-media" onclick="switchView('media')">File transfers</button>
    </nav>
    <main>
      <section id="view-conferences">
        <div class="toolbar">
          <h2>Conferences</h2>
          <span class="count" id="roomCount">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadRooms()">Refresh</button>
        </div>
        <div class="card" id="chart-conferences" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="card">
          <table>
            <thead><tr>
              <th>Room URI</th>
              <th class="hide">Janus Room ID</th>
              <th>Sessions</th>
              <th style="width:30px"></th>
            </tr></thead>
            <tbody id="roomsBody">
              <tr><td colspan="4" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section id="view-sessions" style="display:none">
        <div class="toolbar">
          <h2>Sessions</h2>
          <span class="count" id="sessCount">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadSessions()">Refresh</button>
        </div>
        <div class="card" id="chart-sessions" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="card">
          <table>
            <thead><tr>
              <th>Session</th>
              <th class="hide" title="the client's signaling WebSocket (ip:port), not media">Signaling</th>
              <th>Media</th>
              <th>State</th>
              <th>Duration</th>
              <th class="hide">User agent</th>
            </tr></thead>
            <tbody id="sessBody">
              <tr><td colspan="6" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section id="view-endpoints" style="display:none">
        <div class="toolbar">
          <h2>End-points</h2>
          <span class="count" id="epCount">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadEndpoints()">Refresh</button>
        </div>
        <div class="card" id="chart-endpoints" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="card">
          <table>
            <thead><tr>
              <th>URI</th>
              <th class="hide">User agent</th>
              <th>Connection</th>
            </tr></thead>
            <tbody id="epBody">
              <tr><td colspan="3" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section id="view-accounts" style="display:none">
        <div class="toolbar">
          <h2>Accounts</h2>
          <span class="count" id="stBackend">—</span>
          <span class="spacer"></span>
          <button class="pbtn" id="delReqBtn" onclick="findDeletionRequests()">Marked for deletion</button>
          <button class="btn btn-accent" onclick="loadAccounts()">Refresh</button>
        </div>
        <div class="card" id="chart-accounts" style="margin-bottom:14px;padding:16px 20px"></div>
        <div id="delReqResult"></div>
        <div class="card" style="padding:16px;margin-bottom:14px">
          <form onsubmit="return lookupAccount(event)" style="display:flex;gap:10px;align-items:center">
            <input id="acctInput" placeholder="user@domain" autocomplete="off"
                   style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Show account</button>
          </form>
          <div id="acctResult"></div>
        </div>
        <div class="stats-grid" id="stGrid">
          <div class="card stat"><div class="lbl">Loading…</div></div>
        </div>
        <div class="hint" id="stHint" style="padding:0 4px"></div>
      </section>
      <section id="view-messages" style="display:none">
        <div class="toolbar">
          <h2>Messages</h2>
          <span class="spacer"></span>
        </div>
        <div class="card" id="chart-messages" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="stats-grid">
          <div class="card stat">
            <div class="lbl">Chat messages in database</div>
            <div class="num" id="msgCount">—</div>
            <div class="sub">
              <button class="pbtn" id="msgBtn" onclick="countMessages()">Count now</button>
              <span style="display:block;margin-top:8px">full table scan — may take a while on a large database; messages expire after one year (table TTL)</span>
            </div>
          </div>
        </div>
        <div class="card" style="padding:16px;margin-bottom:14px">
          <form onsubmit="return loadMessageTypes(event)" style="display:flex;gap:10px;align-items:center">
            <label for="msgAcctInput" style="font-size:13px;color:#64748b;white-space:nowrap">Account</label>
            <input id="msgAcctInput" placeholder="account (user@domain)" autocomplete="off"
                   style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Show messages</button>
          </form>
          <div id="msgContactRow" style="display:none;gap:10px;align-items:center;margin-top:10px">
            <label for="msgContactSelect" style="font-size:13px;color:#64748b;white-space:nowrap">Contact</label>
            <select id="msgContactSelect" onchange="msgSetContact(this.value)"
                    style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;background:#fff">
              <option value="">All contacts</option>
            </select>
          </div>
          <div id="msgDateRow" style="display:none;gap:10px;align-items:flex-start;margin-top:10px"></div>
          <div id="msgTypesResult"></div>
        </div>
        <div class="card" style="padding:16px">
          <form onsubmit="return dumpMessage(event)" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <input id="dumpAccount" placeholder="account (user@domain)" autocomplete="off"
                   style="flex:1;min-width:200px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <input id="dumpId" placeholder="message id" autocomplete="off"
                   style="flex:1.4;min-width:240px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Dump message</button>
          </form>
          <div id="dumpResult"></div>
        </div>
      </section>
      <section id="view-media" style="display:none">
        <div class="toolbar">
          <h2>File transfers</h2>
          <span class="count" id="mediaTotal">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadMedia()">Refresh</button>
        </div>
        <div class="stats-grid" id="mediaGrid">
          <div class="card stat"><div class="lbl">Loading…</div></div>
        </div>
        <div class="card" style="padding:16px;margin-bottom:14px">
          <form onsubmit="return loadFileTransfers(event)" style="display:flex;gap:10px;align-items:center">
            <label for="ftAcctInput" style="font-size:13px;color:#64748b;white-space:nowrap">Account</label>
            <input id="ftAcctInput" placeholder="account (user@domain)" autocomplete="off"
                   style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Show transfers</button>
          </form>
          <div id="ftContactRow" style="display:none;gap:10px;align-items:center;margin-top:10px">
            <label for="ftContactSelect" style="font-size:13px;color:#64748b;white-space:nowrap">Contact</label>
            <select id="ftContactSelect" onchange="ftSetContact(this.value)"
                    style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;background:#fff">
              <option value="">All contacts</option>
            </select>
          </div>
          <div id="ftResult"></div>
        </div>
        <div class="hint" style="padding:0 4px">Sizes are walked on the server and cached for two minutes.</div>
      </section>
    </main>`;
  switchView(currentView);
  startLive();
}

/* ---------- data ---------- */
async function loadRooms() {
  const r = await api('rooms');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({ rooms: [] }));
  const rooms = (j.rooms || []).slice().sort((a, b) => (a.uri || '').localeCompare(b.uri || ''));
  $('roomCount').textContent = rooms.length + (rooms.length === 1 ? ' room' : ' rooms');
  const body = $('roomsBody');
  if (!rooms.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty">No live conferences right now.</td></tr>`;
    return;
  }
  body.innerHTML = rooms.map(rm => `
    <tr onclick="openRoom('${encodeURIComponent(rm.uri)}','${esc(rm.uri)}')">
      <td class="mono">${esc(rm.uri)}</td>
      <td class="mono hide">${esc(rm.janus_room_id)}</td>
      <td><span class="pill">${esc(rm.sessions)}</span></td>
      <td class="chev">›</td>
    </tr>`).join('');
}

async function loadEndpoints() {
  const r = await api('endpoints');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({ endpoints: [] }));
  const eps = j.endpoints || [];
  $('epCount').textContent = eps.length + (eps.length === 1 ? ' connection' : ' connections');
  const body = $('epBody');
  if (!body) return;
  if (!eps.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty">No connected end-points right now.</td></tr>`;
    return;
  }
  // One row per account; a connection with no account yet gets a
  // placeholder row so it's still visible.
  const rows = [];
  eps.forEach(ep => {
    if (ep.accounts && ep.accounts.length) {
      ep.accounts.forEach(a => rows.push({
        uri: a.uri, name: a.display_name, ua: a.user_agent,
        reg: a.registration_state, addr: ep.address,
      }));
    } else {
      rows.push({ uri: null, name: null, ua: null, reg: null, addr: ep.address });
    }
  });
  body.innerHTML = rows.map(row => {
    const reg = row.reg === 'registered'
      ? '<span class="badge badge-sip">Registered</span>'
      : row.reg === 'failed'
      ? '<span class="badge badge-muted" style="margin-left:0">Failed</span>'
      : row.reg
      ? `<span class="badge" style="background:#f1f5f9;color:#475569">${esc(row.reg)}</span>`
      : '';
    const name = row.name ? `<div style="font-weight:600">${esc(row.name)} ${reg}</div>` : (reg ? `<div>${reg}</div>` : '');
    const uri = row.uri
      ? `${name}<div class="mono" style="font-size:12px;color:#64748b">${esc(stripSip(row.uri))}</div>`
      : '<span style="color:#94a3b8">(no account yet)</span>';
    return `
    <tr style="cursor:default">
      <td>${uri}</td>
      <td class="hide" style="font-size:13px;color:#475569">${esc(row.ua || '—')}</td>
      <td class="mono">${esc(row.addr || '—')}</td>
    </tr>`;
  }).join('');
}

function fmtDuration(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, '0'), ss = String(sec).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return n.toFixed(n >= 100 ? 0 : 1) + ' ' + units[i];
}

async function loadSessions() {
  const r = await api('sessions');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({ sessions: [] }));
  const ss = j.sessions || [];
  $('sessCount').textContent = ss.length + (ss.length === 1 ? ' session' : ' sessions');
  const body = $('sessBody');
  if (!body) return;
  if (!ss.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">No active one-to-one sessions right now.</td></tr>`;
    return;
  }
  // legs of the same call (both parties on this gateway) share a Call-ID —
  // use that to show the peer's user agent on each leg
  const legsByCall = {};
  ss.forEach(s => { if (s.call_id) (legsByCall[s.call_id] = legsByCall[s.call_id] || []).push(s); });
  body.innerHTML = ss.map(s => {
    const peer = s.call_id && (legsByCall[s.call_id] || []).length === 2
      ? legsByCall[s.call_id].find(l => l !== s) : null;
    const incoming = s.direction === 'incoming';
    const stateColors = { established: ['#ecfdf5', '#047857'], accepted: ['#ecfdf5', '#047857'],
                          early_media: ['#fef3c7', '#92400e'], ringing: ['#fef3c7', '#92400e'],
                          progress: ['#fef3c7', '#92400e'], connecting: ['#eff6ff', '#1d4ed8'],
                          terminated: ['#fef2f2', '#b91c1c'] };
    const c = stateColors[s.state] || ['#f1f5f9', '#475569'];
    const state = `<span class="badge" style="background:${c[0]};color:${c[1]}">${esc(s.state || '?')}</span>`;
    const mediaBadges = { audio: '<span class="badge badge-sip">Audio</span>',
                          video: '<span class="badge badge-webrtc">Video</span>' };
    const media = (s.media || []).map(m => mediaBadges[m] || `<span class="badge" style="background:#f1f5f9;color:#475569">${esc(m)}</span>`).join(' ') || '—';
    const rtp = Object.keys(s.media_ports || {}).map(m =>
      `<div class="mono" style="font-size:11px;color:#64748b;margin-top:3px" title="far-end RTP endpoint (${esc(m)})">${esc(m)} ${esc(s.media_ports[m])}</div>`).join('');
    const dir = incoming ? '←' : '→';
    const slow = (s.slow_download ? ' <span style="color:#dc2626;font-size:11px">↓slow</span>' : '')
               + (s.slow_upload ? ' <span style="color:#dc2626;font-size:11px">↑slow</span>' : '');
    const name = s.remote_display_name ? `<div style="font-weight:600">${esc(s.remote_display_name)}${slow}</div>` : (slow ? `<div>${slow}</div>` : '');
    return `
    <tr style="cursor:default">
      <td>
        ${name}
        <div class="mono" style="font-size:12px">${esc(stripSip(s.account || ''))} ${dir} ${esc(stripSip(s.remote_uri || ''))}</div>
        ${s.call_id ? `<div class="mono" style="font-size:11px;color:#94a3b8">${esc(s.call_id)}</div>` : ''}
      </td>
      <td class="mono hide">${esc(s.address || '—')}</td>
      <td>${media}${rtp}</td>
      <td>${state}</td>
      <td class="mono">${fmtDuration(s.duration)}</td>
      <td class="hide" style="font-size:13px;color:#475569">${esc(s.user_agent || '—')}
        ${peer ? `<div style="font-size:11px;color:#94a3b8" title="user agent of the other party (its own leg is listed too)">peer: ${esc(peer.user_agent || '?')}</div>` : ''}
      </td>
    </tr>`;
  }).join('');
}

function statTile(label, value, subLines) {
  const sub = (subLines || []).filter(Boolean).join('<br>');
  return `<div class="card stat">
    <div class="lbl">${label}</div>
    <div class="num">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ''}
  </div>`;
}

async function loadAccounts() {
  const grid = $('stGrid');
  if (!grid) return;
  const r = await api('storage');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { grid.innerHTML = `<div class="card stat"><div class="err">Failed to load account statistics.</div></div>`; return; }
  $('stBackend').textContent = j.backend === 'cassandra' ? 'Cassandra backend' : 'file backend';
  const tiles = [];
  const acc = j.accounts || {};
  tiles.push(acc.error
    ? statTile('Accounts', '—', [`<span class="err">${esc(acc.error)}</span>`])
    : statTile('Accounts', acc.total != null ? acc.total : '—', [
        acc.active_30d != null ? `${acc.active_30d} active last 30 days · ${acc.active_7d} last 7 days` : null,
        acc.with_api_token != null ? `${acc.with_api_token} with history API token` : null,
      ]));
  const pt = j.push_tokens || {};
  const platforms = pt.platforms
    ? Object.keys(pt.platforms).sort().map(k => `${esc(k)}: ${pt.platforms[k]}`).join(' · ')
    : '';
  tiles.push(pt.error
    ? statTile('Mobile push tokens', '—', [`<span class="err">${esc(pt.error)}</span>`])
    : statTile('Mobile push tokens', pt.total != null ? pt.total : '—', [
        pt.accounts != null ? `across ${pt.accounts} account${pt.accounts === 1 ? '' : 's'}` : null,
        platforms,
      ]));
  if (j.public_keys) {
    tiles.push(j.public_keys.error
      ? statTile('PGP public keys', '—', [`<span class="err">${esc(j.public_keys.error)}</span>`])
      : statTile('PGP public keys', j.public_keys.total != null ? j.public_keys.total : '—',
                 ['accounts with an uploaded key']));
  }
  grid.innerHTML = tiles.join('');
  const backend = j.backend === 'cassandra' && j.cassandra
    ? `Cassandra keyspace <b>${esc(j.cassandra.keyspace || '?')}</b> @ ${esc((j.cassandra.contact_points || []).join(', '))} · tokens table: ${esc(j.cassandra.push_tokens_table)}`
    : `File storage in <span class="mono">${esc(j.storage_dir || '?')}</span>`;
  $('stHint').innerHTML = `${backend}<br>${esc(j.messages_note || '')}`;
}

function openAccount(encAccount) {
  $('acctInput').value = decodeURIComponent(encAccount);
  lookupAccount();
  const el = document.getElementById('acctResult');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function findDeletionRequests() {
  const btn = $('delReqBtn'), el = $('delReqResult');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px"><div class="empty" style="padding:10px">Scanning message store for account deletion requests…</div></div>`;
  let j = null;
  try {
    const r = await api('accounts/marked-for-deletion');
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if (btn) { btn.disabled = false; btn.textContent = 'Marked for deletion'; }
  if (!j) { el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px"><div class="err">Scan failed.</div></div>`; return; }
  if (j.error) { el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px"><div class="err">${esc(j.error)}</div></div>`; return; }
  if (!j.total) {
    el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px">
      <b style="font-size:14px">No accounts marked for deletion</b>
      <div style="font-size:12px;color:#64748b;margin-top:4px">no <span class="mono">${esc(j.content_type)}</span> messages found · scanned in ${esc(j.elapsed)}s</div>
    </div>`;
    return;
  }
  const rows = j.accounts.map(a => `
    <tr style="cursor:default">
      <td class="mono" style="font-size:13px;padding:8px 16px 8px 0">
        <a href="#" style="color:var(--accent)" onclick="openAccount('${encodeURIComponent(a.account)}');return false">${esc(a.account)}</a>
      </td>
      <td class="mono" style="font-size:13px;text-align:right;padding:8px 16px 8px 0">${a.requests}</td>
      <td class="mono" style="font-size:12px;color:#64748b;white-space:nowrap;padding:8px 0">${esc(a.last_request || '—')}</td>
    </tr>`).join('');
  el.innerHTML = `
    <div class="card" style="padding:16px;margin-bottom:14px">
      <b style="font-size:14px">${j.total} account${j.total === 1 ? '' : 's'} marked for deletion</b>
      <div style="font-size:12px;color:#64748b;margin-top:4px">accounts with <span class="mono">${esc(j.content_type)}</span> messages · scanned in ${esc(j.elapsed)}s · click an account to inspect it</div>
      <table style="width:auto;margin-top:6px"><thead><tr>
        <th style="padding:6px 16px 4px 0">Account</th>
        <th style="padding:6px 16px 4px 0;text-align:right">Requests</th>
        <th style="padding:6px 0 4px">Last request</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
}

async function lookupAccount(e) {
  if (e) e.preventDefault();
  const account = $('acctInput').value.trim().toLowerCase();
  if (!account) return false;
  $('acctResult').innerHTML = `<div class="empty" style="padding:18px">Looking up ${esc(account)}…</div>`;
  const r = await api('accounts/' + encodeURIComponent(account) + '/info');
  if (r.status === 403) { boot(); return false; }
  const j = await r.json().catch(() => null);
  renderAccount(j);
  return false;
}

function renderAccount(j) {
  const el = $('acctResult');
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return; }
  const m = j.messages || {};
  const tokens = (j.push_tokens || []).map(t => `<tr style="cursor:default">
      <td class="mono" style="font-size:12px;padding:8px 14px 8px 0">${esc(t.app_id || '?')}</td>
      <td class="mono" style="font-size:12px;padding:8px 14px 8px 0">${esc(t.device_id || '?')}</td>
      <td style="font-size:12px;padding:8px 14px 8px 0">${esc(t.platform || '')}</td>
      <td class="mono" style="font-size:12px;padding:8px 14px 8px 0;word-break:break-all">${esc(t.token || '')}</td>
      <td style="padding:8px 0;text-align:right">
        <button class="pbtn pbtn-kick" onclick="deleteToken('${encodeURIComponent(j.account)}','${encodeURIComponent(t.app_id || '')}','${encodeURIComponent(t.device_id || '')}',this)">Delete</button>
      </td>
    </tr>`).join('');
  const keys = (j.public_keys || []).map(k => `<pre style="font-size:11px;line-height:1.45;background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;margin:8px 0 0">${esc(k)}</pre>`).join('');
  el.innerHTML = `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <h3 style="margin:0;font-size:16px" class="mono">${esc(j.account)}</h3>
        ${j.found ? '<span class="badge badge-sip">message storage enabled</span>'
                  : '<span class="badge badge-muted" style="margin-left:0">message storage not enabled</span>'}
        <span class="spacer" style="flex:1"></span>
        <button class="pbtn pbtn-kick" onclick="purgeAccount('${encodeURIComponent(j.account)}')">Purge account</button>
      </div>
      ${j.found ? `
      <div style="margin-top:10px;font-size:13px;color:#334155">
        ${j.api_token ? `<div><b>API token:</b> <span class="mono" style="font-size:12px;word-break:break-all">${esc(j.api_token)}</span>${j.token_ttl != null ? ` <span style="color:#64748b">· TTL ${esc(j.token_ttl)}</span>` : ''}</div>` : ''}
        ${j.last_login ? `<div style="margin-top:4px"><b>Last login:</b> ${esc(j.last_login)}</div>` : ''}
      </div>` : ''}
      <div style="margin-top:14px">
        <b style="font-size:14px">${m.total != null ? Number(m.total).toLocaleString() : 0} messages stored</b>
        ${m.unread_text != null ? `<span style="font-size:13px;color:#475569"> · ${m.unread_text} unread text message${m.unread_text === 1 ? '' : 's'}</span>` : ''}
        ${m.total ? `<div style="font-size:12px;color:#64748b;margin-top:4px">
          <a href="#messages" style="color:var(--accent)" onclick="openMessagesFor('${encodeURIComponent(j.account)}');return false">show breakdown by type in the Messages tab →</a>
        </div>` : ''}
      </div>
      <div style="margin-top:14px">
        <b style="font-size:14px">${(j.push_tokens || []).length} push token(s) stored</b>
        ${tokens ? `<table style="width:auto;margin-top:2px"><thead><tr>
            <th style="padding:6px 14px 4px 0">App</th><th style="padding:6px 14px 4px 0">Device ID</th>
            <th style="padding:6px 14px 4px 0">Platform</th><th style="padding:6px 14px 4px 0">Token</th>
            <th style="padding:6px 0 4px"></th>
          </tr></thead><tbody>${tokens}</tbody></table>` : ''}
      </div>
      <div style="margin-top:14px">
        <b style="font-size:14px">${(j.public_keys || []).length} public key(s) stored</b>
        ${keys ? `<details style="margin-top:4px"><summary style="font-size:12px;color:#64748b;cursor:pointer">show key(s)</summary>${keys}</details>` : ''}
      </div>
    </div>`;
}

function openMessagesFor(encAccount) {
  switchView('messages');
  $('msgAcctInput').value = decodeURIComponent(encAccount);
  loadMessageTypes();
}

let msgFilters = { account: '', contact: '', date: '' };
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

async function loadMessageTypes(e) {
  if (e) e.preventDefault();
  const account = $('msgAcctInput').value.trim().toLowerCase();
  if (!account) return false;
  msgFilters = { account: account, contact: '', date: '' };
  const sel = $('msgContactSelect');
  if (sel) sel.innerHTML = '<option value="">All contacts</option>';
  if ($('msgContactRow')) $('msgContactRow').style.display = 'none';
  if ($('msgDateRow')) $('msgDateRow').style.display = 'none';
  msgFetch();
  return false;
}

function msgSetContact(contact) {
  msgFilters.contact = contact;
  msgFetch();
}

function msgSetDate(date) {
  msgFilters.date = date;
  msgFetch();
}

function msgDateLabel(date) {
  if (!date) return '';
  const year = date.slice(0, 4), month = date.slice(5, 7), day = date.slice(8, 10);
  if (day) return `${+day} ${MONTH_NAMES[+month - 1]} ${year}`;
  if (month) return `${MONTH_NAMES[+month - 1]} ${year}`;
  return year;
}

// Year -> Month -> Day drill-down with per-level counters, like the
// client-side messages date filter.
function msgRenderDates(j) {
  const row = $('msgDateRow');
  if (!row) return;
  const date = j.date || '';
  const counts = j.date_counts || {};
  const year = date.slice(0, 4), month = date.slice(5, 7), day = date.slice(8, 10);
  const crumbs = [];
  crumbs.push(date ? `<a href="#" style="color:var(--accent)" onclick="msgSetDate('');return false">All dates</a>` : '<b>All dates</b>');
  if (year) crumbs.push(month ? `<a href="#" style="color:var(--accent)" onclick="msgSetDate('${year}');return false">${year}</a>` : `<b>${year}</b>`);
  if (month) crumbs.push(day ? `<a href="#" style="color:var(--accent)" onclick="msgSetDate('${year}-${month}');return false">${MONTH_NAMES[+month - 1]}</a>` : `<b>${MONTH_NAMES[+month - 1]}</b>`);
  if (day) crumbs.push(`<b>${+day}</b>`);
  let keys = Object.keys(counts).sort();
  if (!year) keys.reverse();   // most recent year first; months and days in calendar order
  const chips = keys.map(k => {
    const next = !year ? k : !month ? `${year}-${k}` : `${year}-${month}-${k}`;
    const label = !year ? k : !month ? MONTH_NAMES[+k - 1] : +k;
    return `<button class="pbtn" onclick="msgSetDate('${next}')">${label} <span style="opacity:.7;font-weight:500">(${counts[k].toLocaleString()})</span></button>`;
  }).join('');
  row.style.display = 'flex';
  row.innerHTML = `
    <label style="font-size:13px;color:#64748b;white-space:nowrap;padding-top:6px">Date</label>
    <span style="font-size:13px;white-space:nowrap;padding-top:6px">${crumbs.join(' › ')}</span>
    <div style="display:flex;gap:6px;flex-wrap:wrap;flex:1">${chips || '<span class="empty" style="padding:6px 0">no messages at this level</span>'}</div>`;
}

async function msgFetch() {
  const account = msgFilters.account;
  const el = $('msgTypesResult');
  if (!el || !account) return;
  el.innerHTML = `<div class="empty" style="padding:18px">Loading message types for ${esc(account)}${msgFilters.contact ? ' with contact ' + esc(msgFilters.contact) : ''}…</div>`;
  const params = [];
  if (msgFilters.contact) params.push('contact=' + encodeURIComponent(msgFilters.contact));
  if (msgFilters.date) params.push('date=' + encodeURIComponent(msgFilters.date));
  const r = await api('messages/types/' + encodeURIComponent(account) + (params.length ? '?' + params.join('&') : ''));
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return; }
  const contacts = j.contacts || [];
  const contactRow = $('msgContactRow'), dateRow = $('msgDateRow');
  if (!contacts.length) {
    if (contactRow) contactRow.style.display = 'none';
    if (dateRow) dateRow.style.display = 'none';
    el.innerHTML = `<div class="empty" style="padding:18px">No messages stored for ${esc(account)}.</div>`;
    return;
  }
  // account found — show the filters and fill the contact options
  const sel = $('msgContactSelect');
  if (sel) {
    sel.innerHTML = ['<option value="">All contacts</option>']
      .concat(contacts.map(c => `<option value="${esc(c.name)}"${c.name === j.contact ? ' selected' : ''}>${esc(c.name)} (${c.count})</option>`))
      .join('');
  }
  if (contactRow) contactRow.style.display = 'flex';
  msgRenderDates(j);
  const byType = j.by_type || {};
  const types = Object.keys(byType);
  const filtered = j.contact || j.date;
  const scope = `${j.contact ? ` with <span class="mono">${esc(j.contact)}</span>` : ''}` +
                `${j.date ? ` in <span class="mono">${esc(msgDateLabel(j.date))}</span>` : ''}`;
  // Delete rules per content type: text/* only with a full day selected
  // (too easy to wipe whole conversations otherwise), file transfers
  // never (the File transfers tab handles those, files included),
  // anything else always.
  const daySelected = (j.date || '').length === 10;
  const deleteCell = t => {
    if (!j.can_delete) return '';
    if (t.startsWith('application/sylk-file-transfer'))
      return '<span style="font-size:11px;color:#94a3b8">use the File transfers tab</span>';
    if (t.startsWith('text/') && !daySelected)
      return '<span style="font-size:11px;color:#94a3b8">select a day to delete</span>';
    return `<button class="pbtn pbtn-kick" onclick="deleteMessageType('${encodeURIComponent(account)}','${encodeURIComponent(t)}',${byType[t]},this)">Delete</button>`;
  };
  const rows = types.length ? types.map(t => `
    <tr style="cursor:default">
      <td class="mono" style="font-size:13px;text-align:right;width:90px;padding:9px 16px 9px 0">${byType[t].toLocaleString()}</td>
      <td class="mono" style="font-size:13px;padding:9px 16px 9px 0">${esc(t)}</td>
      <td style="text-align:right;padding:9px 0">${deleteCell(t)}</td>
    </tr>`).join('')
    : `<tr><td colspan="3" class="empty">No messages match the current filters.</td></tr>`;
  el.innerHTML = `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <b style="font-size:14px">${Number(j.total).toLocaleString()} message(s) for <span class="mono">${esc(account)}</span>${scope}</b>
      ${j.oldest ? `<div style="font-size:12px;color:#64748b;margin-top:4px">oldest <span class="mono">${esc(j.oldest)}</span> · newest <span class="mono">${esc(j.newest)}</span></div>` : ''}
      <table style="margin-top:8px"><thead><tr>
        <th style="text-align:right;padding:6px 16px 4px 0">Count</th>
        <th style="padding:6px 16px 4px 0">Content type</th>
        <th style="padding:6px 0 4px"></th>
      </tr></thead><tbody>${rows}</tbody></table>
      ${j.can_delete ? (filtered ? '<div class="hint" style="padding:8px 0 0">delete removes only the messages matching the current filters</div>' : '') : '<div class="hint" style="padding:8px 0 0">deletion is only available on the Cassandra backend</div>'}
    </div>`;
}

async function deleteMessageType(encAccount, encType, count, btn) {
  const account = decodeURIComponent(encAccount);
  const type = decodeURIComponent(encType);
  const scope = (msgFilters.contact ? `\nContact: ${msgFilters.contact}` : '') +
                (msgFilters.date ? `\nDate: ${msgDateLabel(msgFilters.date)}` : '');
  if (!confirm(`Permanently delete ${count} ${type} message(s) for ${account}?${scope}\n\nThis cannot be undone.`)) return;
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  let j = null;
  try {
    const r = await api('messages/delete/' + encodeURIComponent(account), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content_type: type, contact: msgFilters.contact,
                             date: msgFilters.date }),
    });
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if (j && j.ok) toast(`Deleted ${j.deleted} ${type} message(s)`);
  else toast('Delete failed' + (j && j.error ? ': ' + j.error : ''));
  msgFetch();   // refresh the breakdown
}

async function purgeAccount(encAccount) {
  const account = decodeURIComponent(encAccount);
  const typed = prompt(
    `This will PERMANENTLY delete all server-side data of ${account}:\n\n` +
    `• all stored messages\n` +
    `• the account entry and its API token\n` +
    `• all push tokens\n` +
    `• the PGP public key\n` +
    `• all files shared by the account\n\n` +
    `This cannot be undone. Type the account address to confirm:`);
  if (typed === null) return;
  if (typed.trim().toLowerCase() !== account) {
    toast('Account address did not match — nothing was deleted');
    return;
  }
  toast('Purging ' + account + '…');
  let j = null;
  try {
    const r = await api('accounts/' + encAccount + '/purge', { method: 'POST' });
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if (j && j.ok) {
    const parts = [];
    if (j.messages != null && j.messages >= 0) parts.push(`${j.messages} messages`);
    if (j.push_tokens != null && j.push_tokens >= 0) parts.push(`${j.push_tokens} push tokens`);
    if (j.public_keys != null && j.public_keys >= 0) parts.push(`${j.public_keys} public keys`);
    parts.push(`${j.transfer_files || 0} shared files (${fmtBytes(j.transfer_bytes || 0)})`);
    toast(`Purged ${account}: ` + parts.join(', '));
  } else {
    toast('Purge failed' + (j && j.error ? ': ' + j.error : ''));
  }
  setTimeout(() => { $('acctInput').value = account; lookupAccount(); }, 1000);
}

async function deleteToken(encAccount, encApp, encDevice, btn) {
  const account = decodeURIComponent(encAccount);
  const app = decodeURIComponent(encApp);
  const device = decodeURIComponent(encDevice);
  if (!confirm(`Purge the push token for app ${app} on device ${device}?\n\n${account} will no longer receive push notifications on that device.`)) return;
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  let ok = false;
  try {
    const r = await api('tokens/' + encAccount + '/' + encApp + '/' + encDevice, { method: 'DELETE' });
    if (r.status === 403) { boot(); return; }
    ok = r.ok;
  } catch (e) {}
  toast(ok ? 'Push token purged' : 'Delete failed');
  // the purge runs async in the storage thread — give it a moment,
  // then refresh the account view
  setTimeout(() => { $('acctInput').value = account; lookupAccount(); }, 1000);
}

async function dumpMessage(e) {
  if (e) e.preventDefault();
  const account = $('dumpAccount').value.trim().toLowerCase();
  const id = $('dumpId').value.trim();
  if (!account || !id) { toast('Enter both account and message id'); return false; }
  $('dumpResult').innerHTML = `<div class="empty" style="padding:18px">Looking up message ${esc(id)}…</div>`;
  const r = await api('messages/dump/' + encodeURIComponent(account) + '/' + encodeURIComponent(id));
  if (r.status === 403) { boot(); return false; }
  const j = await r.json().catch(() => null);
  const el = $('dumpResult');
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return false; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return false; }
  if (!j.found) {
    el.innerHTML = `<div class="empty" style="padding:18px">No message with id <span class="mono">${esc(j.message_id)}</span> for ${esc(j.account)}.</div>`;
    return false;
  }
  el.innerHTML = (j.messages || []).map(msg => {
    const fields = [['timestamp', msg.timestamp || msg.created_at], ['stored at', msg.created_at],
                    ['direction', msg.direction], ['contact', msg.contact],
                    ['content type', msg.content_type], ['state', msg.state],
                    ['disposition', (msg.disposition || []).join(', ') || null]];
    const rows = fields.filter(f => f[1]).map(f => `<tr style="cursor:default;border:0">
        <td style="padding:3px 18px 3px 0;font-size:13px;color:#475569;white-space:nowrap">${f[0]}</td>
        <td class="mono" style="padding:3px 0;font-size:13px;word-break:break-all">${esc(f[1])}</td>
      </tr>`).join('');
    return `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <table style="width:auto"><tbody>${rows}</tbody></table>
      <div style="font-size:12px;color:#64748b;margin-top:10px">content</div>
      <pre style="font-size:12px;line-height:1.5;background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;margin:6px 0 0;white-space:pre-wrap;word-break:break-word">${esc(msg.content || '')}</pre>
    </div>`;
  }).join('') + (j.scanned ? '<div class="hint" style="padding:8px 0 0">found via account partition scan (id mapping expired)</div>' : '');
  return false;
}

async function countMessages() {
  const btn = $('msgBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Counting…'; }
  let j = {};
  try {
    const r = await api('storage/messages');
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if ($('msgCount')) $('msgCount').textContent = j.total != null ? Number(j.total).toLocaleString() : '—';
  if (btn) { btn.disabled = false; btn.textContent = 'Count again'; }
  if (j.error) toast('Count failed: ' + j.error);
  else if (j.elapsed != null) toast('Counted in ' + j.elapsed + 's');
}

async function loadMedia() {
  const grid = $('mediaGrid');
  if (!grid) return;
  const r = await api('media');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { grid.innerHTML = `<div class="card stat"><div class="err">Failed to load media usage.</div></div>`; return; }
  const defs = [
    ['file_transfers', 'File transfers'],
    ['filesharing', 'Conference files'],
    ['recordings', 'Recordings'],
  ];
  let total = 0, ok = false;
  grid.innerHTML = defs.map(([key, label]) => {
    const d = j[key] || {};
    if (d.error) return statTile(label, '—', [`<span class="err">${esc(d.error)}</span>`]);
    total += d.bytes || 0; ok = true;
    return statTile(label, fmtBytes(d.bytes), [
      d.files != null ? `${d.files} file${d.files === 1 ? '' : 's'} on disk` : null,
      d.path ? `<span class="mono" style="font-size:11px">${esc(d.path)}</span>` : null,
      d.note ? esc(d.note) : null,
    ]);
  }).join('');
  $('mediaTotal').textContent = ok ? fmtBytes(total) + ' total' : '—';
}

let ftFilters = { account: '', contact: '', type: '', sort: 'date', order: 'desc' };
const FT_TYPES = [['audio', 'Audio'], ['image', 'Images'], ['video', 'Video'], ['file', 'Files']];

async function loadFileTransfers(e) {
  if (e) e.preventDefault();
  const account = $('ftAcctInput').value.trim().toLowerCase();
  if (!account) return false;
  ftFilters = { account: account, contact: '', type: '', sort: 'date', order: 'desc' };
  const sel = $('ftContactSelect');
  if (sel) sel.innerHTML = '<option value="">All contacts</option>';
  const row = $('ftContactRow');
  if (row) row.style.display = 'none';
  ftFetch();
  return false;
}

function ftSetType(type) {
  ftFilters.type = type;
  ftFetch();
}

function ftSetContact(contact) {
  ftFilters.contact = contact;
  ftFetch();
}

function ftSetSort(sort) {
  ftFilters.sort = sort;
  ftFetch();
}

function ftToggleOrder() {
  ftFilters.order = ftFilters.order === 'asc' ? 'desc' : 'asc';
  ftFetch();
}

function ftCopyId(encoded) {
  const text = decodeURIComponent(encoded);
  const done = () => toast('Message id copied');
  const fail = () => toast('Copy failed');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => {
      ftCopyIdFallback(text) ? done() : fail();
    });
  } else {
    ftCopyIdFallback(text) ? done() : fail();
  }
}

function ftCopyIdFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
  return ok;
}

async function ftDeleteTransfer(encId, encContact, btn) {
  const id = decodeURIComponent(encId);
  if (!confirm('Delete this file transfer — database entry and files?\n\n' + id)) return;
  btn.disabled = true;
  const r = await api('media/file-transfers/' + encodeURIComponent(ftFilters.account) + '/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transfer_id: id, contact: decodeURIComponent(encContact) }),
  });
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({}));
  if (j.ok) {
    toast(`Deleted ${j.deleted_messages} message${j.deleted_messages === 1 ? '' : 's'}, ${j.deleted_dirs} folder${j.deleted_dirs === 1 ? '' : 's'} (${fmtBytes(j.bytes)})`);
    ftFetch();
  } else { btn.disabled = false; toast('Delete failed: ' + (j.error || r.status)); }
}

async function ftDeleteOrphanFiles(count, btn) {
  if (!confirm(`Delete ${count} file-transfer folder${count === 1 ? '' : 's'} from disk that have no database entry?`)) return;
  btn.disabled = true;
  const r = await api('media/file-transfers/' + encodeURIComponent(ftFilters.account) + '/delete-orphan-files', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contact: ftFilters.contact }),
  });
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({}));
  if (j.ok) { toast(`Deleted ${j.deleted} folder${j.deleted === 1 ? '' : 's'} (${fmtBytes(j.bytes)})`); ftFetch(); }
  else { btn.disabled = false; toast('Delete failed: ' + (j.error || r.status)); }
}

async function ftPurgeOrphaned(count, btn) {
  if (!confirm(`Delete ${count} file-transfer message${count === 1 ? '' : 's'} whose files are no longer on disk?`)) return;
  btn.disabled = true;
  const r = await api('media/file-transfers/' + encodeURIComponent(ftFilters.account) + '/purge-orphaned', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contact: ftFilters.contact, type: ftFilters.type }),
  });
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({}));
  if (j.ok) { toast(`Purged ${j.deleted} message${j.deleted === 1 ? '' : 's'}`); ftFetch(); }
  else { btn.disabled = false; toast('Purge failed: ' + (j.error || r.status)); }
}

async function ftFetch() {
  const account = ftFilters.account;
  const el = $('ftResult');
  if (!el || !account) return;
  el.innerHTML = `<div class="empty" style="padding:18px">Scanning file transfers for ${esc(account)}${ftFilters.contact ? ' to contact ' + esc(ftFilters.contact) : ''}…</div>`;
  const params = [];
  if (ftFilters.contact) params.push('contact=' + encodeURIComponent(ftFilters.contact));
  if (ftFilters.type) params.push('type=' + encodeURIComponent(ftFilters.type));
  if (ftFilters.sort !== 'date') params.push('sort=' + encodeURIComponent(ftFilters.sort));
  if (ftFilters.order !== 'desc') params.push('order=' + encodeURIComponent(ftFilters.order));
  const r = await api('media/file-transfers/' + encodeURIComponent(account) + (params.length ? '?' + params.join('&') : ''));
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return; }
  const contacts = j.contacts || [];
  const row = $('ftContactRow');
  if (!contacts.length && !j.files) {
    if (row) row.style.display = 'none';
    el.innerHTML = `<div class="empty" style="padding:18px">No file-transfer messages in the database for ${esc(account)}.</div>`;
    return;
  }
  // account found — show the contact filter and fill its options
  const sel = $('ftContactSelect');
  if (sel) {
    sel.innerHTML = ['<option value="">All contacts</option>']
      .concat(contacts.map(c => `<option value="${esc(c.name)}"${c.name === j.contact ? ' selected' : ''}>${esc(c.name)} (${c.count})</option>`))
      .join('');
  }
  if (row) row.style.display = 'flex';
  // Drill-down chips: transfer counts by media type, scoped to the
  // selected contact (or the whole account when no contact is chosen).
  const byType = j.by_type || {};
  const allCount = FT_TYPES.reduce((s, t) => s + ((byType[t[0]] || {}).count || 0), 0);
  const allBytes = FT_TYPES.reduce((s, t) => s + ((byType[t[0]] || {}).bytes || 0), 0);
  const chips = [['', 'All', { count: allCount, bytes: allBytes }]]
    .concat(FT_TYPES.map(([key, label]) => [key, label, byType[key] || { count: 0, bytes: 0 }]))
    .concat([['missing', 'Missing file', byType.missing || { count: 0, bytes: 0 }],
             ['nodb', 'Missing database', byType.nodb || { count: 0, bytes: 0 }]])
    .map(([key, label, d]) => {
      const active = (j.type || '') === key;
      const style = active ? ' style="background:var(--accent);border-color:var(--accent);color:#fff"' : '';
      const sub = d.count ? `(${d.count} · ${fmtBytes(d.bytes)})` : '(0)';
      return `<button class="pbtn" onclick="ftSetType('${key}')"${style}>${label} <span style="opacity:.7;font-weight:500">${sub}</span></button>`;
    }).join('');
  const shown = (j.transfers || []).length;
  const typeLabel = j.type === 'missing' ? 'missing-file'
    : (FT_TYPES.find(t => t[0] === j.type) || [null, 'matching'])[1].toLowerCase();
  // a selected media category is implicit — hide the column (but keep
  // it for missing/nodb, where the media type still varies); same for
  // a selected contact
  const showType = !j.type || j.type === 'missing' || j.type === 'nodb';
  const showContact = !j.contact;
  const cols = 6 + (showType ? 1 : 0) + (showContact ? 1 : 0);
  // orphaned count within the current drill-down scope
  const orphaned = j.type === 'missing'
    ? ((byType.missing || {}).count || 0)
    : j.type
    ? ((byType[j.type] || {}).count || 0) - ((byType[j.type] || {}).on_disk || 0)
    : j.files - j.on_disk;
  const nodbCount = (byType.nodb || {}).count || 0;
  const rows = shown ? (j.transfers || []).map(t => `
    <tr style="cursor:default">
      <td class="mono" style="font-size:12px;color:#64748b;white-space:nowrap;padding:8px 16px 8px 0">${esc(t.date)}</td>
      <td class="mono" style="font-size:11px;color:#94a3b8;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:8px 16px 8px 0;cursor:pointer" title="${esc(t.transfer_id)} — click to copy" onclick="ftCopyId('${encodeURIComponent(t.transfer_id || '')}')">${esc(t.transfer_id)}</td>
      ${showType ? `<td style="font-size:12px;color:#64748b;padding:8px 16px 8px 0">${esc(t.type)}</td>` : ''}
      ${showContact ? `<td class="mono" style="font-size:12px;padding:8px 16px 8px 0${(t.contact || '').includes('@') ? '' : ';color:#94a3b8;font-style:italic'}" title="${t.direction === 'outgoing' ? 'sent to' : t.direction === 'incoming' ? 'received from' : ''}">${t.direction === 'outgoing' ? '→' : t.direction === 'incoming' ? '←' : ''} ${esc(t.contact)}</td>` : ''}
      <td class="mono" style="font-size:12px;padding:8px 16px 8px 0;word-break:break-all${t.filename ? '' : ';color:#94a3b8;font-style:italic'}">${esc(t.filename || 'unknown (encrypted metadata)')}</td>
      <td class="mono" style="font-size:12px;text-align:right;white-space:nowrap;padding:8px 16px 8px 0">${fmtBytes(t.bytes)}</td>
      <td style="font-size:12px;white-space:nowrap;padding:8px 16px 8px 0">${t.in_db === false ? `<span style="color:#d97706">✓ ${fmtBytes(t.disk_bytes)} · no db entry</span>` : t.on_disk ? `<span style="color:var(--ok)">✓ ${fmtBytes(t.disk_bytes)}</span>` : '<span style="color:#dc2626">missing</span>'}</td>
      <td style="padding:8px 0;text-align:right"><button class="pbtn pbtn-kick" style="padding:3px 10px;font-size:12px"
          title="Delete this transfer: the database entry and its files on disk"
          onclick="ftDeleteTransfer('${encodeURIComponent(t.transfer_id || '')}','${encodeURIComponent(t.contact || '')}', this)">Delete</button></td>
    </tr>`).join('')
    : `<tr><td colspan="${cols}" class="empty">${j.type === 'nodb' ? 'No files without a database entry' : `No ${esc(j.type ? typeLabel : '')} transfers`}${j.contact ? ` for <span class="mono">${esc(j.contact)}</span>` : ''}.</td></tr>`;
  el.innerHTML = `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <b style="font-size:14px">${j.files} transfer${j.files === 1 ? '' : 's'} (${fmtBytes(j.bytes)}) in the database for <span class="mono">${esc(j.account)}</span>${j.contact ? ` → <span class="mono">${esc(j.contact)}</span>` : ''}</b>
      <div style="font-size:12px;color:#64748b;margin-top:4px">${j.on_disk} still on disk (${fmtBytes(j.disk_bytes)})${j.files > j.on_disk ? `, ${j.files - j.on_disk} expired or removed` : ''}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px">
        ${chips}
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;padding:8px 0;border-top:1px solid var(--border)">
        ${j.type === 'nodb'
          ? `<button class="pbtn pbtn-kick" ${nodbCount ? '' : 'disabled'} onclick="ftDeleteOrphanFiles(${nodbCount}, this)"
                title="Delete the files on disk that have no file-transfer message in the database">Delete file${nodbCount === 1 ? '' : 's'} without database entry${nodbCount ? ` (${nodbCount})` : ''}</button>`
          : `<button class="pbtn pbtn-kick" ${orphaned ? '' : 'disabled'} onclick="ftPurgeOrphaned(${orphaned}, this)"
                title="Delete the file-transfer messages in the current view whose files are no longer on disk">Fix database for missing file${orphaned === 1 ? '' : 's'}${orphaned ? ` (${orphaned})` : ''}</button>`}
        <span style="flex:1"></span>
        <label style="font-size:12px;color:#64748b">Sort by</label>
        <select onchange="ftSetSort(this.value)" style="padding:6px 10px;border:1px solid var(--border);border-radius:7px;font-size:13px;background:#fff">
          <option value="date"${j.sort === 'date' ? ' selected' : ''}>Date</option>
          <option value="size"${j.sort === 'size' ? ' selected' : ''}>Size</option>
          <option value="name"${j.sort === 'name' ? ' selected' : ''}>Name</option>
        </select>
        <button class="pbtn" onclick="ftToggleOrder()" title="toggle sort order">${j.order === 'asc' ? '↑ ASC' : '↓ DESC'}</button>
      </div>
      ${j.total_transfers > shown ? `<div style="font-size:12px;color:#64748b;margin-top:4px">showing ${shown} of ${j.total_transfers} transfers</div>` : ''}
      <table style="margin-top:8px"><thead><tr>
        <th style="padding:6px 16px 4px 0">Date</th>
        <th style="padding:6px 16px 4px 0">Message id</th>
        ${showType ? '<th style="padding:6px 16px 4px 0">Type</th>' : ''}
        <th style="padding:6px 16px 4px 0">Contact</th>
        <th style="padding:6px 16px 4px 0">File</th>
        <th style="padding:6px 16px 4px 0;text-align:right">Size</th>
        <th style="padding:6px 16px 4px 0">On disk</th>
        <th style="padding:6px 0 4px"></th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
}

let currentRoomUri = null;

async function openRoom(encUri, displayUri) {
  currentRoom = encUri;
  currentRoomUri = displayUri;
  $('d-uri').textContent = displayUri;
  $('d-sub').textContent = 'Loading participants…';
  $('d-body').innerHTML = '';
  meterEls = {}; lastPartSig = null;
  $('overlay').classList.add('open');
  await renderParticipants();
}

let meterEls = {};   // audio_pid -> { fill, num, row }
let pidByTarget = {}; // for reference
let lastPartSig = null;

async function renderParticipants() {
  const encUri = currentRoom;
  const r = await api('rooms/' + encUri + '/participants');
  if (r.status === 403) { closeDrawer(); boot(); return; }
  if (r.status === 404) { $('d-sub').textContent = 'Room no longer exists.'; $('d-body').innerHTML = ''; lastPartSig = null; return; }
  const j = await r.json().catch(() => ({ participants: [] }));
  const ps = j.participants || [];
  $('d-sub').textContent = 'Janus room ' + (j.janus_room_id != null ? j.janus_room_id : '?') +
                           ' · ' + ps.length + (ps.length === 1 ? ' participant' : ' participants');
  // Only rebuild the DOM when the roster actually changes — otherwise the
  // 7s refresh would flash the table and reset the live meters. The audio
  // loop updates meters in place between rebuilds.
  const sig = JSON.stringify(ps.map(p => [p.kind, p.target_id, p.muted, p.display_name, p.audio_pid]));
  if (sig === lastPartSig && Object.keys(meterEls).length) return;
  lastPartSig = sig;

  if (!ps.length) {
    $('d-body').innerHTML = `<div class="empty">No participants in this conference.</div>`;
    meterEls = {};
    return;
  }
  $('d-body').innerHTML = `
    <table>
      <thead><tr>
        <th>Participant</th><th class="lvl-cell">Audio level</th><th style="text-align:right">Actions</th>
      </tr></thead>
      <tbody>
        ${ps.map((p, i) => {
          const tid = encodeURIComponent(p.target_id == null ? '' : p.target_id);
          const muteLabel = p.muted === true ? 'Unmute' : 'Mute';
          const muteVal = p.muted === true ? 'false' : 'true';
          const isBridge = p.kind === 'bridge';
          const badge = p.kind === 'sip'
            ? '<span class="badge badge-sip">SIP</span>'
            : p.kind === 'bridge'
            ? '<span class="badge badge-bridge">Bridge</span>'
            : '<span class="badge badge-webrtc">WebRTC</span>';
          const mutedBadge = p.muted === true ? '<span class="badge badge-muted">Muted</span>' : '';
          const srcTitle = p.kind === 'sip' ? 'mic level from conference focus (0–255)' : 'speaker activity from Janus (talking + dBov)';
          const uri = stripSip(p.uri);
          const name = p.display_name || uri || 'unknown';
          const slow = (p.slow_download ? ' <span style="color:#dc2626;font-size:11px">↓slow</span>' : '')
                     + (p.slow_upload ? ' <span style="color:#dc2626;font-size:11px">↑slow</span>' : '');
          const ua = p.user_agent ? `<div style="font-size:11px;color:#94a3b8;margin-top:1px">${esc(p.user_agent)}${slow}</div>` : (slow ? `<div style="margin-top:1px">${slow}</div>` : '');
          return `
          <tr style="cursor:default" data-row="${i}">
            <td>
              <div style="font-weight:600">${esc(name)} ${badge}${mutedBadge}<span class="spkdot" data-dot="${i}"></span></div>
              <div class="mono" style="font-size:12px;color:#64748b">${esc(uri)}</div>
              ${ua}
            </td>
            <td>
              ${isBridge ? '<span style="font-size:12px;color:#94a3b8">audio mixer</span>'
                         : `<div class="meter" title="${srcTitle}"><i data-fill="${i}"></i></div>
              <div class="lvl-num" data-num="${i}">—</div>`}
            </td>
            <td>
              ${isBridge ? '' : `<div class="actions">
                <button class="pbtn" onclick="muteParticipant('${tid}', ${muteVal}, this)">${muteLabel}</button>
                <button class="pbtn pbtn-kick" onclick="kickParticipant('${tid}','${esc(name)}', this)">Kick</button>
              </div>`}
            </td>
          </tr>`; }).join('')}
      </tbody>
    </table>
    <div class="hint">Audio meter: SIP callers show a continuous mic level from the conference focus; WebRTC participants show speaker activity from Janus (talking + dBov), which updates on talking transitions rather than continuously. WebRTC mute asks the client to mute at source; SIP mute/kick is proxied to the focus.</div>`;
  // Index meter elements by target_id (the key the audio-levels endpoint
  // returns) for the live-update loop.
  meterEls = {};
  ps.forEach((p, i) => {
    if (p.target_id == null) return;
    meterEls[String(p.target_id)] = {
      fill: $('d-body').querySelector(`[data-fill="${i}"]`),
      num: $('d-body').querySelector(`[data-num="${i}"]`),
      row: $('d-body').querySelector(`[data-row="${i}"]`),
      dot: $('d-body').querySelector(`[data-dot="${i}"]`),
    };
  });
}

async function updateAudioLevels() {
  if (!currentRoom || !$('overlay').classList.contains('open')) return;
  if (!Object.keys(meterEls).length) return;
  let j;
  try {
    const r = await api('rooms/' + currentRoom + '/audio-levels');
    if (!r.ok) return;
    j = await r.json();
  } catch (e) { return; }
  const scale = j.scale || 255;
  const levels = j.levels || {};
  for (const tid in meterEls) {
    const el = meterEls[tid];
    if (!el.fill) continue;
    const v = levels[tid] || {};
    const value = v.value || 0;
    const stale = !!v.stale;
    const talking = !!v.talking;
    const pct = Math.min(100, Math.round((value / scale) * 100));
    el.fill.style.width = pct + '%';
    el.fill.parentElement.classList.toggle('stale', stale);
    if (el.num) el.num.textContent = stale ? '—' : String(value);
    if (el.row) el.row.classList.toggle('speaking', talking);
  }
}

async function muteParticipant(tid, muted, btn) {
  btn.disabled = true;
  const r = await api('rooms/' + currentRoom + '/participants/' + tid + '/mute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ muted: muted }),
  });
  if (r.ok) { toast(muted ? 'Mute sent' : 'Unmute sent'); setTimeout(renderParticipants, 1200); }
  else { btn.disabled = false; toast('Action failed (' + r.status + ')'); }
}

async function kickParticipant(tid, label, btn) {
  if (!confirm('Remove ' + label + ' from this conference?')) return;
  btn.disabled = true;
  const r = await api('rooms/' + currentRoom + '/participants/' + tid + '/kick', { method: 'POST' });
  if (r.ok) { toast('Kick sent'); setTimeout(renderParticipants, 1200); }
  else { btn.disabled = false; toast('Action failed (' + r.status + ')'); }
}

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

function closeDrawer() { $('overlay').classList.remove('open'); currentRoom = null; currentRoomUri = null; meterEls = {}; lastPartSig = null; }
$('overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') closeDrawer(); });

/* ---------- live updates ---------- */
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; loadRooms(); }, 400);
}
function startLive() {
  stopLive();
  try {
    evtSource = new EventSource('rooms/events');
    evtSource.onmessage = () => scheduleRefresh();   // room created/destroyed
    evtSource.onerror = () => {};                     // browser auto-reconnects
  } catch (e) {}
  // Poll for room/session/end-point changes (none of these emit SSE
  // events). Only the visible section is refreshed; switching tabs
  // triggers an immediate load. The Storage tab is intentionally not
  // polled — its backend scans are heavier, so it loads on demand.
  pollTimer = setInterval(() => {
    if (currentView === 'endpoints') loadEndpoints();
    else if (currentView === 'sessions') loadSessions();
    else if (currentView === 'conferences') loadRooms();
    if (currentRoom) renderParticipants();
  }, 7000);
  // Fast loop for live audio meters while a room panel is open.
  audioTimer = setInterval(updateAudioLevels, 350);
}
function stopLive() {
  if (evtSource) { evtSource.close(); evtSource = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (audioTimer) { clearInterval(audioTimer); audioTimer = null; }
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
}

async function logout() {
  await api('logout', { method: 'POST' });
  closeDrawer();
  boot();
}

/* ---------- bootstrap ---------- */
async function boot() {
  const r = await api('session');
  const j = await r.json().catch(() => ({ authenticated: false }));
  if (j.authenticated) showDashboard(j.username);
  else showLogin(j.login_configured !== false);
}
boot();
</script>
</body>
</html>
"""


# TODO: This implementation is a prototype.  Moving forward it probably makes sense to provide admin API
# capabilities for other applications too.  This could be done in a number of ways:
#
# * On the main web server, under a /admin/ parent route.
# * On a separate web server, which could listen on a different IP and port.
#
# In either case, HTTPS aside, a token based authentication mechanism would be desired.
# Which one is best is not 100% clear at this point.

class AuthError(Exception): pass


@implementer(IObserver)
class AdminWebHandler(object, metaclass=Singleton):
    app = Klein()

    def __init__(self):
        self.listener = None
        # Klein resource served at /webrtcgateway/admin on the built-in
        # web server when http(s)_management_interface is set to
        # 'builtin_webserver'. None means the mount is inactive (the
        # public /admin route answers 404).
        self.builtin_resource = None
        # Active /rooms/events SSE clients (Twisted Request objects).
        self._event_subscribers = set()
        # Valid browser login sessions: token -> {'username', 'created'}.
        # Populated by /login, consumed by _check_auth, cleared by /logout.
        self._auth_tokens = {}
        # All active listeners (plain HTTP + optional HTTPS), closed in stop().
        self._listeners = []
        # Subscribe to videoroom lifecycle notifications fired by
        # VideoroomContainer.add / .remove / .clear in factory.py.
        nc = NotificationCenter()
        nc.add_observer(self, name='VideoroomCreated')
        nc.add_observer(self, name='VideoroomDestroyed')

    def handle_notification(self, notification):
        kind = {
            'VideoroomCreated': 'room-created',
            'VideoroomDestroyed': 'room-destroyed',
        }.get(notification.name)
        if kind is None:
            return
        payload = {
            'type': kind,
            'uri': notification.data.uri,
            'janus_room_id': notification.data.janus_room_id,
        }
        line = ('data: ' + json.dumps(payload) + '\n\n').encode('utf-8')
        for req in list(self._event_subscribers):
            try:
                req.write(line)
            except Exception:
                self._event_subscribers.discard(req)

    def start(self):
        site = Site(self.app.resource())
        site.noisy = False
        self._listeners = []

        http_iface = GeneralConfig.http_management_interface
        https_iface = GeneralConfig.https_management_interface

        # 'builtin_webserver' keyword: mount the admin API on the main
        # SylkServer web server at /webrtcgateway/admin instead of (or in
        # addition to) running standalone listeners. The main server
        # decides the scheme (HTTPS when it has a certificate, HTTP
        # otherwise), so the keyword means the same thing on either
        # setting. The built-in web server is typically public, so the
        # mount is refused unless authentication is configured.
        if BUILTIN_WEBSERVER in (http_iface, https_iface):
            auth_configured = bool(GeneralConfig.http_management_auth_secret
                                   or (GeneralConfig.http_management_admin_username
                                       and GeneralConfig.http_management_admin_password))
            if auth_configured:
                self.builtin_resource = self.app.resource()
                log.info('Admin web handler mounted on the built-in web server at %s/webrtcgateway/admin' % server.url)
            else:
                log.error('Admin web: refusing to mount the admin API on the '
                          'built-in web server without authentication; set '
                          'http_management_auth_secret and/or '
                          'http_management_admin_username/password')

        # Plain HTTP listener — internal tooling (sip-janus-bridge, the
        # audio bridge's /rooms/events SSE, etc.).
        if http_iface and http_iface != BUILTIN_WEBSERVER:
            host, port = http_iface
            # noinspection PyUnresolvedReferences
            self.listener = reactor.listenTCP(port, site, interface=host)
            self._listeners.append(self.listener)
            log.info('Admin web handler started at http://%s:%d' % (host, port))

        # Optional HTTPS listener — browser admin UI over the internet,
        # using the same certificate as the main web/WebSocket server.
        if https_iface and https_iface != BUILTIN_WEBSERVER:
            self._start_https(site, https_iface)

    def _start_https(self, site, https_iface):
        cert_path = WebServerConfig.certificate.normalized if WebServerConfig.certificate else None
        if cert_path is None:
            log.warning('Admin web: https_management_interface is set but no TLS '
                        'certificate is configured (WebServerConfig.certificate); '
                        'HTTPS listener not started')
            return
        if not os.path.isfile(cert_path):
            log.error('Admin web: certificate file %s could not be found; '
                      'HTTPS listener not started' % cert_path)
            return
        try:
            ssl_ctx_factory = DefaultOpenSSLContextFactory(cert_path, cert_path)
            cert_chain_path = (WebServerConfig.certificate_chain.normalized
                               if WebServerConfig.certificate_chain else None)
            if cert_chain_path is not None and os.path.isfile(cert_chain_path):
                ssl_ctx_factory.getContext().use_certificate_chain_file(cert_chain_path)
            shost, sport = https_iface
            # noinspection PyUnresolvedReferences
            listener = reactor.listenSSL(sport, site, ssl_ctx_factory, interface=shost)
            self._listeners.append(listener)
            log.info('Admin web handler started at https://%s:%d' % (shost, sport))
        except Exception:
            log.exception('Admin web: failed to start HTTPS listener')

    def stop(self):
        for listener in self._listeners:
            try:
                listener.stopListening()
            except Exception:
                pass
        self._listeners = []
        self.listener = None
        self.builtin_resource = None

    # Admin web API

    SESSION_COOKIE = b'sylk_admin_session'

    def _has_valid_session(self, request):
        token = request.getCookie(self.SESSION_COOKIE)
        if not token:
            return False
        if isinstance(token, bytes):
            token = token.decode()
        return token in self._auth_tokens

    def _check_auth(self, request):
        # A valid browser login session always passes.
        if self._has_valid_session(request):
            return
        # Shared-secret header (back-compat for sip-janus-bridge & tooling).
        auth_secret = GeneralConfig.http_management_auth_secret
        if auth_secret:
            auth_headers = request.requestHeaders.getRawHeaders('Authorization', default=None)
            if auth_headers and auth_headers[0] == auth_secret:
                return
        # Enforce auth whenever either auth method is configured. If neither
        # the shared secret nor admin credentials are set, preserve the
        # historical open-access behaviour of the management interface.
        creds_configured = bool(GeneralConfig.http_management_admin_username
                                and GeneralConfig.http_management_admin_password)
        if auth_secret or creds_configured:
            raise AuthError()

    @app.handle_errors(AuthError)
    def auth_error(self, request, failure):
        request.setResponseCode(403)
        return 'Authentication error'

    # ------------------------------------------------------------------
    # Browser login — username/password issues a session cookie that
    # _check_auth honours. Credentials come from
    # http_management_admin_username / http_management_admin_password.
    # ------------------------------------------------------------------

    @app.route('/login', methods=['POST'])
    def login(self, request):
        request.setHeader('Content-Type', 'application/json')
        cfg_user = GeneralConfig.http_management_admin_username
        cfg_pass = GeneralConfig.http_management_admin_password
        if not cfg_user or not cfg_pass:
            request.setResponseCode(503)
            return json.dumps({'ok': False, 'error': 'admin login not configured'})

        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        username = (payload.get('username') or '').strip()
        password = payload.get('password') or ''

        # constant-time comparison to avoid leaking length/content timing
        ok = (hmac.compare_digest(username, cfg_user)
              and hmac.compare_digest(password, cfg_pass))
        if not ok:
            request.setResponseCode(401)
            return json.dumps({'ok': False, 'error': 'invalid credentials'})

        token = secrets.token_urlsafe(32)
        self._auth_tokens[token] = {'username': username, 'created': time.time()}
        # Mark the cookie Secure only when the login itself came over HTTPS,
        # so a Secure cookie isn't set (and then dropped) over plain HTTP.
        request.addCookie(self.SESSION_COOKIE, token.encode(), path=b'/',
                          httpOnly=True, sameSite='Strict', secure=bool(request.isSecure()))
        return json.dumps({'ok': True, 'username': username})

    @app.route('/logout', methods=['POST'])
    def logout(self, request):
        request.setHeader('Content-Type', 'application/json')
        token = request.getCookie(self.SESSION_COOKIE)
        if token:
            if isinstance(token, bytes):
                token = token.decode()
            self._auth_tokens.pop(token, None)
        request.addCookie(self.SESSION_COOKIE, b'', path=b'/', max_age=b'0')
        return json.dumps({'ok': True})

    @app.route('/session', methods=['GET'])
    def session_info(self, request):
        request.setHeader('Content-Type', 'application/json')
        if self._has_valid_session(request):
            token = request.getCookie(self.SESSION_COOKIE)
            if isinstance(token, bytes):
                token = token.decode()
            return json.dumps({'authenticated': True,
                               'username': self._auth_tokens[token]['username']})
        # Tell the UI whether a login form is usable at all.
        configured = bool(GeneralConfig.http_management_admin_username
                          and GeneralConfig.http_management_admin_password)
        return json.dumps({'authenticated': False, 'login_configured': configured})

    @app.route('/', methods=['GET'])
    def index(self, request):
        request.setHeader('Content-Type', 'text/html; charset=utf-8')
        return ADMIN_UI_HTML.encode('utf-8')

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

    @app.route('/tokens/<string:account>/<string:app_id>/<string:device_id>', methods=['DELETE'])
    def delete_token(self, request, account, app_id, device_id):
        """Purge one push token (identified by app id + device id, the
        primary key columns) from the token store. Used by the Delete
        buttons in the admin UI's account view."""
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        log.info('[admin] delete push token requested for {} (app={}, device={})'.format(
            account, app_id, device_id))
        storage = TokenStorage()
        storage.remove(account, app_id, device_id)   # runs async in the storage thread
        return json.dumps({'ok': True, 'queued': True})

    # ------------------------------------------------------------------
    # End-points — all active WebSocket connections to the gateway.
    # One entry per connection; each connection lists the accounts added
    # on it (usually one). Consumed by the admin UI's End-points section.
    # ------------------------------------------------------------------

    @staticmethod
    def _peer_address(connection):
        """Split an autobahn peer string ('tcp4:1.2.3.4:56789',
        'tcp6:2001:db8::1:56789', ...) into (host, port)."""
        peer = getattr(connection, 'peer', '') or ''
        if peer.startswith(('tcp4:', 'tcp6:', 'unix:')):
            peer = peer.split(':', 1)[1]
        host, sep, port = peer.rpartition(':')
        if not sep:
            return peer, None
        return host, port

    @app.route('/endpoints', methods=['GET'])
    def list_endpoints(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        endpoints = []
        for connection in list(SylkWebSocketServerFactory.connections):
            handler = connection.connection_handler
            host, port = self._peer_address(connection)
            accounts = []
            if handler is not None:
                for account in list(handler.accounts_map.values()):
                    accounts.append({
                        'uri': account.id,
                        'display_name': account.display_name,
                        'user_agent': account.user_agent,
                        'registration_state': account.registration_state,
                    })
            endpoints.append({
                'ip': host,
                'port': port,
                'address': '{}:{}'.format(host, port) if port else host,
                'device_id': getattr(handler, 'device_id', None),
                'state': getattr(handler, 'state', None),
                'accounts': accounts,
            })
        endpoints.sort(key=lambda e: ((e['accounts'][0]['uri'] or '~') if e['accounts'] else '~', e['address']))
        return json.dumps({'total': len(endpoints), 'endpoints': endpoints})

    # ------------------------------------------------------------------
    # Sessions — real-time one-to-one (SIP) sessions across all connected
    # end-points. Consumed by the admin UI's Sessions section.
    # ------------------------------------------------------------------

    @app.route('/sessions', methods=['GET'])
    def list_sessions(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        now = time.time()
        sessions = []
        for connection in list(SylkWebSocketServerFactory.connections):
            handler = connection.connection_handler
            if handler is None:
                continue
            host, port = self._peer_address(connection)
            address = '{}:{}'.format(host, port) if port else host
            for session in list(handler.sip_sessions):
                account = getattr(session, 'account', None)
                local = getattr(session, 'local_identity', None)
                remote = getattr(session, 'remote_identity', None)
                created = getattr(session, 'created', None)
                sessions.append({
                    'id': session.id,
                    'direction': session.direction,
                    'state': session.state,
                    'account': getattr(account, 'id', None),
                    'display_name': getattr(account, 'display_name', None),
                    'user_agent': getattr(account, 'user_agent', None),
                    'local_uri': getattr(local, 'uri', None),
                    'remote_uri': getattr(remote, 'uri', None),
                    'remote_display_name': getattr(remote, 'display_name', None),
                    'call_id': getattr(session, 'call_id', None),
                    'media': list(getattr(session, 'media', None) or []),
                    'media_ports': dict(getattr(session, 'media_ports', None) or {}),
                    'duration': int(now - created) if created else None,
                    'address': address,
                    'slow_download': bool(getattr(session, 'slow_download', False)),
                    'slow_upload': bool(getattr(session, 'slow_upload', False)),
                })
        sessions.sort(key=lambda s: (-(s['duration'] or 0), s['account'] or '~'))
        return json.dumps({'total': len(sessions), 'sessions': sessions})

    # ------------------------------------------------------------------
    # Accounts — usage overview: account / push-token / public-key
    # statistics from the storage backend (Cassandra when configured,
    # otherwise the file backend). The Cassandra numbers come from
    # full-partition scans of the small tables (chat_accounts,
    # push_tokens, public_key_by_account) — fine on demand, which is why
    # the UI only loads this tab when opened. The message table is only
    # counted via the separate /storage/messages endpoint (it can be
    # huge and rows expire via their one-year TTL).
    # ------------------------------------------------------------------

    _du_cache = {}      # path -> (timestamp, result), cached for 120 s
    DU_CACHE_TTL = 120

    def _dir_usage(self, path):
        """Deferred -> {'path', 'bytes', 'files'}, walked in a thread."""
        now = time.time()
        ts, cached = self._du_cache.get(path, (0, None))
        if cached is not None and now - ts < self.DU_CACHE_TTL:
            return defer.succeed(cached)

        def walk():
            total, files = 0, 0
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, filename))
                        files += 1
                    except OSError:
                        pass
            return {'path': path, 'bytes': total, 'files': files}

        d = threads.deferToThread(walk)

        def cache(result):
            self._du_cache[path] = (time.time(), result)
            return result
        d.addCallback(cache)
        return d

    @staticmethod
    def _cassandra_usage():
        """Deferred -> stats dict, gathered on the cassandra thread."""
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_stats():
            from .models.storage.cassandra import (ChatAccount, PublicKey,
                                                   PushTokens)
            stats = {}
            now = datetime.datetime.utcnow()
            try:
                total = with_token = active_7d = active_30d = 0
                for acc in ChatAccount.objects.all():
                    total += 1
                    if acc.api_token:
                        with_token += 1
                    if acc.last_login is not None:
                        age = (now - acc.last_login).days
                        if age <= 7:
                            active_7d += 1
                        if age <= 30:
                            active_30d += 1
                stats['accounts'] = {'total': total, 'with_api_token': with_token,
                                     'active_7d': active_7d, 'active_30d': active_30d}
            except Exception as e:
                stats['accounts'] = {'error': str(e)}
            try:
                total, platforms, accounts = 0, {}, set()
                for token in PushTokens.objects.all():
                    total += 1
                    platform = token.platform or 'unknown'
                    platforms[platform] = platforms.get(platform, 0) + 1
                    accounts.add('{}@{}'.format(token.username, token.domain))
                stats['push_tokens'] = {'total': total, 'accounts': len(accounts),
                                        'platforms': platforms}
            except Exception as e:
                stats['push_tokens'] = {'error': str(e)}
            try:
                stats['public_keys'] = {'total': PublicKey.objects.count()}
            except Exception as e:
                stats['public_keys'] = {'error': str(e)}
            reactor.callFromThread(deferred.callback, stats)

        query_stats()
        return deferred

    @staticmethod
    def _file_backend_usage():
        """Stats dict for the pickle/json file backend."""
        stats = {}
        try:
            tokens = getattr(TokenStorage(), '_tokens', {}) or {}
            total = sum(len(devices) for devices in tokens.values())
            platforms = {}
            for devices in tokens.values():
                for device in devices.values():
                    platform = (device.get('platform') if isinstance(device, dict) else None) or 'unknown'
                    platforms[platform] = platforms.get(platform, 0) + 1
            stats['push_tokens'] = {'total': total, 'accounts': len(tokens),
                                    'platforms': platforms}
        except Exception as e:
            stats['push_tokens'] = {'error': str(e)}
        conversations_dir = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations')
        try:
            with open(os.path.join(conversations_dir, 'accounts.json')) as f:
                stats['accounts'] = {'total': len(json.load(f))}
        except (OSError, IOError, ValueError):
            stats['accounts'] = {'total': 0}
        try:
            with open(os.path.join(conversations_dir, 'public_keys.json')) as f:
                stats['public_keys'] = {'total': len(json.load(f))}
        except (OSError, IOError, ValueError):
            stats['public_keys'] = {'total': 0}
        return stats

    @app.route('/storage', methods=['GET'])
    def storage_info(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        result = {
            'backend': 'cassandra' if use_cassandra else 'file',
            'cassandra': ({'contact_points': list(CassandraConfig.cluster_contact_points),
                           'keyspace': CassandraConfig.keyspace,
                           'push_tokens_table': CassandraConfig.push_tokens_table or 'push_tokens'}
                          if use_cassandra else None),
            'storage_dir': FileStorageConfig.storage_dir.normalized,
            'messages_note': 'chat messages expire after one year (table TTL); not counted here',
        }
        stats_d = self._cassandra_usage() if use_cassandra else defer.succeed(self._file_backend_usage())

        def assemble(st_value):
            result.update(st_value)
            return json.dumps(result)

        def failed(failure):
            result['error'] = str(failure.value)
            return json.dumps(result)
        stats_d.addCallbacks(assemble, failed)
        return stats_d

    # ------------------------------------------------------------------
    # Media — disk usage of the media stores, one entry per directory:
    # one-to-one file transfers, per-room conference shared files and
    # conference recordings. Sizes are walked in a thread and cached for
    # DU_CACHE_TTL seconds.
    # ------------------------------------------------------------------

    @app.route('/media', methods=['GET'])
    def media_info(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        dirs = [
            ('file_transfers', GeneralConfig.file_transfer_dir.normalized,
             'one-to-one file transfers'),
            ('filesharing', GeneralConfig.filesharing_dir.normalized,
             'conference shared files (kept per active room, removed when the room closes)'),
            ('recordings', GeneralConfig.recording_dir.normalized,
             'conference recordings'),
        ]
        dl = defer.DeferredList([self._dir_usage(path) for _, path, _ in dirs],
                                consumeErrors=True)

        def assemble(results):
            out = {}
            for (key, path, note), (ok, value) in zip(dirs, results):
                entry = dict(value) if ok else {'path': path, 'error': str(value.value)}
                entry['note'] = note
                out[key] = entry
            return json.dumps(out)
        dl.addCallback(assemble)
        return dl

    @staticmethod
    def _hash_uri(uri):
        # Same encoding older FileTransferData versions used for
        # directory names: urlsafe base64 of the uri's md5.
        return base64.urlsafe_b64encode(hashlib.md5(uri.encode('utf-8')).digest()).rstrip(b'=\n').decode('utf-8')

    TRANSFER_TYPES = ('audio', 'image', 'video', 'file')
    FILE_TRANSFER_CONTENT_TYPE = 'application/sylk-file-transfer'

    # Explicit extension maps checked before any mime type: mime types
    # coming from clients or from mimetypes.guess_type are unreliable
    # for container formats (.m4a is audio, yet often typed video/mp4).
    AUDIO_EXTENSIONS = {'.m4a', '.aac', '.mp3', '.ogg', '.oga', '.opus', '.wav',
                        '.flac', '.amr', '.awb', '.caf', '.aif', '.aiff', '.wma', '.mka'}
    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif',
                        '.bmp', '.tif', '.tiff', '.svg', '.avif', '.ico'}
    VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.wmv',
                        '.mpg', '.mpeg', '.3gp', '.3g2', '.ts'}

    @classmethod
    def _transfer_type(cls, filetype, filename):
        # Classify a transfer as audio, image, video or (generic) file.
        # The filename extension wins (with the .asc suffix of
        # PGP-encrypted files stripped), then the mime type stored in
        # the message metadata, then a mimetypes guess on the filename.
        name = filename or ''
        if name.endswith('.asc'):
            name = name[:-4]
        extension = os.path.splitext(name)[1].lower()
        if extension in cls.AUDIO_EXTENSIONS:
            return 'audio'
        if extension in cls.IMAGE_EXTENSIONS:
            return 'image'
        if extension in cls.VIDEO_EXTENSIONS:
            return 'video'
        candidates = []
        if filetype:
            candidates.append(filetype)
        guessed = mimetypes.guess_type(name)[0] if name else None
        if guessed:
            candidates.append(guessed)
        for mimetype in candidates:
            category = mimetype.split('/', 1)[0]
            if category in ('audio', 'image', 'video'):
                return category
        return 'file'

    def _probe_transfer_file(self, root, account, contact, transfer_id, filename):
        """Locate the file of a transfer in the file-transfer store.
        The store is laid out <letter>/<sender>/<receiver>/<transfer_id>/
        with directories named by plain account or by the legacy hashed
        names (urlsafe_b64(md5(uri))), and the file may sit under either
        party depending on which side stored it, so every combination is
        probed. Returns (filename, size) of the stored file or None."""
        pairs = []
        for encode in (lambda u: u, self._hash_uri, lambda u: self._hash_uri('sip:' + u)):
            one, two = encode(account), encode(contact)
            pairs.append((one, two))
            pairs.append((two, one))
        for top, sub in pairs:
            if not top or not sub:
                continue
            folder = os.path.join(root, top[:1], top, sub, transfer_id)
            if not os.path.isdir(folder):
                continue
            fallback = None
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if name.startswith('meta-'):
                    continue
                full = os.path.join(folder, name)
                if not os.path.isfile(full):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if filename and name in (filename, filename + '.asc'):
                    return name, size
                if fallback is None:
                    fallback = (name, size)
            if fallback is not None:
                return fallback
        return None

    def _orphan_transfer_dirs(self, root, account, contact_filter, db_ids):
        """Reverse search of the file-transfer store: transfer
        directories under the account's own trees (plain and legacy
        hashed names) whose transfer id has no file-transfer message in
        the database. Yields (receiver, transfer_id, path, filename,
        bytes, mtime) per orphan directory."""
        contact_names = None
        if contact_filter:
            contact_names = {contact_filter, self._hash_uri(contact_filter),
                             self._hash_uri('sip:' + contact_filter)}
        for name in (account, self._hash_uri(account), self._hash_uri('sip:' + account)):
            base = os.path.join(root, name[:1], name)
            if not os.path.isdir(base):
                continue
            for receiver in sorted(os.listdir(base)):
                receiver_path = os.path.join(base, receiver)
                if not os.path.isdir(receiver_path):
                    continue
                if contact_names is not None and receiver not in contact_names:
                    continue
                for transfer_id in os.listdir(receiver_path):
                    transfer_path = os.path.join(receiver_path, transfer_id)
                    if not os.path.isdir(transfer_path) or transfer_id in db_ids:
                        continue
                    total, filename, mtime = 0, None, 0
                    for dirpath, dirnames, filenames in os.walk(transfer_path):
                        for entry in filenames:
                            try:
                                stat = os.stat(os.path.join(dirpath, entry))
                            except OSError:
                                continue
                            total += stat.st_size
                            mtime = max(mtime, stat.st_mtime)
                            if filename is None and not entry.startswith('meta-'):
                                filename = entry
                    yield receiver, transfer_id, transfer_path, filename, total, mtime

    @app.route('/media/file-transfers/<string:account>', methods=['GET'])
    def file_transfers_account(self, request, account):
        """File transfers of one account, driven by the message database
        — the source of truth: every stored message with the
        application/sylk-file-transfer content type is one transfer,
        its message id being the transfer id. The file-transfer store
        on disk may or may not still hold the actual file, so after
        walking the database each transfer is probed on disk and
        reported with its on-disk status and size.

        Optional query parameters drive the admin drill-down:
        contact=<uri> narrows everything to messages with that contact
        and type=audio|image|video|file narrows the transfer list to
        one media type. The by_type breakdown reflects the contact
        filter (so drilling into a contact re-computes the type counts)
        while the contacts list always covers the whole account, so the
        UI can switch between contacts."""
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        args = request.args or {}
        contact_filter = args.get(b'contact', [b''])[0].decode('utf-8', 'replace').strip().lower()
        type_filter = args.get(b'type', [b''])[0].decode('utf-8', 'replace').strip().lower()
        if type_filter not in self.TRANSFER_TYPES and type_filter not in ('missing', 'nodb'):
            type_filter = ''
        sort = args.get(b'sort', [b'date'])[0].decode('utf-8', 'replace').strip().lower()
        if sort not in ('date', 'size', 'name'):
            sort = 'date'
        order = args.get(b'order', [b''])[0].decode('utf-8', 'replace').strip().lower()
        reverse = order != 'asc'
        root = GeneralConfig.file_transfer_dir.normalized
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points

        def assemble(messages, backend, get):
            total, files = 0, 0
            on_disk, disk_bytes = 0, 0
            transfers = []
            by_type = {transfer_type: {'count': 0, 'bytes': 0, 'on_disk': 0, 'disk_bytes': 0}
                       for transfer_type in self.TRANSFER_TYPES + ('missing', 'nodb')}
            db_ids = set()
            contacts = {}
            for message in messages:
                content_type = get(message, 'content_type') or ''
                if not content_type.startswith(self.FILE_TRANSFER_CONTENT_TYPE):
                    continue
                contact = (get(message, 'contact') or '').strip().lower()
                metadata = {}
                try:
                    metadata = json.loads(get(message, 'content') or '')
                except (TypeError, ValueError):
                    pass
                if not isinstance(metadata, dict):
                    metadata = {}
                filename = metadata.get('filename')
                try:
                    filesize = int(metadata.get('filesize'))
                except (TypeError, ValueError):
                    filesize = 0
                transfer_type = self._transfer_type(metadata.get('filetype'), filename)
                for candidate in (get(message, 'message_id'), metadata.get('transfer_id')):
                    if candidate:
                        db_ids.add(candidate)
                entry = contacts.setdefault(contact, {'count': 0, 'bytes': 0})
                entry['count'] += 1
                entry['bytes'] += filesize
                if contact_filter and contact != contact_filter:
                    continue
                transfer_id = get(message, 'message_id') or metadata.get('transfer_id') or ''
                stored = self._probe_transfer_file(root, account, contact, transfer_id, filename) if transfer_id else None
                if filename is None and stored is not None:
                    # metadata was encrypted or unparseable — classify
                    # from the name of the file found on disk instead
                    transfer_type = self._transfer_type(None, stored[0])
                total += filesize
                files += 1
                by_type[transfer_type]['count'] += 1
                by_type[transfer_type]['bytes'] += filesize
                if stored is not None:
                    on_disk += 1
                    disk_bytes += stored[1]
                    by_type[transfer_type]['on_disk'] += 1
                    by_type[transfer_type]['disk_bytes'] += stored[1]
                else:
                    by_type['missing']['count'] += 1
                    by_type['missing']['bytes'] += filesize
                if type_filter == 'missing':
                    if stored is not None:
                        continue
                elif type_filter and transfer_type != type_filter:
                    continue
                created_at = get(message, 'created_at') or get(message, 'timestamp')
                date = str(created_at or '').replace('T', ' ')[:16]
                transfers.append({'contact': contact,
                                  'direction': get(message, 'direction'),
                                  'transfer_id': transfer_id,
                                  'filename': filename or (stored[0] if stored else None),
                                  'type': transfer_type,
                                  'bytes': filesize,
                                  'date': date,
                                  'sort_key': str(created_at or ''),
                                  'on_disk': stored is not None,
                                  'disk_bytes': stored[1] if stored else 0})
            # reverse search: files on disk without a database entry
            for receiver, transfer_id, path, filename, size, mtime in self._orphan_transfer_dirs(root, account, contact_filter, db_ids):
                by_type['nodb']['count'] += 1
                by_type['nodb']['bytes'] += size
                if type_filter != 'nodb':
                    continue
                date = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime)) if mtime else ''
                transfers.append({'contact': receiver, 'direction': None,
                                  'transfer_id': transfer_id,
                                  'filename': filename,
                                  'type': self._transfer_type(None, filename),
                                  'bytes': size, 'date': date, 'sort_key': date,
                                  'on_disk': True, 'disk_bytes': size,
                                  'in_db': False})
            if sort == 'size':
                transfers.sort(key=lambda transfer: transfer['bytes'], reverse=reverse)
            elif sort == 'name':
                transfers.sort(key=lambda transfer: (transfer['filename'] or '').lower(), reverse=reverse)
            else:
                transfers.sort(key=lambda transfer: transfer['sort_key'], reverse=reverse)
            for transfer in transfers:
                del transfer['sort_key']
            contact_list = [dict(name=name, **usage) for name, usage in contacts.items()]
            contact_list.sort(key=lambda entry: (-entry['count'], entry['name']))
            return json.dumps({'account': account, 'backend': backend,
                               'contact': contact_filter, 'type': type_filter,
                               'sort': sort, 'order': 'desc' if reverse else 'asc',
                               'bytes': total, 'files': files,
                               'on_disk': on_disk, 'disk_bytes': disk_bytes,
                               'by_type': by_type, 'contacts': contact_list,
                               'total_transfers': len(transfers),
                               'transfers': transfers[:50]})

        if not use_cassandra:
            def scan_file_backend():
                messages = []
                try:
                    path = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations',
                                        account[0], '{}_messages.json'.format(account))
                    with open(path) as f:
                        messages = json.load(f)
                except (OSError, IOError, ValueError):
                    pass
                return assemble(messages, 'file', get=lambda m, k: m.get(k))
            return threads.deferToThread(scan_file_backend)

        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_transfers():
            from .models.storage.cassandra import ChatMessage
            try:
                messages = ChatMessage.objects(ChatMessage.account == account).limit(None)
                result = assemble(messages, 'cassandra', get=lambda m, k: getattr(m, k, None))
            except Exception as e:
                result = json.dumps({'account': account, 'backend': 'cassandra', 'error': str(e)})
            reactor.callFromThread(deferred.callback, result)

        query_transfers()
        return deferred

    @app.route('/media/file-transfers/<string:account>/purge-orphaned', methods=['POST'])
    def purge_orphaned_transfers(self, request, account):
        """Delete the account's file-transfer messages whose file is no
        longer in the file-transfer store (expired or removed) — the
        database rows stay the source of truth for what happened, this
        just drops the ones pointing at files that are gone. The scope
        follows the admin drill-down: an optional contact and type in
        the JSON body narrow what is purged. Like the other message
        deletion actions, only supported on the Cassandra backend."""
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        contact_filter = (payload.get('contact') or '').strip().lower()
        type_filter = (payload.get('type') or '').strip().lower()
        if type_filter not in self.TRANSFER_TYPES:
            # 'missing' needs no media-type restriction here — the purge
            # only ever deletes messages whose file is missing anyway
            type_filter = ''
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        if not use_cassandra:
            request.setResponseCode(501)
            return json.dumps({'ok': False, 'error': 'purge is only supported on the Cassandra backend'})
        root = GeneralConfig.file_transfer_dir.normalized
        log.info('[admin] purge of file-transfer messages without files requested for {}{}{}'.format(
            account,
            ' contact {}'.format(contact_filter) if contact_filter else '',
            ' type {}'.format(type_filter) if type_filter else ''))
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def purge():
            from .models.storage.cassandra import ChatMessage
            result = {'ok': True, 'account': account,
                      'contact': contact_filter, 'type': type_filter}
            checked = deleted = 0
            try:
                for message in ChatMessage.objects(ChatMessage.account == account).limit(None):
                    content_type = message.content_type or ''
                    if not content_type.startswith(self.FILE_TRANSFER_CONTENT_TYPE):
                        continue
                    contact = (message.contact or '').strip().lower()
                    if contact_filter and contact != contact_filter:
                        continue
                    try:
                        metadata = json.loads(message.content or '')
                    except (TypeError, ValueError):
                        metadata = {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    filename = metadata.get('filename')
                    transfer_id = message.message_id or metadata.get('transfer_id') or ''
                    stored = self._probe_transfer_file(root, account, contact, transfer_id, filename) if transfer_id else None
                    if type_filter:
                        transfer_type = self._transfer_type(metadata.get('filetype'), filename)
                        if filename is None and stored is not None:
                            transfer_type = self._transfer_type(None, stored[0])
                        if transfer_type != type_filter:
                            continue
                    checked += 1
                    if stored is None:
                        message.delete()
                        deleted += 1
            except Exception as e:
                result['ok'] = False
                result['error'] = str(e)
            result['checked'] = checked
            result['deleted'] = deleted
            log.info('[admin] purged {} of {} file-transfer message(s) without files for {}'.format(deleted, checked, account))
            reactor.callFromThread(deferred.callback, json.dumps(result))

        purge()
        return deferred

    def _transfer_dirs(self, root, account, contact, transfer_id):
        """Every existing directory of a transfer in the file store,
        across all layout variants: plain and legacy hashed names for
        either party, in sender-first and receiver-first order, mixed
        freely (the contact may already be a hashed directory name, as
        with entries found by the reverse search)."""
        variants = lambda uri: [name for name in (uri, self._hash_uri(uri), self._hash_uri('sip:' + uri)) if name]
        dirs = []
        for top in variants(account) + variants(contact):
            for sub in variants(contact) + variants(account):
                folder = os.path.join(root, top[:1], top, sub, transfer_id)
                if os.path.isdir(folder) and folder not in dirs:
                    dirs.append(folder)
        return dirs

    @app.route('/media/file-transfers/<string:account>/delete', methods=['POST'])
    def delete_transfer(self, request, account):
        """Delete one file transfer completely: this account's
        file-transfer message row(s) with the given message id AND the
        transfer's directories in the file store. Works also for
        entries only present on one side (a message whose file already
        expired, or a file without a database entry). Like the other
        message deletions, Cassandra backend only."""
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        transfer_id = (payload.get('transfer_id') or '').strip()
        contact = (payload.get('contact') or '').strip().lower()
        if not transfer_id:
            request.setResponseCode(400)
            return json.dumps({'ok': False, 'error': 'transfer_id is required'})
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        if not use_cassandra:
            request.setResponseCode(501)
            return json.dumps({'ok': False, 'error': 'deletion is only supported on the Cassandra backend'})
        root = GeneralConfig.file_transfer_dir.normalized
        log.info('[admin] delete file transfer {} requested for {}{}'.format(
            transfer_id, account, ' contact {}'.format(contact) if contact else ''))
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def delete_one():
            from .models.storage.cassandra import ChatMessage
            result = {'ok': True, 'account': account, 'transfer_id': transfer_id}
            deleted_messages = deleted_dirs = removed_bytes = 0
            try:
                matched = [message for message in ChatMessage.objects(ChatMessage.account == account).limit(None)
                           if message.message_id == transfer_id
                           and (message.content_type or '').startswith(self.FILE_TRANSFER_CONTENT_TYPE)]
                for message in matched:
                    message.delete()
                    deleted_messages += 1
                for folder in self._transfer_dirs(root, account, contact, transfer_id):
                    size = 0
                    for dirpath, dirnames, filenames in os.walk(folder):
                        for entry in filenames:
                            try:
                                size += os.path.getsize(os.path.join(dirpath, entry))
                            except OSError:
                                pass
                    try:
                        rmtree(folder)
                    except OSError as e:
                        result['ok'] = False
                        result['error'] = str(e)
                        continue
                    deleted_dirs += 1
                    removed_bytes += size
            except Exception as e:
                result['ok'] = False
                result['error'] = str(e)
            result['deleted_messages'] = deleted_messages
            result['deleted_dirs'] = deleted_dirs
            result['bytes'] = removed_bytes
            log.info('[admin] deleted file transfer {} for {}: {} message(s), {} folder(s), {} bytes'.format(
                transfer_id, account, deleted_messages, deleted_dirs, removed_bytes))
            reactor.callFromThread(deferred.callback, json.dumps(result))

        delete_one()
        return deferred

    def _collect_transfer_ids(self, content_type, message_id, content, ids):
        """Collect the transfer ids a file-transfer message accounts
        for: its message id and the transfer_id in its metadata."""
        if not (content_type or '').startswith(self.FILE_TRANSFER_CONTENT_TYPE):
            return
        if message_id:
            ids.add(message_id)
        try:
            metadata = json.loads(content or '')
        except (TypeError, ValueError):
            return
        if isinstance(metadata, dict) and metadata.get('transfer_id'):
            ids.add(metadata['transfer_id'])

    @app.route('/media/file-transfers/<string:account>/delete-orphan-files', methods=['POST'])
    def delete_orphan_transfer_files(self, request, account):
        """Reverse cleanup: delete from the file-transfer store the
        transfer directories under this account that have no
        corresponding file-transfer message in the database — the
        database is the source of truth, so files it does not know
        about are leftovers. An optional contact in the JSON body
        narrows the scope. Only files are deleted, never database
        rows, so this works on both storage backends."""
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        contact_filter = (payload.get('contact') or '').strip().lower()
        root = GeneralConfig.file_transfer_dir.normalized
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        log.info('[admin] delete of file-transfer files without database entry requested for {}{}'.format(
            account, ' contact {}'.format(contact_filter) if contact_filter else ''))

        def delete_orphans(db_ids):
            deleted, removed_bytes, errors = 0, 0, 0
            for receiver, transfer_id, path, filename, size, mtime in list(self._orphan_transfer_dirs(root, account, contact_filter, db_ids)):
                try:
                    rmtree(path)
                except OSError:
                    errors += 1
                    continue
                deleted += 1
                removed_bytes += size
            result = {'ok': True, 'account': account, 'contact': contact_filter,
                      'deleted': deleted, 'bytes': removed_bytes}
            if errors:
                result['errors'] = errors
            log.info('[admin] deleted {} file-transfer folder(s) without database entry ({} bytes) for {}'.format(
                deleted, removed_bytes, account))
            return json.dumps(result)

        if not use_cassandra:
            def file_backend():
                ids = set()
                try:
                    path = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations',
                                        account[0], '{}_messages.json'.format(account))
                    with open(path) as f:
                        messages = json.load(f)
                except (OSError, IOError, ValueError):
                    messages = []
                for message in messages:
                    self._collect_transfer_ids(message.get('content_type'), message.get('message_id'),
                                               message.get('content'), ids)
                return delete_orphans(ids)
            return threads.deferToThread(file_backend)

        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_and_delete():
            from .models.storage.cassandra import ChatMessage
            try:
                ids = set()
                for message in ChatMessage.objects(ChatMessage.account == account).limit(None):
                    self._collect_transfer_ids(message.content_type, message.message_id, message.content, ids)
                result = delete_orphans(ids)
            except Exception as e:
                result = json.dumps({'ok': False, 'account': account, 'error': str(e)})
            reactor.callFromThread(deferred.callback, result)

        query_and_delete()
        return deferred

    # ------------------------------------------------------------------
    # Account lookup — per-account storage details for the Accounts tab
    # search box, modelled on the sylk-db 'show' and
    # sylk-dump-message-cassandra --types CLI tools: storage state, API
    # token (+TTL), last login, message counts by category and content
    # type, unread count, push tokens and PGP public keys.
    # ------------------------------------------------------------------

    @staticmethod
    def _categorize_messages(messages, account, get=None):
        get = get or (lambda m, k: getattr(m, k, None))
        counters = {'total': 0, 'text': 0, 'imdn': 0, 'filetransfer': 0,
                    'metadata': 0, 'other': 0, 'unread_text': 0}
        by_type = {}
        for message in messages:
            content_type = get(message, 'content_type') or 'unknown'
            counters['total'] += 1
            by_type[content_type] = by_type.get(content_type, 0) + 1
            if content_type.startswith('text'):
                counters['text'] += 1
            elif content_type.startswith('message/imdn'):
                counters['imdn'] += 1
            elif content_type.startswith('application/sylk-file-transfer'):
                counters['filetransfer'] += 1
            elif content_type.startswith('application/sylk-message-metadata'):
                counters['metadata'] += 1
            else:
                counters['other'] += 1
            if (content_type in ('text/plain', 'text/html')
                    and get(message, 'direction') == 'incoming'
                    and get(message, 'contact') != account
                    and 'display' in (get(message, 'disposition') or [])):
                counters['unread_text'] += 1
        counters['by_type'] = dict(sorted(by_type.items(), key=lambda item: -item[1]))
        return counters

    def _account_info_file(self, account):
        """Account details from the file backend (accounts.json etc)."""
        storage = MessageStorage()
        result = {'account': account, 'backend': 'file'}
        info = (getattr(storage, '_accounts', None) or {}).get(account)
        result['found'] = info is not None
        if info is not None:
            result['api_token'] = info.get('api_token')
            result['token_ttl'] = info.get('token_expire')
            result['last_login'] = info.get('last_login')
        messages = []
        try:
            path = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations',
                                account[0], '{}_messages.json'.format(account))
            with open(path) as f:
                messages = json.load(f)
        except (OSError, IOError, ValueError):
            pass
        result['messages'] = self._categorize_messages(messages, account, get=lambda m, k: m.get(k))
        devices = TokenStorage()[account] or {}
        result['push_tokens'] = [{'app_id': device.get('app_id'), 'device_id': device.get('device_id'),
                                  'platform': device.get('platform'),
                                  'token': device.get('token') or ''}
                                 for device in devices.values() if isinstance(device, dict)]
        public_key = (getattr(storage, '_public_keys', None) or {}).get(account)
        result['public_keys'] = [public_key] if public_key else []
        return json.dumps(result)

    # ------------------------------------------------------------------
    # Accounts marked for deletion — sylk-mobile's delete-account flow
    # stores an application/sylk-account-delete-request message on the
    # account's own AOR (see confirm_account_deletion below, which
    # correlates the email confirmation against it). This finds every
    # account holding such a message. On Cassandra it is a server-side
    # filtered scan of chat_messages_by_timestamp (ALLOW FILTERING) —
    # on-demand only, like the message counter.
    # ------------------------------------------------------------------

    ACCOUNT_DELETE_REQUEST_TYPE = 'application/sylk-account-delete-request'

    @app.route('/accounts/marked-for-deletion', methods=['GET'])
    def accounts_marked_for_deletion(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        started = time.time()

        def assemble(accounts, error=None):
            entries = [dict(account=name, **usage) for name, usage in accounts.items()]
            entries.sort(key=lambda entry: entry['last_request'] or '', reverse=True)
            result = {'content_type': self.ACCOUNT_DELETE_REQUEST_TYPE,
                      'total': len(entries), 'accounts': entries,
                      'elapsed': round(time.time() - started, 1)}
            if error:
                result['error'] = error
            return json.dumps(result)

        if not use_cassandra:
            def scan_files():
                accounts = {}
                conversations_dir = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations')
                for dirpath, dirnames, filenames in os.walk(conversations_dir):
                    for filename in filenames:
                        if not filename.endswith('_messages.json'):
                            continue
                        try:
                            with open(os.path.join(dirpath, filename)) as f:
                                messages = json.load(f)
                        except (OSError, IOError, ValueError):
                            continue
                        account = filename[:-len('_messages.json')]
                        for message in messages:
                            if message.get('content_type') == self.ACCOUNT_DELETE_REQUEST_TYPE:
                                entry = accounts.setdefault(account, {'requests': 0, 'last_request': None})
                                entry['requests'] += 1
                                created = str(message.get('created_at') or message.get('timestamp') or '')
                                if created and (entry['last_request'] is None or created > entry['last_request']):
                                    entry['last_request'] = created
                return assemble(accounts)
            return threads.deferToThread(scan_files)

        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_marked():
            from cassandra.cqlengine import connection as cql_connection
            from cassandra.query import SimpleStatement
            from .models.storage.cassandra import ChatMessage
            accounts = {}
            error = None
            try:
                session = cql_connection.get_session()
                statement = SimpleStatement(
                    'SELECT account, created_at FROM {}.{} WHERE content_type=%s ALLOW FILTERING'.format(
                        CassandraConfig.keyspace, ChatMessage.__table_name__),
                    fetch_size=1000)
                for row in session.execute(statement, [self.ACCOUNT_DELETE_REQUEST_TYPE], timeout=120):
                    entry = accounts.setdefault(row['account'], {'requests': 0, 'last_request': None})
                    entry['requests'] += 1
                    created = str(row['created_at']) if row['created_at'] is not None else None
                    if created and (entry['last_request'] is None or created > entry['last_request']):
                        entry['last_request'] = created
            except Exception as e:
                error = str(e)
            reactor.callFromThread(deferred.callback, assemble(accounts, error))

        query_marked()
        return deferred

    @app.route('/accounts/<string:account>/info', methods=['GET'])
    def account_info(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        if not use_cassandra:
            return self._account_info_file(account)

        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_account():
            from cassandra.cqlengine import connection as cql_connection
            from .models.storage.cassandra import (ChatAccount, ChatMessage,
                                                   PublicKey, PushTokens)
            result = {'account': account, 'backend': 'cassandra'}
            try:
                accounts = list(ChatAccount.objects(ChatAccount.account == account))
                result['found'] = bool(accounts)
                if accounts:
                    acc = accounts[0]
                    result['api_token'] = acc.api_token
                    result['last_login'] = str(acc.last_login) if acc.last_login is not None else None
                    if acc.api_token:
                        try:
                            session = cql_connection.get_session()
                            row = session.execute('SELECT TTL(api_token) AS ttl FROM {}.{} WHERE account=%s'.format(
                                CassandraConfig.keyspace, ChatAccount.__table_name__), [account])
                            result['token_ttl'] = row.one()['ttl']
                        except Exception:
                            result['token_ttl'] = None
                    messages = ChatMessage.objects(ChatMessage.account == account).limit(None)
                    result['messages'] = self._categorize_messages(messages, account)
                username, _, domain = account.partition('@')
                tokens = PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain)
                result['push_tokens'] = [{'app_id': token.app_id, 'device_id': token.device_id,
                                          'platform': token.platform, 'user_agent': token.user_agent,
                                          'token': token.device_token or ''}
                                         for token in tokens]
                keys = PublicKey.objects(PublicKey.account == account)
                result['public_keys'] = [key.public_key for key in keys]
            except Exception as e:
                result['error'] = str(e)
            reactor.callFromThread(deferred.callback, json.dumps(result))

        query_account()
        return deferred

    # ------------------------------------------------------------------
    # Message types — per-account breakdown by content type for the
    # Messages tab (like sylk-dump-message-cassandra <account> --types),
    # and per-type deletion (like sylk-delete-cassandra <account>
    # --type T --apply, but matching the content type EXACTLY, never by
    # prefix, so a delete button removes exactly what its row shows).
    # Like the CLI, deletion only removes ChatMessage rows.
    # ------------------------------------------------------------------

    @staticmethod
    def _message_type_stats(messages, get, contact_filter, date_filter):
        """Message counts by content type, with the admin drill-down
        filters applied: contact narrows to one contact and date_filter
        is a drill-down prefix ('', YYYY, YYYY-MM or YYYY-MM-DD) that
        narrows to that year, month or day. date_counts holds the next
        drill-down level below the current selection — messages per
        year, per month of the selected year, or per day of the
        selected month — like the client-side date filter.

        The two filters cross-scope each other's counters, so the
        drill-down works in one direction at a time: with a contact
        selected the date counters cover only that contact (until the
        contact is reset), and with a date selected the contact list
        and its counters cover only that period (until the date is
        reset)."""
        by_type = {}
        contacts = {}
        date_counts = {}
        oldest = newest = None
        level = len(date_filter)
        for message in messages:
            contact = (get(message, 'contact') or '').strip().lower()
            created = str(get(message, 'created_at') or get(message, 'timestamp') or '')
            date_match = not date_filter or created.startswith(date_filter)
            if date_match:
                # contact counters are scoped by the date filter
                contacts[contact] = contacts.get(contact, 0) + 1
            if contact_filter and contact != contact_filter:
                continue
            # date counters are scoped by the contact filter
            if created:
                if level == 0:
                    key = created[:4]
                elif created.startswith(date_filter):
                    key = created[5:7] if level == 4 else created[8:10] if level == 7 else None
                else:
                    key = None
                if key:
                    date_counts[key] = date_counts.get(key, 0) + 1
            if not date_match:
                continue
            content_type = get(message, 'content_type') or 'unknown'
            by_type[content_type] = by_type.get(content_type, 0) + 1
            if created:
                oldest = created if oldest is None or created < oldest else oldest
                newest = created if newest is None or created > newest else newest
        contact_list = [{'name': name, 'count': count} for name, count in contacts.items()]
        contact_list.sort(key=lambda entry: (-entry['count'], entry['name']))
        by_type = dict(sorted(by_type.items(), key=lambda item: -item[1]))
        return {'by_type': by_type, 'total': sum(by_type.values()),
                'contacts': contact_list,
                'date_counts': dict(sorted(date_counts.items())),
                'oldest': oldest[:19] if oldest else None,      # trim milliseconds
                'newest': newest[:19] if newest else None}

    @staticmethod
    def _date_prefix_arg(value):
        # a date drill-down prefix: YYYY, YYYY-MM or YYYY-MM-DD
        value = (value or '').strip()[:10]
        patterns = {4: 'dddd', 7: 'dddd-dd', 10: 'dddd-dd-dd'}
        pattern = patterns.get(len(value))
        if pattern and all((c == '-') == (p == '-') and (p == '-' or c.isdigit())
                           for c, p in zip(value, pattern)):
            return value
        return ''

    @app.route('/messages/types/<string:account>', methods=['GET'])
    def message_types(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        args = request.args or {}
        contact_filter = args.get(b'contact', [b''])[0].decode('utf-8', 'replace').strip().lower()
        date_filter = self._date_prefix_arg(args.get(b'date', [b''])[0].decode('utf-8', 'replace'))
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points

        if not use_cassandra:
            messages = []
            try:
                path = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations',
                                    account[0], '{}_messages.json'.format(account))
                with open(path) as f:
                    messages = json.load(f)
            except (OSError, IOError, ValueError):
                pass
            stats = self._message_type_stats(messages, lambda m, k: m.get(k),
                                             contact_filter, date_filter)
            return json.dumps(dict(stats, account=account, backend='file', can_delete=False,
                                   contact=contact_filter, date=date_filter))

        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_types():
            result = {'account': account, 'backend': 'cassandra', 'can_delete': True,
                      'contact': contact_filter, 'date': date_filter}
            from .models.storage.cassandra import ChatMessage
            try:
                messages = ChatMessage.objects(ChatMessage.account == account).limit(None)
                result.update(self._message_type_stats(messages, lambda m, k: getattr(m, k, None),
                                                       contact_filter, date_filter))
            except Exception as e:
                result['error'] = str(e)
            reactor.callFromThread(deferred.callback, json.dumps(result))

        query_types()
        return deferred

    @app.route('/messages/delete/<string:account>', methods=['POST'])
    def delete_messages_by_type(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        content_type = (payload.get('content_type') or '').strip()
        if not content_type:
            request.setResponseCode(400)
            return json.dumps({'ok': False, 'error': 'content_type is required'})
        contact_filter = (payload.get('contact') or '').strip().lower()
        date_filter = self._date_prefix_arg(payload.get('date'))
        # same rules the UI enforces: file transfers are deleted from the
        # File transfers tab (files included); text messages only for a
        # selected day, to avoid wiping whole conversations by accident
        if content_type.startswith(self.FILE_TRANSFER_CONTENT_TYPE):
            request.setResponseCode(400)
            return json.dumps({'ok': False, 'error': 'file-transfer messages are deleted from the File transfers tab'})
        if content_type.startswith('text/') and len(date_filter) != 10:
            request.setResponseCode(400)
            return json.dumps({'ok': False, 'error': 'text messages can only be deleted with a day selected'})
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        if not use_cassandra:
            request.setResponseCode(501)
            return json.dumps({'ok': False, 'error': 'deletion is only supported on the Cassandra backend'})

        log.info('[admin] delete messages requested for {} with type {}{}{}'.format(
            account, content_type,
            ' contact {}'.format(contact_filter) if contact_filter else '',
            ' date {}'.format(date_filter) if date_filter else ''))
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def delete_matching():
            from .models.storage.cassandra import ChatMessage
            result = {'ok': True, 'account': account, 'content_type': content_type,
                      'contact': contact_filter, 'date': date_filter}
            deleted = 0

            def matches(message):
                if message.content_type != content_type:
                    return False
                if contact_filter and (message.contact or '').strip().lower() != contact_filter:
                    return False
                if date_filter and not str(message.created_at or '').startswith(date_filter):
                    return False
                return True

            try:
                matched = [message for message in ChatMessage.objects(ChatMessage.account == account).limit(None)
                           if matches(message)]
                for message in matched:
                    message.delete()
                    deleted += 1
            except Exception as e:
                result['ok'] = False
                result['error'] = str(e)
            result['deleted'] = deleted
            log.info('[admin] deleted {} {} message(s) for {}'.format(deleted, content_type, account))
            reactor.callFromThread(deferred.callback, json.dumps(result))

        delete_matching()
        return deferred

    # ------------------------------------------------------------------
    # Account purge — permanently remove ALL server-side data of one
    # account, like 'sylk-db remove all <account>': every ChatMessage
    # row, the chat_accounts row (and its API token), all push tokens
    # and the PGP public key. In addition the account's shared files are
    # removed from the file-transfer store (both the plain-named sender
    # directory and the legacy hashed ones older releases wrote).
    # ------------------------------------------------------------------

    @app.route('/accounts/<string:account>/purge', methods=['POST'])
    def purge_account(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        if '@' not in account:
            request.setResponseCode(400)
            return json.dumps({'ok': False, 'error': 'invalid account'})
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        log.info('[admin] PURGE requested for account {}'.format(account))

        def purge_transfers():
            """Remove the account's sender directories from the
            file-transfer store; returns files/bytes removed."""
            root = GeneralConfig.file_transfer_dir.normalized
            removed_files, removed_bytes = 0, 0
            for name in (account, self._hash_uri(account), self._hash_uri('sip:' + account)):
                base = os.path.join(root, name[:1], name)
                if not os.path.isdir(base):
                    continue
                for dirpath, dirnames, filenames in os.walk(base):
                    for filename in filenames:
                        try:
                            removed_bytes += os.path.getsize(os.path.join(dirpath, filename))
                            removed_files += 1
                        except OSError:
                            pass
                rmtree(base, ignore_errors=True)
            self._du_cache.pop(root, None)   # invalidate the cached tile size
            return {'transfer_files': removed_files, 'transfer_bytes': removed_bytes}

        if use_cassandra:
            db_deferred = defer.Deferred()

            @run_in_thread('cassandra')
            def purge_db():
                from .models.storage.cassandra import (ChatAccount, ChatMessage,
                                                       PublicKey, PushTokens)
                counts = {}
                try:
                    deleted = 0
                    for message in ChatMessage.objects(ChatMessage.account == account).limit(None):
                        message.delete()
                        deleted += 1
                    counts['messages'] = deleted
                    deleted = 0
                    for acc in ChatAccount.objects(ChatAccount.account == account):
                        acc.delete()
                        deleted += 1
                    counts['account_rows'] = deleted
                    username, _, domain = account.partition('@')
                    deleted = 0
                    for token in PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain):
                        token.delete()
                        deleted += 1
                    counts['push_tokens'] = deleted
                    deleted = 0
                    for key in PublicKey.objects(PublicKey.account == account):
                        key.delete()
                        deleted += 1
                    counts['public_keys'] = deleted
                except Exception as e:
                    counts['error'] = str(e)
                reactor.callFromThread(db_deferred.callback, counts)

            purge_db()
        else:
            def purge_file_backend():
                counts = {}
                try:
                    TokenStorage().removeAll(account)
                    storage = MessageStorage()
                    storage.remove_account(account)
                    storage.remove_public_key(account)
                    conversations_dir = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations')
                    for suffix in ('_messages.json', '_id_timestamp.json'):
                        path = os.path.join(conversations_dir, account[0], account + suffix)
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    counts['messages'] = counts['account_rows'] = counts['push_tokens'] = counts['public_keys'] = -1
                except Exception as e:
                    counts['error'] = str(e)
                return counts
            db_deferred = threads.deferToThread(purge_file_backend)

        dl = defer.DeferredList([db_deferred, threads.deferToThread(purge_transfers)],
                                consumeErrors=True)

        def assemble(results):
            (db_ok, db_value), (ft_ok, ft_value) = results
            result = {'ok': True, 'account': account}
            if db_ok:
                result.update(db_value)
                if 'error' in db_value:
                    result['ok'] = False
            else:
                result['ok'] = False
                result['error'] = str(db_value.value)
            if ft_ok:
                result.update(ft_value)
            else:
                result['ok'] = False
                result.setdefault('error', str(ft_value.value))
            log.info('[admin] purged account {}: {}'.format(account, result))
            return json.dumps(result)
        dl.addCallback(assemble)
        return dl

    # ------------------------------------------------------------------
    # Message dump — fetch a specific message by account + message id,
    # like 'sylk-dump-message-cassandra <account> --id ID'. Fast path
    # resolves created_at through chat_message_created_at_by_id and does
    # a primary-key read; if the mapping is gone (expired TTL) it falls
    # back to scanning the account partition like the CLI does.
    # ------------------------------------------------------------------

    @staticmethod
    def _dump_message_row(created_at, direction, contact, content_type, state, disposition, timestamp, content):
        if isinstance(content, (bytes, bytearray)):
            content = content.decode('utf-8', 'ignore')
        return {'created_at': str(created_at) if created_at is not None else None,
                'timestamp': str(timestamp) if timestamp is not None else None,
                'direction': direction, 'contact': contact, 'content_type': content_type,
                'state': state, 'disposition': list(disposition or []), 'content': content}

    @app.route('/messages/dump/<string:account>/<string:message_id>', methods=['GET'])
    def dump_message(self, request, account, message_id):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        account = account.strip().lower()
        message_id = message_id.strip()
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points

        if not use_cassandra:
            result = {'account': account, 'message_id': message_id, 'backend': 'file'}
            messages = []
            try:
                path = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations',
                                    account[0], '{}_messages.json'.format(account))
                with open(path) as f:
                    messages = json.load(f)
            except (OSError, IOError, ValueError):
                pass
            rows = [self._dump_message_row(m.get('created_at'), m.get('direction'), m.get('contact'),
                                           m.get('content_type'), m.get('state'), m.get('disposition'),
                                           m.get('timestamp'), m.get('content'))
                    for m in messages if m.get('message_id') == message_id]
            result['found'] = bool(rows)
            result['messages'] = rows[:10]
            return json.dumps(result)

        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_message():
            from .models.storage.cassandra import ChatMessage, ChatMessageIdMapping
            result = {'account': account, 'message_id': message_id, 'backend': 'cassandra'}
            rows = []
            try:
                mappings = list(ChatMessageIdMapping.objects(ChatMessageIdMapping.message_id == message_id))
                if mappings and mappings[0].created_at is not None:
                    rows = list(ChatMessage.objects(ChatMessage.account == account,
                                                    ChatMessage.created_at == mappings[0].created_at,
                                                    ChatMessage.message_id == message_id))
                if not rows:
                    # mapping expired or missing — scan the account partition
                    rows = [m for m in ChatMessage.objects(ChatMessage.account == account).limit(None)
                            if m.message_id == message_id]
                    result['scanned'] = True
            except Exception as e:
                result['error'] = str(e)
            result['found'] = bool(rows)
            result['messages'] = [self._dump_message_row(m.created_at, m.direction, m.contact,
                                                         m.content_type, m.state, m.disposition,
                                                         m.msg_timestamp, m.content)
                                  for m in rows[:10]]
            reactor.callFromThread(deferred.callback, json.dumps(result))

        query_message()
        return deferred

    @app.route('/storage/messages', methods=['GET'])
    def storage_messages_count(self, request):
        """Total messages in the DB. Deliberately a separate endpoint,
        triggered from the UI's 'Count' button rather than on every
        Storage load: on Cassandra it is a full scan of
        chat_messages_by_timestamp (SELECT COUNT(*)), which can take a
        while on a big cluster. Rows expired by the one-year TTL are not
        included. On the file backend it walks every *_messages.json."""
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        started = time.time()
        use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points

        def finish(result):
            result['elapsed'] = round(time.time() - started, 1)
            return json.dumps(result)

        if use_cassandra:
            deferred = defer.Deferred()

            @run_in_thread('cassandra')
            def count_messages():
                from .models.storage.cassandra import ChatMessage
                try:
                    total = ChatMessage.objects.timeout(120).count()
                except Exception as e:
                    reactor.callFromThread(deferred.callback, {'error': str(e)})
                else:
                    reactor.callFromThread(deferred.callback, {'total': total})

            count_messages()
            return deferred.addCallback(finish)
        else:
            def count_files():
                conversations_dir = os.path.join(FileStorageConfig.storage_dir.normalized, 'conversations')
                total = 0
                for dirpath, dirnames, filenames in os.walk(conversations_dir):
                    for filename in filenames:
                        if filename.endswith('_messages.json'):
                            try:
                                with open(os.path.join(dirpath, filename)) as f:
                                    total += len(json.load(f))
                            except (OSError, IOError, ValueError):
                                pass
                return {'total': total}
            return threads.deferToThread(count_files).addCallback(finish)

    # ------------------------------------------------------------------
    # Videoroom lookup — used by sip-janus-bridge and similar tooling to
    # translate the SIP-side room URI (e.g. 299472434@videoconference...)
    # into the random numeric Janus room id sylkserver assigned at
    # create time. Authenticated via http_management_auth_secret.
    # ------------------------------------------------------------------

    @app.route('/rooms', methods=['GET'])
    def list_rooms(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        rooms = []
        for room in SylkWebSocketServerFactory.videorooms:
            try:
                sessions = len(room._sessions)
            except AttributeError:
                sessions = 0
            rooms.append({
                'uri': room.uri,
                'janus_room_id': room.id,
                'sessions': sessions,
            })
        return json.dumps({'rooms': rooms})

    @app.route('/rooms/<string:uri>', methods=['GET'])
    def get_room_by_uri(self, request, uri):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        # Klein passes the path segment URL-decoded already; videoroom keys
        # are stored as-is (lower-cased at create time by sylkserver).
        key = uri.lower()
        if key not in SylkWebSocketServerFactory.videorooms:
            request.setResponseCode(404)
            return json.dumps({'error': 'no such room', 'uri': uri})
        room = SylkWebSocketServerFactory.videorooms[key]
        try:
            sessions = len(room._sessions)
        except AttributeError:
            sessions = 0
        return json.dumps({
            'uri': room.uri,
            'janus_room_id': room.id,
            'sessions': sessions,
        })

    @app.route('/rooms/by-id/<int:janus_room_id>', methods=['GET'])
    def get_room_by_id(self, request, janus_room_id):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        if janus_room_id not in SylkWebSocketServerFactory.videorooms:
            request.setResponseCode(404)
            return json.dumps({
                'error': 'no such room',
                'janus_room_id': janus_room_id,
            })
        room = SylkWebSocketServerFactory.videorooms[janus_room_id]
        try:
            sessions = len(room._sessions)
        except AttributeError:
            sessions = 0
        return json.dumps({
            'uri': room.uri,
            'janus_room_id': room.id,
            'sessions': sessions,
        })

    @staticmethod
    def _room_participants(room):
        """Build the unified participant list for a Videoroom.

        Two populations are merged:
          * WebRTC publishers — the VideoroomSessionInfo objects in
            room._sessions with type 'publisher' (the audio bridge is
            tagged 'bridge' and excluded; subscriber feeds are skipped).
            Their `target_id` is the gateway videoroom session id.
          * SIP callers behind the audio bridge — room.sip_participants,
            rebuilt from every conference-info NOTIFY. Their `target_id`
            is the conference focus's per-session participant id.

        `target_id` is what the kick / mute endpoints address.
        """
        # Reverse map: WebRTC videoroom session id -> conference focus
        # participant id (pid). Audio-level datagrams are keyed by pid, so
        # this lets the UI match levels to WebRTC rows (SIP rows already
        # use the pid as their target_id). Built from the room's
        # NOTIFY-derived webrtc_participants_by_pid {pid: session}.
        session_id_to_pid = {}
        for pid, sess in (getattr(room, 'webrtc_participants_by_pid', None) or {}).items():
            sid = getattr(sess, 'id', None)
            if sid is not None:
                session_id_to_pid[sid] = pid
        participants = []
        for session in list(room._sessions):
            if getattr(session, 'type', None) != 'publisher':
                continue
            account = getattr(session, 'account', None)
            participants.append({
                'kind': 'webrtc',
                'target_id': session.id,
                'session_id': session.id,
                'audio_pid': session_id_to_pid.get(session.id),
                'janus_pid': getattr(session, 'publisher_id', None),
                'uri': getattr(account, 'id', None),
                'display_name': getattr(account, 'display_name', None),
                'user_agent': getattr(account, 'user_agent', None),
                'muted': None,  # source-controlled for WebRTC; not tracked here
                'slow_download': bool(getattr(session, 'slow_download', False)),
                'slow_upload': bool(getattr(session, 'slow_upload', False)),
            })
        for sp in list(getattr(room, 'sip_participants', None) or []):
            participants.append({
                'kind': 'sip',
                'target_id': sp.get('id'),
                'session_id': None,
                'audio_pid': sp.get('id'),
                'uri': sp.get('uri'),
                'display_name': sp.get('display_name'),
                'user_agent': sp.get('user_agent'),
                'muted': sp.get('muted'),
                'slow_download': False,
                'slow_upload': False,
            })
        # The audio bridge (sylk-janus-audio-bridge) — shown for visibility
        # only; it's infrastructure, so it carries no mute/kick actions and
        # no audio meter (its level is the conference mix, not a speaker).
        bridge = getattr(room, 'bridge_info', None)
        if bridge:
            participants.append({
                'kind': 'bridge',
                'target_id': bridge.get('id') or 'bridge',
                'session_id': None,
                'audio_pid': None,
                'janus_pid': None,
                'uri': bridge.get('uri'),
                'display_name': bridge.get('display_name') or 'Audio bridge',
                'user_agent': None,
                'muted': None,
                'slow_download': False,
                'slow_upload': False,
            })
        order = {'bridge': 0, 'webrtc': 1, 'sip': 2}
        participants.sort(key=lambda p: (order.get(p['kind'], 9), (p['uri'] or ''), str(p['target_id'])))
        return participants

    @app.route('/rooms/<string:uri>/participants', methods=['GET'])
    def get_room_participants(self, request, uri):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        key = uri.lower()
        if key not in SylkWebSocketServerFactory.videorooms:
            request.setResponseCode(404)
            return json.dumps({'error': 'no such room', 'uri': uri})
        room = SylkWebSocketServerFactory.videorooms[key]
        participants = self._room_participants(room)
        return json.dumps({
            'uri': room.uri,
            'janus_room_id': room.id,
            'participant_count': len(participants),
            'participants': participants,
        })

    # Latched WebRTC talking state is held until a stopped-talking event
    # arrives; if one is somehow missed (publisher dropped mid-talk) the
    # state would stick "on". Decay it after this many ms with no update.
    WEBRTC_TALKING_TTL_MS = 10000
    # SIP meter shaping. The focus reports pjmedia signal levels (0..255)
    # which are *mean* amplitude — for speech the mean sits low, so a raw
    # linear bar reads tiny even when it's loud (and doesn't track what you
    # hear). We instead drive the meter from the per-window PEAK and apply a
    # perceptual dB curve, matching how the sylk client VU meters look.
    #   FOCUS_DB_FLOOR: levels at/below this many dBFS read as 0 (also acts
    #                   as the silence gate — raise toward 0 for less
    #                   sensitivity, lower (e.g. -36) for more headroom).
    FOCUS_DB_FLOOR = -30.0
    FOCUS_SPEAKING_DISPLAY = 60  # of 255 on the shaped scale -> "speaking"

    @staticmethod
    def _focus_display_level(linear):
        # Map a 0..255 linear amplitude (peak) to a 0..255 perceptual meter
        # value via 20*log10, floored at FOCUS_DB_FLOOR (which doubles as a
        # noise gate so silence stays at 0).
        if linear <= 0:
            return 0
        db = 20.0 * math.log10(min(255.0, float(linear)) / 255.0)
        floor = AdminWebHandler.FOCUS_DB_FLOOR
        if db <= floor:
            return 0
        return int(round((1.0 - db / floor) * 255))

    @app.route('/rooms/<string:uri>/audio-levels', methods=['GET'])
    def get_room_audio_levels(self, request, uri):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        key = uri.lower()
        if key not in SylkWebSocketServerFactory.videorooms:
            request.setResponseCode(404)
            return json.dumps({'error': 'no such room', 'uri': uri})
        room = SylkWebSocketServerFactory.videorooms[key]
        participants = self._room_participants(room)
        # Two sources, merged per participant and keyed by target_id (the
        # same id the UI uses for each row):
        #   * SIP callers  -> conference focus UDP snapshot (continuous
        #                     0..255 RX level), keyed by focus pid.
        #   * WebRTC peers -> Janus talking/stopped-talking state (latched
        #                     speaker flag + a dBov-derived level), keyed
        #                     by Janus publisher_id.
        snap = AudioLevelUDPClient().latest_levels.get(key) or {}
        focus_levels = snap.get('levels', {}) if snap else {}
        focus_age = (int(max(0.0, (time.time() - snap.get('received', 0))) * 1000)
                     if snap else None)
        focus_stale = focus_age is None or focus_age > 3000
        talking_map = getattr(room, 'webrtc_talking', None) or {}
        now = time.time()
        out = {}
        for p in participants:
            tid = str(p['target_id'])
            if p['kind'] == 'sip':
                pid = p.get('audio_pid')
                v = focus_levels.get(pid) if pid else None
                # Drive the meter from the per-window peak (falls back to the
                # mean if a peak wasn't reported) and shape it perceptually.
                raw = int(v.get('rx_peak', v.get('rx', 0))) if v else 0
                display = self._focus_display_level(raw)
                out[tid] = {
                    'value': display, 'raw': raw,
                    'talking': (not focus_stale and display >= self.FOCUS_SPEAKING_DISPLAY),
                    'source': 'focus', 'stale': bool(focus_stale),
                }
            else:
                jpid = p.get('janus_pid')
                entry = talking_map.get(jpid) if jpid is not None else None
                if entry and (now - entry.get('ts', now)) * 1000 <= self.WEBRTC_TALKING_TTL_MS:
                    talking = bool(entry.get('talking'))
                    out[tid] = {
                        'value': int(entry.get('level', 0)) if talking else 0,
                        'talking': talking, 'source': 'janus',
                        'dbov': entry.get('dbov'), 'stale': False,
                    }
                else:
                    out[tid] = {'value': 0, 'talking': False, 'source': 'janus', 'stale': False}
        return json.dumps({
            'uri': uri,
            'scale': 255,
            'focus_age_ms': focus_age,
            'levels': out,
        })

    # ------------------------------------------------------------------
    # Moderation — kick / mute. These reuse the per-connection request
    # handlers (_RH_videoroom_remove / _RH_videoroom_mute_participant)
    # which already route correctly per participant type (Janus kick for
    # WebRTC, SIP REFER ;method=BYE/MUTE for callers behind the bridge).
    # They must run in the gateway's green thread, so we marshal onto it
    # via call_in_green_thread and reply to the browser with a queued ack.
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_moderator(room, require_focus=False, exclude_session_id=None):
        """Return a WebRTC publisher session in the room to act as the
        moderator for a control request, or None. When require_focus is
        set, only a session whose chat leg reached a SIP focus qualifies
        (needed to REFER SIP participants)."""
        for session in list(room._sessions):
            if getattr(session, 'type', None) != 'publisher':
                continue
            if getattr(session, 'owner', None) is None:
                continue
            if exclude_session_id is not None and session.id == exclude_session_id:
                continue
            if require_focus:
                ch = getattr(session, 'chat_handler', None)
                sip = getattr(ch, 'sip_session', None) if ch is not None else None
                if sip is None or not getattr(sip, 'remote_focus', False):
                    continue
            return session
        return None

    class _AdminModerationRequest(object):
        """Minimal stand-in for a sylkrtc request object — the reused
        handlers only read these attributes."""
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def _dispatch_moderation(self, room, moderator, method_name, req, label):
        """Run a moderation handler in the green thread, logging failures."""
        def _go():
            try:
                getattr(moderator.owner, method_name)(req)
            except Exception as e:
                room.log.warning('admin {} failed: {}'.format(label, e))
        call_in_green_thread(_go)

    @app.route('/rooms/<string:uri>/participants/<string:target_id>/kick', methods=['POST'])
    def kick_participant(self, request, uri, target_id):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        key = uri.lower()
        if key not in SylkWebSocketServerFactory.videorooms:
            request.setResponseCode(404)
            return json.dumps({'ok': False, 'error': 'no such room'})
        room = SylkWebSocketServerFactory.videorooms[key]
        # A WebRTC target is addressed by its own session id; a SIP target
        # by the focus participant id. Need a moderator that isn't the
        # target itself (the kick handler refuses self-kick), and one with
        # a focus chat leg when the target is SIP.
        is_sip = target_id not in room._id_map
        moderator = self._pick_moderator(room, require_focus=is_sip,
                                         exclude_session_id=target_id)
        if moderator is None:
            request.setResponseCode(409)
            return json.dumps({'ok': False,
                               'error': 'no eligible moderator session in room'})
        req = self._AdminModerationRequest(session=moderator.id,
                                           participants=[target_id])
        room.log.info('admin kick requested for {} in room {}'.format(target_id, room.uri))
        self._dispatch_moderation(room, moderator, '_RH_videoroom_remove', req,
                                  'kick {}'.format(target_id))
        return json.dumps({'ok': True, 'queued': True})

    @app.route('/rooms/<string:uri>/participants/<string:target_id>/mute', methods=['POST'])
    def mute_participant(self, request, uri, target_id):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        key = uri.lower()
        if key not in SylkWebSocketServerFactory.videorooms:
            request.setResponseCode(404)
            return json.dumps({'ok': False, 'error': 'no such room'})
        room = SylkWebSocketServerFactory.videorooms[key]
        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        muted = bool(payload.get('muted', True))
        is_sip = target_id not in room._id_map
        moderator = self._pick_moderator(room, require_focus=is_sip)
        if moderator is None:
            request.setResponseCode(409)
            return json.dumps({'ok': False,
                               'error': 'no eligible moderator session in room'})
        req = self._AdminModerationRequest(session=moderator.id,
                                           participant_id=target_id, muted=muted)
        room.log.info('admin {} requested for {} in room {}'.format(
            'mute' if muted else 'unmute', target_id, room.uri))
        self._dispatch_moderation(room, moderator, '_RH_videoroom_mute_participant',
                                  req, '{} {}'.format('mute' if muted else 'unmute', target_id))
        return json.dumps({'ok': True, 'queued': True, 'muted': muted})

    @app.route('/metrics/daily', methods=['GET'])
    def metrics_daily(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        try:
            days = min(365, max(1, int(request.args.get(b'days', [b'30'])[0])))
        except (ValueError, TypeError):
            days = 30
        deferred = Metrics().get_daily(days)

        def fmt(result):
            day_list = [(datetime.datetime.utcnow() - datetime.timedelta(days=i)).strftime('%Y%m%d')
                        for i in range(days - 1, -1, -1)]
            metrics = {metric: [{'day': day, 'value': int((result.get(metric) or {}).get(day, 0))}
                                for day in day_list]
                       for metric in ('connections', 'registrations', 'accounts', 'sessions', 'sessions_audio', 'sessions_video', 'conferences', 'messages')}
            out = {'days': days, 'metrics': metrics}
            if 'error' in result:
                out['error'] = result['error']
            return json.dumps(out)
        deferred.addCallback(fmt)
        return deferred

    @app.route('/rooms/events', methods=['GET'])
    def rooms_events(self, request):
        """
        Server-Sent Events stream of videoroom lifecycle events.
        Each event is a `data: {...}\\n\\n` JSON payload with at least:
            type:  "room-created" | "room-destroyed"
            uri:   the room's SIP URI
            janus_room_id: numeric Janus room id

        Klein keeps the response open as long as the Deferred we return
        below stays unfired. It fires only when the client disconnects.
        """
        self._check_auth(request)
        request.setHeader('Content-Type', 'text/event-stream')
        request.setHeader('Cache-Control', 'no-cache')
        request.setHeader('Connection', 'keep-alive')
        request.setHeader('Access-Control-Allow-Origin', '*')

        request.write(b': connected\n\n')

        # Seed with current set of live rooms so the client doesn't need
        # a separate /rooms call to bootstrap.
        for room in SylkWebSocketServerFactory.videorooms:
            payload = json.dumps({
                'type': 'room-created',
                'uri': room.uri,
                'janus_room_id': room.id,
            })
            request.write(('data: ' + payload + '\n\n').encode('utf-8'))

        self._event_subscribers.add(request)

        # Send a comment every 25 s so the connection survives any
        # idle-timeout in proxies / HAProxy / load balancers.
        keepalive_call = reactor.callLater(25, self._sse_keepalive, request)

        # Return a Deferred that fires only when the peer disconnects —
        # this is what tells Klein to keep the HTTP response open.
        done = defer.Deferred()

        def _on_finish(_):
            self._event_subscribers.discard(request)
            if keepalive_call.active():
                keepalive_call.cancel()
            if not done.called:
                done.callback(None)
        request.notifyFinish().addBoth(_on_finish)
        return done

    def _sse_keepalive(self, request):
        try:
            request.write(b': keepalive\n\n')
        except Exception:
            self._event_subscribers.discard(request)
            return
        # Re-arm
        if request in self._event_subscribers:
            reactor.callLater(25, self._sse_keepalive, request)

    @app.route('/accounts/<string:account>', methods=['POST'])
    def confirm_account_deletion(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')

        # Read the POST body. cdrtool sends application/json; we
        # accept it as-is and fall back to logging the raw bytes
        # if it doesn't parse so the operator can spot a malformed
        # caller.
        raw = request.content.read() if request.content else b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError) as e:
            log.warning(f'[delete-account-api] {account}: malformed POST body ({e}); raw={raw[:200]!r}')
            payload = {}

        log.info(f'[delete-account-api] {account}: confirmation received, payload={json.dumps(payload, sort_keys=True)}')

        client_request_id = payload.get('client_request_id') if isinstance(payload, dict) else None
        if not client_request_id:
            log.info(f'[delete-account-api] {account}: no client_request_id in payload — cannot correlate to a chat-side message')
            return json.dumps({
                'ok': True,
                'matched': False,
                'reason': 'no_client_request_id_in_payload',
            })

        # Two-channel proof: the mobile/web client sent an
        # application/sylk-account-delete-request message to its
        # own AOR at request time, using client_request_id as the
        # message_id. A match here means the SIP-credential
        # holder INITIATED the request AND the email holder
        # CONFIRMED it — both halves of the round-trip accounted
        # for. Logging only at this stage; no storage cleanup.
        deferred = MessageStorage().find_message(
            account,
            client_request_id,
            content_type='application/sylk-account-delete-request',
        )

        def _on_result(matched):
            if matched:
                log.info(f'[delete-account-api] {account}: '
                         f'account deletion request match email confirmation request '
                         f'(client_request_id={client_request_id})')
            else:
                log.info(f'[delete-account-api] {account}: '
                         f'no user request has been found for the delete operation '
                         f'(client_request_id={client_request_id})')
            return json.dumps({
                'ok':                True,
                'matched':           bool(matched),
                'client_request_id': client_request_id,
            })

        deferred.addCallback(_on_result)
        return deferred
