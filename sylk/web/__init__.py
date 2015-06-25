# Copyright (C) 2015 AG Projects. See LICENSE for details.
#

__all__ = ['Klein', 'WebServer', 'server']

import os

from application import log
from application.python.types import Singleton
from twisted.internet import reactor
from twisted.internet.ssl import DefaultOpenSSLContextFactory
from twisted.web.resource import Resource
from twisted.web.server import Site

from sylk import __version__
from sylk.configuration import WebServerConfig
from sylk.web.klein import Klein

# Set the 'Server' header string which Twisted Web will use
import twisted.web.server
twisted.web.server.version = b'SylkServer/%s' % __version__


class RootResource(Resource):
    isLeaf = True

    def render_GET(self, request):
        request.setHeader('Content-Type', 'text/plain')
        return 'Welcome to SylkServer!'


class WebServer(object):
    __metaclass__ = Singleton

    def __init__(self):
        self.base = Resource()
        self.base.putChild('', RootResource())
        self.site = Site(self.base, logPath=os.devnull)
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
        if cert_path is not None:
            if not os.path.isfile(cert_path):
                log.error('Certificate file could not be found')
                return
            try:
                ssl_context = DefaultOpenSSLContextFactory(cert_path, cert_path)
            except Exception:
                log.exception('Creating SSL context')
                log.err()
                return
            self.listener = reactor.listenSSL(port, self.site, ssl_context, backlog=511, interface=interface)
            scheme = 'https'
        else:
            self.listener = reactor.listenTCP(port, self.site, backlog=511, interface=interface)
            scheme = 'http'
        port = self.listener.getHost().port
        self.__dict__['url'] = '%s://%s:%d' % (scheme, WebServerConfig.hostname or interface.normalized, port)
        log.msg('Web server listening for requests on: %s' % self.url)

    def stop(self):
        if self.listener is not None:
            self.listener.stopListening()

server = WebServer()

