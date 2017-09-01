
import json
import uuid

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from autobahn.twisted.websocket import connectWS, WebSocketClientFactory, WebSocketClientProtocol
from twisted.internet import reactor, defer
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python.failure import Failure
from zope.interface import implements

from sylk import __version__ as SYLK_VERSION
from sylk.applications.webrtcgateway.configuration import JanusConfig
from sylk.applications.webrtcgateway.logger import log


class Request(object):
    def __init__(self, request_type, **kwargs):
        self.janus = request_type
        self.transaction = uuid.uuid4().hex
        if JanusConfig.api_secret:
            self.apisecret = JanusConfig.api_secret
        self.__dict__.update(**kwargs)

    @property
    def type(self):
        return self.janus

    @property
    def transaction_id(self):
        return self.transaction

    def as_dict(self):
        return self.__dict__.copy()


class JanusError(Exception):
    def __init__(self, code, reason):
        super(JanusError, self).__init__(reason)
        self.code = code
        self.reason = reason


class JanusClientProtocol(WebSocketClientProtocol):
    _event_handlers = None
    _pending_transactions = None
    _keepalive_timers = None
    _keepalive_interval = 45

    notification_center = NotificationCenter()

    def onOpen(self):
        self.notification_center.post_notification('JanusBackendConnected', sender=self)
        self._pending_transactions = {}
        self._keepalive_timers = {}
        self._event_handlers = {}

    def onMessage(self, payload, isBinary):
        if isBinary:
            log.warn('Unexpected binary payload received')
            return
        self.notification_center.post_notification('WebRTCJanusTrace', sender=self, data=NotificationData(direction='INCOMING', message=payload, peer=self.peer))
        try:
            data = json.loads(payload)
        except Exception as e:
            log.warn('Error decoding payload: %s' % e)
            return
        try:
            message_type = data.pop('janus')
        except KeyError:
            log.warn('Received payload lacks message type: %s' % payload)
            return
        transaction_id = data.pop('transaction', None)
        if message_type == 'event' or transaction_id is None:
            # This is an event. Janus is not very consistent here, some 'events'
            # do have the transaction id set. So we check for the message type as well.
            handle_id = data.pop('sender', -1)
            handler = self._event_handlers.get(handle_id, Null)
            try:
                handler(handle_id, message_type, data)
            except Exception:
                log.exception()
            return
        try:
            request, deferred = self._pending_transactions.pop(transaction_id)
        except KeyError:
            log.warn('Discarding unexpected response: %s' % payload)
            return
        # events were handled above, so the only message types we get here are ack, success and error
        # todo: some plugin errors are delivered with message_type == 'success' and the error code is buried somewhere in plugindata
        if message_type == 'error':
            code = data['error']['code']
            reason = data['error']['reason']
            deferred.errback(JanusError(code, reason))
        elif message_type == 'ack':
            deferred.callback(None)
        else:  # success
            # keepalive and trickle only receive an ACK, thus are handled above in message_type == 'ack', not here
            if request.type in ('create', 'attach'):
                result = data['data']['id']
            elif request.type in ('destroy', 'detach'):
                result = None
            else:  # info, message (for synchronous message requests only)
                result = data
            deferred.callback(result)

    def connectionLost(self, reason):
        super(JanusClientProtocol, self).connectionLost(reason)
        self.notification_center.post_notification('JanusBackendDisconnected', sender=self, data=NotificationData(reason=reason.getErrorMessage()))

    def disconnect(self, code=1000, reason=u''):
        self.sendClose(code, reason)

    def _send_request(self, request):
        deferred = defer.Deferred()
        data = json.dumps(request.as_dict())
        self.notification_center.post_notification('WebRTCJanusTrace', sender=self, data=NotificationData(direction='OUTGOING', message=data, peer=self.peer))
        self.sendMessage(data)
        self._pending_transactions[request.transaction_id] = (request, deferred)
        return deferred

    def _start_keepalive(self, session_id):
        self._keepalive_timers[session_id] = reactor.callLater(self._keepalive_interval, self._send_keepalive, session_id)

    def _stop_keepalive(self, session_id):
        timer = self._keepalive_timers.pop(session_id, Null)
        timer.cancel()

    def _send_keepalive(self, session_id):
        request = Request('keepalive', session_id=session_id)
        deferred = self._send_request(request)
        deferred.addBoth(self._keepalive_callback, session_id)
        return deferred

    def _keepalive_callback(self, result, session_id):
        if isinstance(result, Failure):
            self._keepalive_timers.pop(session_id)
        else:
            self._keepalive_timers[session_id] = reactor.callLater(self._keepalive_interval, self._send_keepalive, session_id)

    # Public API

    def set_event_handler(self, handle_id, event_handler):
        if event_handler is None:
            self._event_handlers.pop(handle_id, None)
        else:
            assert callable(event_handler)
            self._event_handlers[handle_id] = event_handler

    def info(self):
        request = Request('info')
        return self._send_request(request)

    def create_session(self):
        request = Request('create')
        return self._send_request(request).addCallback(self._start_keepalive)

    def destroy_session(self, session_id):
        self._stop_keepalive(session_id)
        request = Request('destroy', session_id=session_id)
        return self._send_request(request)

    def attach(self, session_id, plugin):
        request = Request('attach', session_id=session_id, plugin=plugin)
        return self._send_request(request)

    def detach(self, session_id, handle_id):
        request = Request('detach', session_id=session_id, handle_id=handle_id)
        return self._send_request(request)

    def message(self, session_id, handle_id, body, jsep=None):
        request = Request('message', session_id=session_id, handle_id=handle_id, body=body)
        if jsep is not None:
            request.jsep = jsep
        return self._send_request(request)

    def trickle(self, session_id, handle_id, candidates):
        request = Request('trickle', session_id=session_id, handle_id=handle_id)
        if candidates:
            if len(candidates) == 1:
                request.candidate = candidates[0]
            else:
                request.candidates = candidates
        else:
            request.candidate = {'completed': True}
        return self._send_request(request)


class JanusClientFactory(ReconnectingClientFactory, WebSocketClientFactory):
    noisy = False
    protocol = JanusClientProtocol


class JanusBackend(object):
    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):
        self.factory = JanusClientFactory(url=JanusConfig.api_url, protocols=['janus-protocol'], useragent='SylkServer/%s' % SYLK_VERSION)
        self.connector = None
        self.protocol = Null
        self._stopped = False

    @property
    def ready(self):
        return self.protocol is not Null

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='JanusBackendConnected')
        notification_center.add_observer(self, name='JanusBackendDisconnected')
        self.connector = connectWS(self.factory)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.factory.stopTrying()
        notification_center = NotificationCenter()
        notification_center.discard_observer(self, name='JanusBackendConnected')
        notification_center.discard_observer(self, name='JanusBackendDisconnected')
        if self.connector is not None:
            self.connector.disconnect()
            self.connector = None
        if self.protocol is not None:
            self.protocol.disconnect()
            self.protocol = Null

    def set_event_handler(self, handle_id, event_handler):
        self.protocol.set_event_handler(handle_id, event_handler)

    def info(self):
        return self.protocol.info()

    def create_session(self):
        return self.protocol.create_session()

    def destroy_session(self, session_id):
        return self.protocol.destroy_session(session_id)

    def attach(self, session_id, plugin):
        return self.protocol.attach(session_id, plugin)

    def detach(self, session_id, handle_id):
        return self.protocol.detach(session_id, handle_id)

    def message(self, session_id, handle_id, body, jsep=None):
        return self.protocol.message(session_id, handle_id, body, jsep)

    def trickle(self, session_id, handle_id, candidates):
        return self.protocol.trickle(session_id, handle_id, candidates)

    # Notification handling

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_JanusBackendConnected(self, notification):
        assert self.protocol is Null
        self.protocol = notification.sender
        log.info('Janus backend connection up')
        self.factory.resetDelay()

    def _NH_JanusBackendDisconnected(self, notification):
        log.info('Janus backend connection down: %s' % notification.data.reason)
        self.protocol = Null
