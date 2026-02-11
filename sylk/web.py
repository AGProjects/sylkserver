import mimetypes
import os
import re

import twisted.web.server
from application import log
from application.python.types import Singleton
from klein import Klein
from twisted.internet import reactor
from twisted.internet.ssl import DefaultOpenSSLContextFactory
from twisted.web.http import HTTPChannel
from twisted.web.resource import NoResource, Resource
from twisted.web.server import Site
from twisted.web.static import File

from sylk import __version__
from sylk.configuration import WebServerConfig

__all__ = 'Klein', 'StaticFileResource', 'WebServer', 'server'


# Set the 'Server' header string which Twisted Web will use
twisted.web.server.version = b'SylkServer/%s' % __version__.encode()

def loadMimeTypes(mimetype_locations='mime.types', init=mimetypes.init):
    """
    Produces a mapping of extensions (with leading dot) to MIME types.

    It does this by calling the C{init} function of the L{mimetypes} module.
    This will have the side effect of modifying the global MIME types cache
    in that module.

    Multiple file locations containing mime-types can be passed as a list.
    The files will be sourced in that order, overriding mime-types from the
    files sourced beforehand, but only if a new entry explicitly overrides
    the current entry.

    @param mimetype_locations: Optional. List of paths to C{mime.types} style
        files that should be used.
    @type mimetype_locations: iterable of paths or L{None}
    @param init: The init function to call. Defaults to the global C{init}
        function of the C{mimetypes} module. For internal use (testing) only.
    @type init: callable
    """
    init(mimetype_locations)
    mimetypes.types_map.update(
        {
            '.conf':  'text/plain',
            '.diff':  'text/plain',
            '.flac':  'audio/x-flac',
            '.java':  'text/plain',
            '.oz':    'text/x-oz',
            '.swf':   'application/x-shockwave-flash',
            '.wml':   'text/vnd.wap.wml',
            '.xul':   'application/vnd.mozilla.xul+xml',
            '.patch': 'text/plain'
        }
    )
    return mimetypes.types_map
    

class StaticFileResource(File):
    contentTypes = loadMimeTypes()
    def directoryListing(self):
        return NoResource('Directory listing not available')


class RootResource(Resource):
    isLeaf = True

    def render_GET(self, request):
        request.setHeader('Content-Type', 'text/plain')
        return b'Welcome to SylkServer!'


class TrackedUploadHTTPChannel(HTTPChannel):
    active_uploads = {}

    def lineReceived(self, line):
        if not self.requests:
            try:
                method, path, version = line.decode().split(" ", 2)
            except ValueError:
                return super().lineReceived(line)
            if method.upper() == "POST":
                match = re.match(r'/webrtcgateway/filetransfer/[^/]+/[^/]+/([^/]+)/', path)
                if match:
                    transfer_id = match.group(1)
                    self.current_transfer_id = transfer_id
                    TrackedUploadHTTPChannel.active_uploads[transfer_id] = self
                    log.debug(f"Captured http transfer_id early: {transfer_id}")
        super().lineReceived(line)

    def requestDone(self, request):
        if hasattr(self, "current_transfer_id"):
            log.debug(f"HTTP request done, remove {self.current_transfer_id}")
            transfer_id = self.current_transfer_id
            TrackedUploadHTTPChannel.active_uploads.pop(transfer_id, None)
            del self.current_transfer_id
        super().requestDone(request)

    def connectionLost(self, reason):
        if hasattr(self, "current_transfer_id"):
            log.debug(f"HTTP connection lost, removing {self.current_transfer_id}")
            transfer_id = self.current_transfer_id
            TrackedUploadHTTPChannel.active_uploads.pop(transfer_id, None)
            del self.current_transfer_id
        super().connectionLost(reason)


class TrackedUploadSite(Site):
    protocol = TrackedUploadHTTPChannel


class WebServer(object, metaclass=Singleton):
    def __init__(self):
        self.base = Resource()
        self.base.putChild(b'', RootResource())
        self.site = TrackedUploadSite(self.base, logPath=None)
        self.site.noisy = False
        self.listener = None

    @property
    def url(self):
        return self.__dict__.get('url', '')

    def register_resource(self, path, resource):
        self.base.putChild(path, resource)

    def start(self):
        interface = WebServerConfig.local_ip
        port = WebServerConfig.local_port
        cert_path = WebServerConfig.certificate.normalized if WebServerConfig.certificate else None
        cert_chain_path = WebServerConfig.certificate_chain.normalized if WebServerConfig.certificate_chain else None
        if cert_path is not None:
            if not os.path.isfile(cert_path):
                log.error('Certificate file %s could not be found' % cert_path)
                return
            try:
                ssl_ctx_factory = DefaultOpenSSLContextFactory(cert_path, cert_path)
            except Exception:
                log.exception('Creating TLS context')
                return
            if cert_chain_path is not None:
                if not os.path.isfile(cert_chain_path):
                    log.error('Certificate chain file %s could not be found' % cert_chain_path)
                    return
                ssl_ctx = ssl_ctx_factory.getContext()
                try:
                    ssl_ctx.use_certificate_chain_file(cert_chain_path)
                except Exception:
                    log.exception('Setting TLS certificate chain file')
                    return
            self.listener = reactor.listenSSL(port, self.site, ssl_ctx_factory, backlog=511, interface=interface)
            scheme = 'https'
        else:
            self.listener = reactor.listenTCP(port, self.site, backlog=511, interface=interface)
            scheme = 'http'
        port = self.listener.getHost().port
        self.__dict__['url'] = '%s://%s:%d' % (scheme, WebServerConfig.hostname or interface.normalized, WebServerConfig.public_port or port)
        log.info('Web server listening for requests on: %s' % self.url)

    def stop(self):
        if self.listener is not None:
            self.listener.stopListening()


server = WebServer()
