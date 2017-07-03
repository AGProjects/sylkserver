
from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.account.bonjour import BonjourPresenceState
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import SylkApplication, ApplicationLogger
from sylk.bonjour import BonjourService
from sylk.configuration import ServerConfig


log = ApplicationLogger(__package__)


def format_identity(identity):
    if identity.display_name:
        return u'%s <sip:%s@%s>' % (identity.display_name, identity.uri.user, identity.uri.host)
    else:
        return u'sip:%s@%s' % (identity.uri.user, identity.uri.host)


class EchoApplication(SylkApplication):
    def __init__(self):
        self.bonjour_services = set()

    def start(self):
        if ServerConfig.enable_bonjour:
            application_map = dict((item.split(':')) for item in ServerConfig.application_map)
            for uri, app in application_map.iteritems():
                if app == 'echo':
                    service = BonjourService(service='sipuri', name='Echo Test', uri_user=uri, is_focus=False)
                    service.start()
                    service.presence_state = BonjourPresenceState('available', u'Call me to test your client')
                    self.bonjour_services.add(service)

    def stop(self):
        for service in self.bonjour_services:
            service.stop()
        self.bonjour_services.clear()

    def incoming_session(self, session):
        log.info(u'New incoming session %s from %s' % (session.call_id, format_identity(session.remote_identity)))
        audio_streams = [stream for stream in session.proposed_streams if stream.type=='audio']
        chat_streams = [stream for stream in session.proposed_streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            log.info(u'Session %s rejected: invalid media, only RTP audio and MSRP chat are supported' % session.call_id)
            session.reject(488)
            return
        if audio_streams:
            session.send_ring_indication()

        handler = EchoHandler(session, audio_streams[0] if audio_streams else None, chat_streams[0] if chat_streams else None)
        handler.run()

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, request, data):
        request.reject(405)


class EchoHandler(object):
    implements(IObserver)

    def __init__(self, session, audio_stream, chat_stream):
        self.session = session
        self.audio_stream = audio_stream
        self.chat_stream = chat_stream

        self.end_timer = None

    def run(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)

        streams = []
        if self.audio_stream is not None:
            streams.append(self.audio_stream)
        if self.chat_stream is not None:
            streams.append(self.chat_stream)
        reactor.callLater(2 if self.audio_stream is not None else 0, self._accept_session, self.session, streams)

    def _accept_session(self, session, streams):
        if session.state == 'incoming':
            session.accept(streams)

    def _make_audio_stream_echo(self, stream):
        if stream.producer_slot is not None and stream.consumer_slot is not None:
            # TODO: handle slot changes
            stream.bridge.remove(stream.device)
            stream.mixer.connect_slots(stream.producer_slot, stream.consumer_slot)

    def _cleanup(self):
        notification_center = NotificationCenter()

        notification_center.remove_observer(self, sender=self.session)
        self.session = None

        if self.audio_stream is not None:
            notification_center.discard_observer(self, sender=self.audio_stream)
            self.audio_stream = None

        if self.chat_stream is not None:
            notification_center.discard_observer(self, sender=self.chat_stream)
            self.chat_stream = None

        if self.end_timer is not None and self.end_timer.active():
            self.end_timer.cancel()
        self.end_timer = None

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        try:
            audio_stream = next(stream for stream in session.streams if stream.type == 'audio')
        except StopIteration:
            audio_stream = None
        try:
            chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
        except StopIteration:
            chat_stream = None
        log.info('Session %s started' % session.call_id)
        if audio_stream is not None:
            self._make_audio_stream_echo(audio_stream)
            notification.center.add_observer(self, sender=audio_stream)
        self.audio_stream = audio_stream
        if chat_stream is not None:
            notification.center.add_observer(self, sender=chat_stream)
        self.chat_stream = chat_stream
        self.end_timer = reactor.callLater(600, self.session.end)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.info('Session %s ended' % session.call_id)
        self._cleanup()

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        log.info(u'Session %s failed from %s' % (session.call_id, format_identity(session.remote_identity)))
        self._cleanup()

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            audio_streams = [stream for stream in notification.data.proposed_streams if stream.type=='audio']
            chat_streams = [stream for stream in notification.data.proposed_streams if stream.type=='chat']
            if not audio_streams and not chat_streams:
                session.reject_proposal()
                return
            streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
            session.accept_proposal(streams)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        session = notification.sender
        for stream in notification.data.added_streams:
            notification.center.add_observer(self, sender=stream)
            log.info(u'Session %s has added %s' % (session.call_id, stream.type))
            if stream.type == 'audio':
                self._make_audio_stream_echo(stream)
                self.audio_stream = stream
            elif stream.type == 'chat':
                self.chat_stream = stream

        for stream in notification.data.removed_streams:
            notification.center.remove_observer(self, sender=stream)
            log.info(u'Session %s has removed %s' % (session.call_id, stream.type))
            if stream.type == 'audio':
                self.audio_stream = None
            elif stream.type == 'chat':
                self.chat_stream = None

        if not session.streams:
            log.info(u'Session %s has removed all streams, session will be terminated' % session.call_id)
            session.end()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        notification.sender.reject_transfer(403)

    def _NH_AudioStreamGotDTMF(self, notification):
        log.info(u'Session %s received DTMF: %s' % (self.session.call_id, notification.data.digit))

    def _NH_ChatStreamGotMessage(self, notification):
        stream = notification.sender
        message = notification.data.message
        stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
        stream.send_message(message.content, message.content_type)

