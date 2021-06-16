
from application.notification import NotificationCenter, NotificationData
from twisted.internet import defer, reactor
from twisted.words.protocols.jabber import error, xmlstream
from twisted.words.protocols.jabber.jid import internJID
from wokkel.component import InternalComponent, Router
from wokkel.server import XMPPS2SServerFactory, DeferredS2SClientFactory


__all__ = 'SylkRouter', 'SylkInternalComponent'


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

        if timeout is not None:
            def onTimeout():
                del self._iqDeferreds[element['id']]
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

