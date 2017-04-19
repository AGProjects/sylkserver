
import functools
import json
import uuid

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from autobahn.twisted.websocket import connectWS, WebSocketClientFactory, WebSocketClientProtocol
from sipsimple.util import ISOTimestamp
from twisted.internet import reactor, defer
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python.failure import Failure
from zope.interface import implements

from sylk import __version__ as SYLK_VERSION
from sylk.applications.webrtcgateway.configuration import JanusConfig
from sylk.applications.webrtcgateway.janus.logger import Logger as JanusLogger
from sylk.applications.webrtcgateway.logger import log


class JanusRequest(object):
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
    _janus_event_handlers = None
    _janus_pending_transactions = None
    _janus_keepalive_timers = None
    _janus_keepalive_interval = 45

    def onOpen(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('JanusBackendConnected', sender=self)
        self._janus_pending_transactions = {}
        self._janus_keepalive_timers = {}
        self._janus_event_handlers = {}

    def onMessage(self, payload, isBinary):
        if isBinary:
            log.warn('Unexpected binary payload received')
            return
        if JanusConfig.trace_janus:
            self.factory.janus_logger.msg("IN", ISOTimestamp.now(), payload)
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
            handler = self._janus_event_handlers.get(handle_id, Null)
            try:
                handler(handle_id, message_type, data)
            except Exception:
                log.err()
            return
        try:
            req, d = self._janus_pending_transactions.pop(transaction_id)
        except KeyError:
            log.warn('Discarding unexpected response: %s' % payload)
            return
        if message_type == 'error':
            code = data['error']['code']
            reason = data['error']['reason']
            d.errback(JanusError(code, reason))
        else:
            if req.type == 'info':
                result = data
            elif req.type == 'create':
                result = data['data']['id']
            elif req.type == 'destroy':
                result = None
            elif req.type == 'attach':
                result = data['data']['id']
            elif req.type == 'detach':
                result = None
            elif req.type == 'keepalive':
                result = None
            elif req.type == 'ack':
                result = None
            else:
                result = data
            d.callback(result)

    def connectionLost(self, reason):
        super(JanusClientProtocol, self).connectionLost(reason)
        notification_center = NotificationCenter()
        notification_center.post_notification('JanusBackendDisconnected', sender=self, data=NotificationData(reason=reason.getErrorMessage()))

    def disconnect(self, code=1000, reason=u''):
        self.sendClose(code, reason)

    def janus_set_event_handler(self, handle_id, event_handler):
        if event_handler is None:
            self._janus_event_handlers.pop(handle_id, None)
        else:
            assert callable(event_handler)
            self._janus_event_handlers[handle_id] = event_handler

    def _janus_send_request(self, req):
        data = json.dumps(req.as_dict())
        if JanusConfig.trace_janus:
            self.factory.janus_logger.msg("OUT", ISOTimestamp.now(), data)
        self.sendMessage(data)
        d = defer.Deferred()
        self._janus_pending_transactions[req.transaction_id] = (req, d)
        return d

    def janus_info(self):
        req = JanusRequest('info')
        return self._janus_send_request(req)

    def janus_create_session(self):
        req = JanusRequest('create')
        return self._janus_send_request(req)

    def janus_destroy_session(self, session_id):
        req = JanusRequest('destroy', session_id=session_id)
        return self._janus_send_request(req)

    def janus_attach(self, session_id, plugin):
        req = JanusRequest('attach', session_id=session_id, plugin=plugin)
        return self._janus_send_request(req)

    def janus_detach(self, session_id, handle_id):
        req = JanusRequest('detach', session_id=session_id, handle_id=handle_id)
        return self._janus_send_request(req)

    def janus_message(self, session_id, handle_id, body, jsep=None):
        req = JanusRequest('message', session_id=session_id, handle_id=handle_id, body=body)
        if jsep is not None:
            req.jsep = jsep
        return self._janus_send_request(req)

    def janus_trickle(self, session_id, handle_id, candidates):
        req = JanusRequest('trickle', session_id=session_id, handle_id=handle_id)
        if candidates:
            if len(candidates) == 1:
                req.candidate = candidates[0]
            else:
                req.candidates = candidates
        else:
            req.candidate = {'completed': True}
        return self._janus_send_request(req)

    def _janus_keepalive_cb(self, session_id, result):
        if isinstance(result, Failure):
            self.janus_stop_keepalive(session_id)
            return
        self._janus_keepalive_timers[session_id] = reactor.callLater(self._janus_keepalive_interval, self._janus_send_keepalive, session_id)

    def _janus_send_keepalive(self, session_id):
        req = JanusRequest('keepalive', session_id=session_id)
        d = self._janus_send_request(req)
        d.addBoth(functools.partial(self._janus_keepalive_cb, session_id))
        return d

    def janus_start_keepalive(self, session_id):
        self.janus_stop_keepalive(session_id)
        self._janus_keepalive_timers[session_id] = reactor.callLater(self._janus_keepalive_interval, self._janus_send_keepalive, session_id)

    def janus_stop_keepalive(self, session_id):
        timer = self._janus_keepalive_timers.pop(session_id, None)
        if timer is not None and timer.active():
            timer.cancel()


class JanusClientFactory(ReconnectingClientFactory, WebSocketClientFactory):
    noisy = False
    protocol = JanusClientProtocol


class JanusBackend(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        self.janus_logger = JanusLogger()
        self.factory = JanusClientFactory(url=JanusConfig.api_url,
                                          protocols=['janus-protocol'],
                                          useragent='SylkServer/%s' % SYLK_VERSION)
        self.factory.janus_logger = self.janus_logger
        self.connector = None
        self.connection = Null
        self._stopped = False

    def __getattr__(self, attr):
        if attr.startswith('janus_'):
            return getattr(self.connection, attr)
        return self.attr

    @property
    def ready(self):
        return self.connection is not Null

    def start(self):
        self.janus_logger.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='JanusBackendConnected')
        notification_center.add_observer(self, name='JanusBackendDisconnected')
        self.connector = connectWS(self.factory)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.janus_logger.stop()
        self.factory.stopTrying()
        notification_center = NotificationCenter()
        notification_center.discard_observer(self, name='JanusBackendConnected')
        notification_center.discard_observer(self, name='JanusBackendDisconnected')
        if self.connector is not None:
            self.connector.disconnect()
            self.connector = None
        if self.connection is not None:
            self.connection.disconnect()
            self.connection = Null

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_JanusBackendConnected(self, notification):
        assert self.connection is Null
        self.connection = notification.sender
        log.msg('Janus backend connection up')
        self.factory.resetDelay()

    def _NH_JanusBackendDisconnected(self, notification):
        log.msg('Janus backend connection down: %s' % notification.data.reason)
        self.connection = Null

