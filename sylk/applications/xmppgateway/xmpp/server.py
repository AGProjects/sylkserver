
import binascii
import os

from application.notification import NotificationCenter, NotificationData
from twisted.internet import defer, reactor
from twisted.internet.ssl import CertificateOptions
from twisted.words.protocols.jabber import error, xmlstream
from twisted.words.protocols.jabber.jid import internJID
from twisted.words.protocols.jabber.xmlstream import TLSInitiatingInitializer
from wokkel.component import InternalComponent, Router
from wokkel.server import XMPPS2SServerFactory, DeferredS2SClientFactory, ServerService, XMPPServerConnectAuthenticator, initiateS2S

from sylk.applications.xmppgateway.logger import log


__all__ = 'SylkRouter', 'SylkInternalComponent', 'SylkServerService'


class SylkInternalComponent(InternalComponent):
    def __init__(self, *args, **kwargs):
        InternalComponent.__init__(self, *args, **kwargs)
        self._iqDeferreds = {}

    def startService(self):
        InternalComponent.startService(self)
        self.xmlstream.addObserver('/iq[@type="result"]', self._onIQResponse)
        self.xmlstream.addObserver('/iq[@type="error"]', self._onIQResponse)

    def stopService(self):
        InternalComponent.stopService(self)
        iqDeferreds = self._iqDeferreds
        self._iqDeferreds = {}
        for d in iqDeferreds.values():
            d.errback(xmlstream.TimeoutError("Shutting down"))

    def request(self, request):
        if request.stanzaKind != 'iq' or request.stanzaType not in ('get', 'set'):
            return defer.fail(ValueError("Not a request"))

        element = request.toElement()

        # Make sure we have a trackable id on the stanza
        if not request.stanzaID:
            element.addUniqueId()
            request.stanzaID = element['id']

        # Set up iq response tracking
        d = defer.Deferred()
        self._iqDeferreds[element['id']] = d

        timeout = getattr(request, 'timeout', None)
        # Always arm a timeout so the deferred (and the IQ element it holds) is
        # guaranteed to be removed from _iqDeferreds even if the remote peer
        # never answers -- otherwise unanswered S2S IQs (dead/slow federated
        # servers) accumulate in the dict forever and leak.
        if timeout is None:
            timeout = 60

        if timeout is not None:
            def onTimeout():
                self._iqDeferreds.pop(element['id'], None)
                d.errback(xmlstream.TimeoutError("IQ timed out"))

            call = reactor.callLater(timeout, onTimeout)

            def cancelTimeout(result):
                if call.active():
                    call.cancel()

                return result

            d.addBoth(cancelTimeout)
        self.send(element)
        return d

    def _onIQResponse(self, iq):
        try:
            d = self._iqDeferreds[iq["id"]]
        except KeyError:
            return

        del self._iqDeferreds[iq["id"]]
        iq.handled = True
        if iq['type'] == 'error':
            d.errback(error.exceptionFromStanza(iq))
        else:
            d.callback(iq)


class SylkRouter(Router):

    def route(self, stanza):
        """
        Route a stanza. (subclassed to avoid vebose logging)

        @param stanza: The stanza to be routed.
        @type stanza: L{domish.Element}.
        """
        destination = internJID(stanza['to'])

        if destination.host in self.routes:
            self.routes[destination.host].send(stanza)
        else:
            self.routes[None].send(stanza)


class TLSXMPPServerConnectAuthenticator(XMPPServerConnectAuthenticator):
    """
    Outgoing S2S authenticator that negotiates STARTTLS (when the peer offers
    it) before running server dialback.

    wokkel's stock outgoing authenticator only installs the dialback
    initializer, so a peer that advertises <starttls><required/></starttls>
    (e.g. Prosody) never receives a STARTTLS and times the stream out. Here a
    TLSInitiatingInitializer is inserted ahead of dialback. The TLS layer only
    provides encryption; the peer certificate is intentionally not verified,
    because S2S identity is established by dialback (the same way the inbound
    listener authenticates peers), so an unverified CertificateOptions is used.
    """

    def __init__(self, thisHost, otherHost, secret, configurationForTLS=None):
        XMPPServerConnectAuthenticator.__init__(self, thisHost, otherHost, secret)
        # IOpenSSLContextFactory presenting our certificate for thisHost, so the
        # peer can validate our identity after STARTTLS. When None (no TLS
        # configured), an anonymous context is used and only encryption is
        # provided (peers enforcing secure S2S auth will then reject us).
        self.configurationForTLS = configurationForTLS

    def associateWithStream(self, xs):
        XMPPServerConnectAuthenticator.associateWithStream(self, xs)
        # NOTE: configurationForTLS must be passed to the constructor; the
        # initializer reads the private self._configurationForTLS in onProceed,
        # so setting a public attribute afterwards has no effect and would make
        # it fall back to optionsForClientTLS(), which presents no client
        # certificate (causing peers with secure S2S auth to reject us).
        configuration = self.configurationForTLS or CertificateOptions()
        tls_initializer = TLSInitiatingInitializer(xs, required=False, configurationForTLS=configuration)
        # Run STARTTLS before the dialback initializer the base class installed.
        xs.initializers.insert(0, tls_initializer)
        log.info('Outgoing XMPP S2S to %s as %s: STARTTLS initializer installed (TLS-before-dialback active)' % (self.otherHost, self.thisHost))


class SylkServerService(ServerService):
    """
    ServerService that uses a STARTTLS-capable outgoing authenticator and logs
    each outgoing S2S attempt and its outcome, so failures surface with context
    instead of as an unhandled Deferred traceback.
    """

    # Set by XMPPManager to an SNIContextFactory so outgoing connections can
    # present the certificate matching the originating domain.
    ssl_context_factory = None

    def __init__(self, router, domain=None, secret=None):
        # wokkel auto-generates the dialback secret with binascii.hexlify(),
        # which returns bytes on Python 3, but generateKey() does
        # secret.encode('ascii'), raising "'bytes' object has no attribute
        # 'encode'". Ensure the shared dialback secret is always a str.
        if secret is None:
            secret = binascii.hexlify(os.urandom(16)).decode('ascii')
        elif isinstance(secret, bytes):
            secret = secret.decode('ascii')
        ServerService.__init__(self, router, domain=domain, secret=secret)

    def initiateOutgoingStream(self, thisHost, otherHost):
        def resetConnecting(result):
            self._outgoingConnecting.discard((thisHost, otherHost))
            return result

        if (thisHost, otherHost) in self._outgoingConnecting:
            return

        log.info('Initiating outgoing XMPP S2S stream from %s to %s' % (thisHost, otherHost))

        configurationForTLS = None
        if self.ssl_context_factory is not None:
            configurationForTLS = self.ssl_context_factory.client_context_factory(thisHost)
        authenticator = TLSXMPPServerConnectAuthenticator(thisHost, otherHost, self.secret, configurationForTLS=configurationForTLS)
        factory = DeferredS2SClientFactory(authenticator)
        factory.addBootstrap(xmlstream.STREAM_AUTHD_EVENT, self.outgoingInitialized)
        factory.logTraffic = self.logTraffic

        self._outgoingConnecting.add((thisHost, otherHost))

        d = initiateS2S(factory)
        d.addBoth(resetConnecting)
        d.addCallbacks(self._outgoingStreamEstablished, self._outgoingStreamFailed,
                       callbackArgs=(thisHost, otherHost), errbackArgs=(thisHost, otherHost))
        return d

    def _outgoingStreamEstablished(self, xs, thisHost, otherHost):
        log.info('Outgoing XMPP S2S stream from %s to %s established' % (thisHost, otherHost))
        return xs

    def _outgoingStreamFailed(self, failure, thisHost, otherHost):
        # Drop any stanzas queued for a connection that could not be established
        # and log the reason, instead of leaving an unhandled Deferred error.
        self._outgoingQueues.pop((thisHost, otherHost), None)
        log.error('Outgoing XMPP S2S stream from %s to %s failed: %s' %
                  (thisHost, otherHost, failure.getErrorMessage()))


class LoggingXMLStream(xmlstream.XmlStream):
    notification_center = NotificationCenter()

    def __init__(self, *args, **kw):
        xmlstream.XmlStream.__init__(self, *args, **kw)
        self.rawDataInFn = self._log_incoming_message
        self.rawDataOutFn = self._log_outgoing_message

    def _log_incoming_message(self, message):
        self.notification_center.post_notification('XMPPMessageTrace', sender=self, data=NotificationData(direction='INCOMING', message=message))

    def _log_outgoing_message(self, message):
        self.notification_center.post_notification('XMPPMessageTrace', sender=self, data=NotificationData(direction='OUTGOING', message=message))


# Modify wokkel's factories to not be noisy and to use our logging protocol
XMPPS2SServerFactory.noisy = False
XMPPS2SServerFactory.protocol = LoggingXMLStream

DeferredS2SClientFactory.noisy = False
DeferredS2SClientFactory.protocol = LoggingXMLStream

