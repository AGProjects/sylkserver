
import json

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from autobahn.twisted.websocket import connectWS, WebSocketClientFactory, WebSocketClientProtocol
from eventlib.twistedutil import block_on
from twisted.internet import reactor, defer
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python.failure import Failure
from zope.interface import implements

from sylk import __version__
from .configuration import JanusConfig
from .logger import log
from .models import janus


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
            message = janus.JanusMessage.from_payload(json.loads(payload))
        except Exception as e:
            log.warning('Error decoding Janus message: {!s}'.format(e))
            return

        if isinstance(message, (janus.CoreEvent, janus.PluginEvent)):
            # some of the plugin events might have the transaction, but we do not finalize
            # the transaction for them as they are not direct responses for the transaction
            handler = self._event_handlers.get(message.sender, Null)
            try:
                handler(message)
            except Exception as e:
                log.exception('Error while running Janus event handler: {!s}'.format(e))
            return

        # at this point it can only be a response. clear the transaction and return the answer.
        try:
            request, deferred = self._pending_transactions.pop(message.transaction)
        except KeyError:
            log.warn('Discarding unexpected response: %s' % payload)
            return

        if isinstance(message, janus.AckResponse):
            deferred.callback(None)
        elif isinstance(message, janus.SuccessResponse):
            deferred.callback(message)
        elif isinstance(message, janus.ErrorResponse):
            deferred.errback(JanusError(message.error.code, message.error.reason))
        else:
            assert isinstance(message, janus.PluginResponse)
            plugin_data = message.plugindata.data
            if isinstance(plugin_data, (janus.SIPErrorEvent, janus.VideoroomErrorEvent)):
                deferred.errback(JanusError(plugin_data.error_code, plugin_data.error))
            else:
                deferred.callback(message)

    def connectionLost(self, reason):
        super(JanusClientProtocol, self).connectionLost(reason)
        self.notification_center.post_notification('JanusBackendDisconnected', sender=self, data=NotificationData(reason=reason.getErrorMessage()))

    def disconnect(self, code=1000, reason=u''):
        self.sendClose(code, reason)

    def _send_request(self, request):
        if request.janus != 'keepalive' and 'session_id' in request:  # postpone keepalive messages as long as we have non-keepalive traffic for a given session
            keepalive_timer = self._keepalive_timers.get(request.session_id, None)
            if keepalive_timer is not None and keepalive_timer.active():
                keepalive_timer.reset(self._keepalive_interval)
        deferred = defer.Deferred()
        message = json.dumps(request.__data__)
        self.notification_center.post_notification('WebRTCJanusTrace', sender=self, data=NotificationData(direction='OUTGOING', message=message, peer=self.peer))
        self.sendMessage(message)
        self._pending_transactions[request.transaction] = request, deferred
        return deferred

    def _start_keepalive(self, response):
        session_id = response.data.id
        self._keepalive_timers[session_id] = reactor.callLater(self._keepalive_interval, self._send_keepalive, session_id)
        return response

    def _stop_keepalive(self, session_id):
        timer = self._keepalive_timers.pop(session_id, None)
        if timer is not None and timer.active():
            timer.cancel()

    def _send_keepalive(self, session_id):
        deferred = self._send_request(janus.SessionKeepaliveRequest(session_id=session_id))
        deferred.addBoth(self._keepalive_callback, session_id)

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
        return self._send_request(janus.InfoRequest())

    def create_session(self):
        return self._send_request(janus.SessionCreateRequest()).addCallback(self._start_keepalive)

    def destroy_session(self, session_id):
        self._stop_keepalive(session_id)
        return self._send_request(janus.SessionDestroyRequest(session_id=session_id))

    def attach_plugin(self, session_id, plugin):
        return self._send_request(janus.PluginAttachRequest(session_id=session_id, plugin=plugin))

    def detach_plugin(self, session_id, handle_id):
        return self._send_request(janus.PluginDetachRequest(session_id=session_id, handle_id=handle_id))

    def message(self, session_id, handle_id, body, jsep=None):
        if jsep is not None:
            return self._send_request(janus.MessageRequest(session_id=session_id, handle_id=handle_id, body=body, jsep=jsep))
        else:
            return self._send_request(janus.MessageRequest(session_id=session_id, handle_id=handle_id, body=body))

    def trickle(self, session_id, handle_id, candidates):
        return self._send_request(janus.TrickleRequest(session_id=session_id, handle_id=handle_id, candidates=candidates))


class JanusClientFactory(ReconnectingClientFactory, WebSocketClientFactory):
    noisy = False
    protocol = JanusClientProtocol


class JanusBackend(object):
    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):
        self.factory = JanusClientFactory(url=JanusConfig.api_url, protocols=['janus-protocol'], useragent='SylkServer/%s' % __version__)
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

    def attach_plugin(self, session_id, plugin):
        return self.protocol.attach_plugin(session_id, plugin)

    def detach_plugin(self, session_id, handle_id):
        return self.protocol.detach_plugin(session_id, handle_id)

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


class JanusSession(object):
    backend = JanusBackend()

    def __init__(self):
        response = block_on(self.backend.create_session())  # type: janus.SuccessResponse
        self.id = response.data.id

    def destroy(self):
        return self.backend.destroy_session(self.id)


class JanusPluginHandle(object):
    backend = JanusBackend()
    plugin = None

    def __init__(self, session, event_handler):
        if self.plugin is None:
            raise TypeError('Cannot instantiate {0.__class__.__name__} with no associated plugin'.format(self))
        response = block_on(self.backend.attach_plugin(session.id, self.plugin))  # type: janus.SuccessResponse
        self.id = response.data.id
        self.session = session
        self.backend.set_event_handler(self.id, event_handler)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.detach()

    def detach(self):
        try:
            block_on(self.backend.detach_plugin(self.session.id, self.id))
        except JanusError as e:
            log.warning('could not detach Janus plugin: %s', e)
        self.backend.set_event_handler(self.id, None)

    def message(self, body, jsep=None, async=False):
        deferred = self.backend.message(self.session.id, self.id, body, jsep)
        return deferred if async else block_on(deferred)

    def trickle(self, candidates, async=False):
        deferred = self.backend.trickle(self.session.id, self.id, candidates)
        return deferred if async else block_on(deferred)


class GenericPluginHandle(JanusPluginHandle):
    def __init__(self, plugin, session, event_handler):
        self.plugin = plugin
        super(GenericPluginHandle, self).__init__(session, event_handler)


class SIPPluginHandle(JanusPluginHandle):
    plugin = 'janus.plugin.sip'

    def register(self, account, proxy=None):
        self.message(janus.SIPRegister(proxy=proxy, **account.user_data))

    def unregister(self):
        self.message(janus.SIPUnregister())

    def call(self, account, uri, sdp, proxy=None):
        # in order to make a call we need to register first. do so without actually registering, as we are already registered
        self.message(janus.SIPRegister(proxy=proxy, send_register=False, **account.user_data))
        self.message(janus.SIPCall(uri=uri, srtp='sdes_optional'), jsep=janus.SDPOffer(sdp=sdp))

    def accept(self, sdp):
        self.message(janus.SIPAccept(), jsep=janus.SDPAnswer(sdp=sdp))

    def decline(self, code=486):
        self.message(janus.SIPDecline(code=code))

    def hangup(self):
        self.message(janus.SIPHangup())


class VideoroomPluginHandle(JanusPluginHandle):
    plugin = 'janus.plugin.videoroom'

    def create(self, room, config, publishers=10):
        self.message(janus.VideoroomCreate(room=room, publishers=publishers, **config.janus_data))

    def destroy(self, room):
        try:
            self.message(janus.VideoroomDestroy(room=room))
        except JanusError as e:
            log.warning('could not destroy video room %s: %s', room, e)

    def join(self, room, sdp, display_name=None):
        if display_name:
            self.message(janus.VideoroomJoin(room=room, display=display_name), jsep=janus.SDPOffer(sdp=sdp))
        else:
            self.message(janus.VideoroomJoin(room=room), jsep=janus.SDPOffer(sdp=sdp))

    def leave(self):
        self.message(janus.VideoroomLeave())

    def update_publisher(self, options):
        self.message(janus.VideoroomUpdatePublisher(**options))

    def feed_attach(self, room, feed):
        self.message(janus.VideoroomFeedAttach(room=room, feed=feed))

    def feed_detach(self):
        self.message(janus.VideoroomFeedDetach())

    def feed_start(self, sdp):
        self.message(janus.VideoroomFeedStart(), jsep=janus.SDPAnswer(sdp=sdp))

    def feed_pause(self):
        self.message(janus.VideoroomFeedPause())

    def feed_resume(self):
        self.message(janus.VideoroomFeedStart())

    def feed_update(self, options):
        self.message(janus.VideoroomFeedUpdate(**options))
