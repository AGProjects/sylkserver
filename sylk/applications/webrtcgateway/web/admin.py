
import json

from application.python.types import Singleton
from klein import Klein
from twisted.internet import reactor
from twisted.web.server import Site

from sylk.applications.webrtcgateway.configuration import GeneralConfig
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.web import push
from sylk.applications.webrtcgateway.web.storage import TokenStorage

__all__ = ['AdminWebHandler']


class AuthError(Exception): pass


class AdminWebHandler(object):
    __metaclass__ = Singleton

    app = Klein()

    def __init__(self):
        self.listener = None

    def start(self):
        host, port = GeneralConfig.http_management_interface
        self.listener = reactor.listenTCP(port, Site(self.app.resource()), interface=host)
        log.msg('Admin web handler started at http://%s:%d' % (host, port))

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

    @app.route('/incoming_session', methods=['POST'])
    def incoming_session(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        try:
            data = json.load(request.content)
            originator = data['originator']
            destination = data['destination']
        except Exception, e:
            return json.dumps({'success': False, 'error': str(e)})
        else:
            storage = TokenStorage()
            tokens = storage[destination]
            push.incoming_session(originator, destination, tokens)
            return json.dumps({'success': True})

    @app.route('/missed_session', methods=['POST'])
    def missed_session(self, request):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        try:
            data = json.load(request.content)
            originator = data['originator']
            destination = data['destination']
        except Exception, e:
            return json.dumps({'success': False, 'error': str(e)})
        else:
            storage = TokenStorage()
            tokens = storage[destination]
            push.missed_session(originator, destination, tokens)
            return json.dumps({'success': True})

    @app.route('/tokens/<string:account>')
    def get_tokens(self, request, account):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        storage = TokenStorage()
        tokens = storage[account]
        return json.dumps({'tokens': list(tokens)})

    @app.route('/tokens/<string:account>/<string:token>', methods=['POST', 'DELETE'])
    def process_token(self, request, account, token):
        self._check_auth(request)
        request.setHeader('Content-Type', 'application/json')
        storage = TokenStorage()
        if request.method == 'POST':
            storage.add(account, token)
        elif request.method == 'DELETE':
            storage.remove(account, token)
        return json.dumps({'success': True})
