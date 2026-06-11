
import hashlib
import hmac
import json
import math
import mimetypes
import os
import secrets
import time
from shutil import copyfileobj

from application.notification import IObserver, NotificationCenter
from application.python.types import Singleton
from application.system import makedirs
from autobahn.twisted.resource import WebSocketResource
from sipsimple.streams.msrp.filetransfer import FileSelector
from sipsimple.threading.green import call_in_green_thread
from twisted.internet import defer, reactor
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
from .configuration import GeneralConfig, JanusConfig
from .datatypes import FileTransferData
from .factory import SylkWebSocketServerFactory
from .janus import JanusBackend
from .logger import log
from .models import sylkrtc
from .protocol import SYLK_WS_PROTOCOL
from .sip_handlers import MessageHandler
from .storage import MessageStorage, TokenStorage

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

    def stop(self):
        if self.factory is not None:
            for conn in self.factory.connections.copy():
                conn.dropConnection(abort=True)
            self.factory = None
        if self.backend is not None:
            self.backend.stop()
            self.backend = None


ADMIN_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SylkServer · Conferences</title>
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
        <h2>SylkServer Admin</h2>
        <p>${configured ? 'Sign in to view live conferences.' : 'Admin login is not configured on this server.'}</p>
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

function showDashboard(username) {
  $('app').innerHTML = `
    <header class="topbar">
      <span class="dot"></span>
      <h1>SylkServer · Live Conferences</h1>
      <span class="spacer"></span>
      <span class="who">${esc(username || '')}</span>
      <button class="btn btn-light" onclick="logout()">Sign out</button>
    </header>
    <main>
      <div class="toolbar">
        <h2>Conferences</h2>
        <span class="count" id="roomCount">—</span>
        <span class="spacer"></span>
        <button class="btn btn-accent" onclick="loadRooms()">Refresh</button>
      </div>
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
    </main>`;
  loadRooms();
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
  // Poll for session-count changes (joins/leaves don't emit SSE events).
  pollTimer = setInterval(() => {
    loadRooms();
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

        # Plain HTTP listener — internal tooling (sip-janus-bridge, the
        # audio bridge's /rooms/events SSE, etc.). Always on.
        host, port = GeneralConfig.http_management_interface
        # noinspection PyUnresolvedReferences
        self.listener = reactor.listenTCP(port, site, interface=host)
        self._listeners.append(self.listener)
        log.info('Admin web handler started at http://%s:%d' % (host, port))

        # Optional HTTPS listener — browser admin UI over the internet,
        # using the same certificate as the main web/WebSocket server.
        https_iface = GeneralConfig.https_management_interface
        if https_iface:
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
