
import json
import os
import random

from application.notification import IObserver, NotificationCenter
from application.python import Null
from eventlib import proc
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.audio import WavePlayer, WavePlayerError
from sipsimple.streams.msrp.chat import CPIMParserError, CPIMPayload
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import defer, reactor
from twisted.web.client import Agent, readBody
from twisted.web.http_headers import Headers
from zope.interface import implementer

from sylk.applications import ApplicationLogger, SylkApplication
from sylk.applications.echo import MessageHandler as EchoMessageHandler
from sylk.applications.playback.configuration import get_config
from sylk.bonjour import BonjourService
from sylk.configuration import ServerConfig

log = ApplicationLogger(__package__)


agent = Agent(reactor)
headers = Headers({'User-Agent': ['SylkServer'],
                   'Content-Type': ['application/json']})


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
        content_type = data.headers.get('Content-Type', Null).content_type
        from_header = data.headers.get('From', Null)
        to_header = data.headers.get('To', Null)

        if Null in (content_type, from_header, to_header):
            request.answer(400)
            return

        if not data.body:
            log.warning('SIP message from %s to %s rejected: empty body' % (from_header.uri, '%s@%s' % (to_header.uri.user, to_header.uri.host)))
            request.answer(400)
            return

        cpim_message = None
        if content_type == 'message/cpim':
            try:
                cpim_message = CPIMPayload.decode(data.body)
            except (CPIMParserError, UnicodeDecodeError):  # TODO: fix decoding in sipsimple
                log.warning('SIP message from %s to %s rejected: CPIM parse error' % (from_header.uri, '%s@%s' % (to_header.uri.user, to_header.uri.host)))
                request.answer(400)
                return
            else:
                content_type = cpim_message.content_type
        request_uri = data.request_uri
        config = get_config('%s@%s' % (request_uri.user, request_uri.host))
        if config is None:
            config = get_config('%s' % request_uri.user)
            if config is None:
                log.info('Message rejected: no configuration found for %s' % (request_uri))
                request.answer(404)
                return
        if not config.enable_chuck_norris_reply:
            log.info("Message rejected for %s: Chuck Norris reply is disabled. Even Chuck can't reply right now."  % (request_uri))
            request.answer(404)

        log.info('received SIP message (%s) from %s to %s' % (content_type, from_header.uri, '%s@%s' % (to_header.uri.user, to_header.uri.host)))

        request.answer(200)

        message_handler = ChuckNorrisMessageHandler(log)
        message_handler.incoming_message(data, content_type=content_type, cpim_message=cpim_message)


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


@implementer(IObserver)
class ChuckNorrisMessageHandler(EchoMessageHandler):

    def _get_fact_from_file(self):
        filepath = os.path.join(os.path.dirname(__file__), "facts.txt")

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                facts = [line.strip() for line in f if line.strip()]
                return random.choice(facts) if facts else "No facts available."
        except Exception as e:
            self.log.warning("Failed loading fallback facts file: %s", e)
            return "Chuck Norris doesn't need backup facts."

    @defer.inlineCallbacks
    def fetch_chuck_fact(self):
        categories = ['dev', 'music', 'travel', 'money', 'history', 'food', 'science', 'movie']
        destination = f"https://api.chucknorris.io/jokes/random?category={random.choice(categories)}"
        try:
            resp = yield agent.request(b'GET', destination.encode('utf-8'), headers=headers)
            if resp.code != 200:
                body = yield readBody(resp)
                self.log.warning("Non-200 response (%s) fetching chuck norris fact from %s: %r", resp.code, destination, body)
                raise Exception("Bad status code")

            body = yield readBody(resp)
            payload = json.loads(body)
            return payload['value']

        except defer.CancelledError:
            raise

        except Exception as e:
            self.log.warning("Error fetching chuck norris fact from %s: %s", destination, e)
            fact = self._get_fact_from_file()
            return fact

    def handle_incoming_message(self):
        if self.content_type in ('text/plain', 'text/html'):
            self.imdn_message()
            fetcher = defer.maybeDeferred(self.fetch_chuck_fact)
            fetcher.addCallback(self.send_chuck_norris_fact_message)

    @run_in_green_thread
    def send_chuck_norris_fact_message(self, content):
        to_uri = self.from_header.uri
        route = self._lookup_sip_target_route(to_uri)
        if not route:
            self.log.error("No route found for reply message to %s" % to_uri)
            return

        self.log.info("Playack message to %s using proxy %s" % (to_uri, route))
        self._send_message(self.from_header, self.to_header, content, 'text/plain', route)

    def _NH_SIPMessageDidSucceed(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        self.log.info('Message was accepted by remote party')

    def _NH_SIPMessageDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        data = notification.data
        reason = data.reason.decode() if isinstance(data.reason, bytes) else data.reason
        self.log.warning('Could not deliver message %s (%s)' % (reason, data.code))


