
import os

from application.python import Null
from application.notification import IObserver, NotificationCenter
from eventlib import proc
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.audio import WavePlayer, WavePlayerError
from twisted.internet import reactor
from zope.interface import implementer

from sylk.applications import SylkApplication, ApplicationLogger
from sylk.applications.playback.configuration import get_config
from sylk.bonjour import BonjourService
from sylk.configuration import ServerConfig


log = ApplicationLogger(__package__)


class PlaybackApplication(SylkApplication):

    def start(self):
        self.bonjour_services = []
        if ServerConfig.enable_bonjour:
            application_map = dict((item.split(':')) for item in ServerConfig.application_map)
            for uri, app in application_map.items():
                if app == 'playback':
                    config = get_config('%s' % uri)
                    if config is None:
                        continue
                    if os.path.isfile(config.file) and os.access(config.file, os.R_OK):
                        service = BonjourService(service='sipuri', name='Playback Test', uri_user=uri, is_focus=False)
                        service.start()
                        service.presence_state = BonjourPresenceState('available', 'File: %s' % os.path.basename(config.file))
                        self.bonjour_services.append(service)

    def stop(self):
        for service in self.bonjour_services:
            service.stop()
        del self.bonjour_services[:]

    def incoming_session(self, session):
        log.info('Session %s from %s to %s' % (session.call_id, session.remote_identity.uri, session.local_identity.uri))
        config = get_config('%s@%s' % (session.request_uri.user, session.request_uri.host))
        if config is None:
            config = get_config('%s' % session.request_uri.user)
            if config is None:
                log.info('Session %s rejected: no configuration found for %s' % (session.call_id, session.request_uri))
                session.reject(488)
                return
        stream_types = {'audio'}
        if config.enable_video:
            stream_types.add('video')
        streams = [stream for stream in session.proposed_streams if stream.type in stream_types]
        if not streams:
            log.info(u'Session %s rejected: invalid media' % session.call_id)
            session.reject(488)
            return
        handler = PlaybackHandler(config, session)
        handler.run()

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, request, data):
        request.answer(405)


@implementer(IObserver)
class PlaybackHandler(object):

    def __init__(self, config, session):
        self.config = config
        self.session = session
        self.proc = None

    def run(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)
        self.session.send_ring_indication()
        stream_types = {'audio'}
        if self.config.enable_video:
            stream_types.add('video')
        streams = [stream for stream in self.session.proposed_streams if stream.type in stream_types]
        reactor.callLater(self.config.answer_delay, self._accept_session, self.session, streams)

    def _accept_session(self, session, streams):
        if session.state == 'incoming':
            session.accept(streams)

    def _play(self):
        config = get_config('%s@%s' % (self.session.request_uri.user, self.session.request_uri.host))
        if config is None:
            config = get_config('%s' % self.session.request_uri.user)
        try:
            audio_stream = next(stream for stream in self.session.streams if stream.type=='audio')
        except StopIteration:
            self.proc = None
            return
        player = WavePlayer(audio_stream.mixer, config.file)
        audio_stream.bridge.add(player)
        log.info('Playing file %s for session %s' % (config.file, self.session.call_id))
        try:
            player.play().wait()
        except (ValueError, WavePlayerError) as e:
            log.warning('Error playing file %s: %s' % (config.file, e))
        except proc.ProcExit:
            pass
        finally:
            player.stop()
            self.proc = None
            audio_stream.bridge.remove(player)
            self.session.end()
            self.session = None

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            stream_types = {'audio'}
            if self.config.enable_video:
                stream_types.add('video')
            streams = [stream for stream in session.proposed_streams if stream.type in stream_types]
            if not streams:
                session.reject_proposal()
                return
            session.accept_proposal(streams)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        session = notification.sender

        for stream in notification.data.added_streams:
            log.info('Session %s added %s' % (session.call_id, stream.type))

        for stream in notification.data.removed_streams:
            log.info('Session %s removed %s' % (session.call_id, stream.type))

        if notification.data.added_streams and self.proc is None:
            self.proc = proc.spawn(self._play)

        if notification.data.removed_streams and not session.streams:
            session.end()

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        log.info('Session %s started' % session.call_id)
        self.proc = proc.spawn(self._play)

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        log.info('Session %s failed' % session.call_id)
        notification.center.remove_observer(self, sender=session)

    def _NH_SIPSessionWillEnd(self, notification):
        if self.proc:
            self.proc.kill()

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.info('Session %s ended' % session.call_id)
        notification.center.remove_observer(self, sender=session)

