
import json
import os
import re
from binascii import b2a_hex as hex_encode

from application.notification import (IObserver, NotificationCenter,
                                      NotificationData)
from application.python import Null
from application.python.types import Singleton
from autobahn.exception import Disconnected
from autobahn.twisted.websocket import (WebSocketClientFactory,
                                        WebSocketClientProtocol, connectWS)
from eventlib.twistedutil import block_on
from twisted.internet import defer, reactor
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python.failure import Failure
from zope.interface import implementer

from sylk import __version__

from .configuration import JanusConfig
from .logger import log
from .models import janus


class JanusError(Exception):
    def __init__(self, code, reason):
        super(JanusError, self).__init__(reason)
        self.code = code
        self.reason = reason


_pstn_uri_re = re.compile(r'^(?:sips?:)?(?:\+|00)\d+@')


def _is_pstn_uri(uri):
    return bool(_pstn_uri_re.match(uri))


class JanusClientProtocol(WebSocketClientProtocol):
    _event_handlers = None
    _pending_transactions = None
    _keepalive_timers = None
    _keepalive_interval = 45
    _connection_lost = False

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

        self.notification_center.post_notification('WebRTCJanusTrace', sender=self, data=NotificationData(direction='INCOMING', message=payload.decode(), peer=self.peer))

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
            if isinstance(plugin_data, (janus.SIPErrorEvent, janus.NoSIPErrorEvent, janus.VideoroomErrorEvent)):
                deferred.errback(JanusError(plugin_data.error_code, plugin_data.error))
            else:
                deferred.callback(message)

    def connectionLost(self, reason):
        super(JanusClientProtocol, self).connectionLost(reason)
        self._connection_lost = True
        # The Janus sessions died with the connection (e.g. Janus restarted):
        # stop their keepalives and fail the requests still waiting for an
        # answer, so nothing sends on, or waits for, the closed connection.
        for timer in (self._keepalive_timers or {}).values():
            if timer.active():
                timer.cancel()
        self._keepalive_timers = {}
        pending, self._pending_transactions = self._pending_transactions or {}, {}
        for request, deferred in pending.values():
            deferred.errback(JanusError(503, 'Janus connection lost'))
        self.notification_center.post_notification('JanusBackendDisconnected', sender=self, data=NotificationData(reason=reason.getErrorMessage()))

    def disconnect(self, code=1000, reason=''):
        self.sendClose(code, reason)

    def _send_request(self, request):
        if request.janus != 'keepalive' and 'session_id' in request:  # postpone keepalive messages as long as we have non-keepalive traffic for a given session
            keepalive_timer = self._keepalive_timers.get(request.session_id, None)
            if keepalive_timer is not None and keepalive_timer.active():
                keepalive_timer.reset(self._keepalive_interval)
        if self._connection_lost or self._pending_transactions is None:
            return defer.fail(JanusError(503, 'Janus connection is not available'))
        deferred = defer.Deferred()
        message = json.dumps(request.__data__)
        self.notification_center.post_notification('WebRTCJanusTrace', sender=self, data=NotificationData(direction='OUTGOING', message=message, peer=self.peer))
        try:
            self.sendMessage(message.encode())
        except Disconnected:
            return defer.fail(JanusError(503, 'Janus connection is not available'))
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
            self._keepalive_timers.pop(session_id, None)
        elif self._connection_lost:
            pass
        else:
            self._keepalive_timers[session_id] = reactor.callLater(self._keepalive_interval, self._send_keepalive, session_id)

    # Public API

    def set_event_handler(self, handle_id, event_handler):
        if event_handler is None:
            self._event_handlers.pop(handle_id, None)
            log.debug("Destroy Janus session, %d handlers in use" % len(list(self._event_handlers.keys())));
        else:
            assert callable(event_handler)
            self._event_handlers[handle_id] = event_handler
            log.debug("Create Janus session, %d handlers in use" % len(list(self._event_handlers.keys())));

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

    def message(self, session_id, handle_id, body, jsep=None, transaction=None):
        kw = dict(session_id=session_id, handle_id=handle_id, body=body)
        if jsep is not None:
            kw['jsep'] = jsep
        if transaction is not None:
            kw['transaction'] = transaction
        return self._send_request(janus.MessageRequest(**kw))

    def trickle(self, session_id, handle_id, candidates):
        return self._send_request(janus.TrickleRequest(session_id=session_id, handle_id=handle_id, candidates=candidates))


class JanusClientFactory(ReconnectingClientFactory, WebSocketClientFactory):
    noisy = False
    protocol = JanusClientProtocol


@implementer(IObserver)
class JanusBackend(object, metaclass=Singleton):

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

    def message(self, session_id, handle_id, body, jsep=None, transaction=None):
        return self.protocol.message(session_id, handle_id, body, jsep, transaction=transaction)

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

    def message(self, body, jsep=None, _async=False):
        deferred = self.backend.message(self.session.id, self.id, body, jsep)
        return deferred if _async else block_on(deferred)

    def trickle(self, candidates, _async=False):
        deferred = self.backend.trickle(self.session.id, self.id, candidates)
        return deferred if _async else block_on(deferred)


class GenericPluginHandle(JanusPluginHandle):
    def __init__(self, plugin, session, event_handler):
        self.plugin = plugin
        super(GenericPluginHandle, self).__init__(session, event_handler)


class SIPPluginHandle(JanusPluginHandle):
    plugin = 'janus.plugin.sip'

    def register(self, account, proxy=None):
        send_register = True if account.auth_handle.type == 'SIP' else False
        force_tcp = False
        if proxy and ('transport=tcp' in proxy or 'sips' in proxy):
            force_tcp = True
        self.message(janus.SIPRegister(proxy=proxy, outbound_proxy=proxy, send_register=send_register, force_tcp=force_tcp,
                                       **account.user_data))

    def register_helper(self, account, master_id=0):
        self.message(janus.SIPRegisterHelper(master_id=master_id, **account.user_data))

    def unregister(self):
        self.message(janus.SIPUnregister())

    def call(self, account, uri, sdp, headers=None, proxy=None):
        headers = {'headers': headers} if headers is not None else {}

        force_tcp = False
        if proxy and ('transport=tcp' in proxy or 'sips' in proxy):
            force_tcp = True

        call_kwargs = dict(uri=uri, **headers)
        if not _is_pstn_uri(uri):
            call_kwargs['srtp'] = 'sdes_optional'

        self.message(janus.SIPRegister(proxy=proxy, outbound_proxy=proxy, send_register=False, force_tcp=force_tcp, **account.user_data))
        self.message(janus.SIPCall(**call_kwargs), jsep=janus.SDPOffer(sdp=sdp))

    def accept(self, sdp, headers=None):
        headers = {'headers': headers} if headers is not None else {}
        self.message(janus.SIPAccept(**headers), jsep=janus.SDPAnswer(sdp=sdp))

    def update(self, sdp, jsep_type='offer', headers=None):
        kwargs = {'headers': headers} if headers is not None else {}
        if jsep_type == 'offer':
            jsep = janus.SDPOffer(sdp=sdp)
        elif jsep_type == 'answer':
            jsep = janus.SDPAnswer(sdp=sdp)
        else:
            raise ValueError('jsep_type must be "offer" or "answer", got %r' % jsep_type)
        self.message(janus.SIPUpdate(**kwargs), jsep=jsep)

    def decline(self, code=486):
        self.message(janus.SIPDecline(code=code))

    def hangup(self):
        self.message(janus.SIPHangup())

    def send_message(self, **message):
        self.message(janus.SIPMessage(**message))

    def send_dtmf_info(self, digit, duration=None):
        parameters = {'digit': digit}
        if duration is not None:
            parameters['duration'] = duration
        self.message(janus.SIPDtmfInfo(**parameters))


class NoSIPPluginHandle(JanusPluginHandle):
    """
    Janus NoSIP plugin: relays media between the WebRTC PeerConnection of
    the handle and a plain RTP/SRTP peer, leaving the signalling to us.

    The plugin requests are asynchronous: Janus acks them and later sends
    the result as an event carrying the request's transaction. generate()
    and process() wait for that event (they block the calling green
    thread), so they can be used like normal function calls. All other
    events (webrtcup, media, slowlink, hangup, ...) go to event_handler.
    """

    plugin = 'janus.plugin.nosip'
    request_timeout = 10

    def __init__(self, session, event_handler):
        self._pending_requests = {}  # transaction -> Deferred waiting for the result event
        self._event_handler = event_handler
        super(NoSIPPluginHandle, self).__init__(session, self._handle_event)

    def generate(self, sdp, sdp_type, srtp=None, info=None):
        """
        Convert a WebRTC offer/answer (JSEP) into a plain SDP for the remote
        party. Returns janus.NoSIPResultGenerated (.type and .sdp). Janus
        flags renegotiations of an established PeerConnection by itself.
        """
        request = janus.NoSIPGenerate(**_present(srtp=srtp, info=info))
        jsep = janus.SDPOffer(sdp=sdp) if sdp_type == 'offer' else janus.SDPAnswer(sdp=sdp)
        return self._request(request, jsep=jsep).plugindata.data.result

    def process(self, sdp, sdp_type, srtp=None, update=None, info=None):
        """
        Convert a plain SDP offer/answer from the remote party into a JSEP
        for the WebRTC side. Returns (janus.NoSIPResultProcessed, JSEP).
        """
        request = janus.NoSIPProcess(type=sdp_type, sdp=sdp, **_present(srtp=srtp, update=update, info=info))
        event = self._request(request)
        return event.plugindata.data.result, event.jsep

    def hangup(self):
        self._request(janus.NoSIPHangup())

    def keyframe(self, user=False, peer=False):
        self._request(janus.NoSIPKeyframe(user=user, peer=peer))

    def detach(self):
        super(NoSIPPluginHandle, self).detach()
        self._fail_pending(JanusError(499, 'NoSIP handle detached'))

    def _request(self, body, jsep=None):
        # should only be called from a green thread
        transaction = hex_encode(os.urandom(16)).decode()
        result = defer.Deferred()
        self._pending_requests[transaction] = result
        timer = reactor.callLater(self.request_timeout, self._timeout_request, transaction, body)

        def stop_timer(value):
            if timer.active():
                timer.cancel()
            return value

        def request_failed(failure):
            deferred = self._pending_requests.pop(transaction, None)
            if deferred is not None:
                deferred.errback(failure)

        result.addBoth(stop_timer)
        self.backend.message(self.session.id, self.id, body, jsep, transaction=transaction).addErrback(request_failed)
        return block_on(result)

    def _timeout_request(self, transaction, body):
        deferred = self._pending_requests.pop(transaction, None)
        if deferred is not None:
            deferred.errback(JanusError(408, 'NoSIP {} request timed out'.format(body.__data__.get('request'))))

    def _fail_pending(self, error):
        pending, self._pending_requests = self._pending_requests, {}
        for deferred in pending.values():
            deferred.errback(error)

    def _handle_event(self, event):
        transaction = getattr(event, 'transaction', None)
        deferred = self._pending_requests.pop(transaction, None) if transaction is not None else None
        if deferred is not None and isinstance(event, janus.PluginEvent):
            data = event.plugindata.data
            if isinstance(data, janus.NoSIPErrorEvent):
                deferred.errback(JanusError(data.error_code, data.error))
            else:
                deferred.callback(event)
        elif self._event_handler is not None:
            self._event_handler(event)


def _present(**kw):
    return {name: value for name, value in kw.items() if value is not None}


class VideoroomPluginHandle(JanusPluginHandle):
    plugin = 'janus.plugin.videoroom'

    def create(self, room, config, publishers=10):
        self.message(janus.VideoroomCreate(room=room, publishers=publishers, **config.janus_data))

    def destroy(self, room):
        try:
            self.message(janus.VideoroomDestroy(room=room))
        except JanusError as e:
            log.warning('could not destroy video room %s: %s', room, e)

    def join(self, room, sdp, audio, video, display_name=None):
        if display_name:
            self.message(janus.VideoroomJoin(room=room, audio=audio, video=video, display=display_name), jsep=janus.SDPOffer(sdp=sdp))
        else:
            self.message(janus.VideoroomJoin(room=room, audio=audio, video=video), jsep=janus.SDPOffer(sdp=sdp))

    def leave(self):
        self.message(janus.VideoroomLeave())

    def update_publisher(self, options):
        self.message(janus.VideoroomUpdatePublisher(**options))

    def kick_publisher(self, room, publisher_id):
        # Moderation-level kick: any publisher's plugin handle in a
        # room without a secret can ask Janus to remove another
        # publisher from the same room. Used by the gateway's
        # videoroom-remove handler when the target is a WebRTC
        # participant (vs the SIP REFER ;method=BYE path that's used
        # for SIP-side participants). Fire-and-forget — Janus will
        # emit a "kicked" event on the kicked publisher's handle.
        self.message(janus.VideoroomKick(room=room, id=publisher_id))

    def feed_attach(self, room, feed, offer_audio, offer_video):
        self.message(janus.VideoroomFeedAttach(room=room, feed=feed, offer_audio=offer_audio, offer_video=offer_video))

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
