# Copyright (C) 2015 AG Projects. See LICENSE for details.
#

__all__ = ['Klein', 'WebServer', 'server']

import os

from application import log
from application.python.types import Singleton
from gnutls.interfaces.twisted import X509Credentials
from twisted.internet import reactor
from twisted.web.resource import Resource
from twisted.web.server import Site

from sylk import __version__
from sylk.configuration import WebServerConfig
from sylk.tls import Certificate, PrivateKey
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
        self.listener = None

    @property
    def url(self):
        return self.__dict__.get('url', '')

    def register_resource(self, path, resource):
        self.base.putChild(path, resource)

    def start(self):
        interface = WebServerConfig.local_ip
        port = WebServerConfig.local_port
        if os.path.isfile(WebServerConfig.certificate):
            cert = Certificate(WebServerConfig.certificate.normalized)
            key = PrivateKey(WebServerConfig.certificate.normalized)
            credentials = X509Credentials(cert, key)
            self.listener = reactor.listenTLS(port, self.site, credentials, interface=interface)
            scheme = 'https'
        else:
            self.listener = reactor.listenTCP(port, self.site, interface=interface)
            scheme = 'http'
        port = self.listener.getHost().port
        self.__dict__['url'] = '%s://%s:%d' % (scheme, WebServerConfig.hostname or interface.normalized, port)
        log.msg('Web server listening for requests on: %s' % self.url)

    def stop(self):
        if self.listener is not None:
            self.listener.stopListening()

server = WebServer()

