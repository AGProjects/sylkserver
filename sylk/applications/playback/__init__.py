# Copyright (C) 2013 AG Projects. See LICENSE for details
#

from application.python import Null
from application.notification import IObserver, NotificationCenter
from eventlib import proc
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import SylkApplication, ApplicationLogger
from sylk.applications.playback.configuration import get_config


log = ApplicationLogger.for_package(__package__)


class PlaybackApplication(SylkApplication):
    implements(IObserver)

    def start(self):
        pass

    def stop(self):
        pass

    def incoming_session(self, session):
        log.msg('Incoming session %s from %s to %s' % (session.call_id, session.remote_identity.uri, session.local_identity.uri))
        ruri = session._invitation.request_uri
        config = get_config('%s@%s' % (ruri.user, ruri.host))
        if config is None:
            log.msg(u'Session %s rejected: no configuration found for %s' % (session.call_id, ruri))
            session.reject(488)
            return
        stream_types = {'audio'}
        if config.enable_video:
            stream_types.add('video')
        streams = [stream for stream in session.proposed_streams if stream.type in stream_types]
        if not streams:
            log.msg(u'Session %s rejected: invalid media, only RTP audio and video is supported' % session.call_id)
            session.reject(488)
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)
        session.send_ring_indication()
        reactor.callLater(config.answer_delay, self._accept_session, session, streams)

    def _accept_session(self, session, streams):
        if session.state == 'incoming':
            session.accept(streams)

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, request, data):
        request.reject(405)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_green_thread
    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        log.msg('Session %s started' % session.call_id)
        handler = PlaybackHandler(session)
        handler.run()

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        log.msg('Session %s failed' % session.call_id)
        NotificationCenter().remove_observer(self, sender=session)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.msg('Session %s ended' % session.call_id)
        NotificationCenter().remove_observer(self, sender=session)

    def _NH_SIPSessionNewProposal(self, notification):
        if notification.data.originator == 'remote':
            session = notification.sender
            session.reject_proposal()


class PlaybackHandler(object):
    implements(IObserver)

    def __init__(self, session):
        self.session = session
        self.proc = None
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)

    def run(self):
        self.proc = proc.spawn(self._play)

    def _play(self):
        ruri = self.session._invitation.request_uri
        config = get_config('%s@%s' % (ruri.user, ruri.host))
        audio_stream = self.session.streams[0]
        player = WavePlayer(audio_stream.mixer, config.file)
        audio_stream.bridge.add(player)
        log.msg(u"Playing file %s for session %s" % (config.file, self.session.call_id))
        try:
            player.play().wait()
        except (ValueError, WavePlayerError), e:
            log.warning(u"Error playing file %s: %s" % (config.file, e))
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

    def _NH_SIPSessionWillEnd(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self.proc:
            self.proc.kill()

