
import os
import sys

from threading import Event
from uuid import uuid4

from application import log
from application.notification import NotificationCenter
from application.python import Null
from application.system import makedirs
from eventlib import proc
from sipsimple.account import Account, BonjourAccount, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioDevice, RootAudioBridge
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import AudioMixer
from sipsimple.lookup import DNSManager
from sipsimple.storage import MemoryStorage
from sipsimple.threading import ThreadManager
from sipsimple.threading.green import run_in_green_thread
from sipsimple.video import VideoDevice
from twisted.internet import reactor

# Load stream extensions needed for integration with SIP SIMPLE SDK
import sylk.streams; del sylk.streams

from sylk.accounts import DefaultAccount
from sylk.applications import IncomingRequestHandler
from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig
from sylk.configuration.settings import AccountExtension, BonjourAccountExtension, SylkServerSettingsExtension
from sylk.log import TraceLogManager
from sylk.session import SessionManager
from sylk.web import WebServer


class SylkServer(SIPApplication):
    def __init__(self):
        self.request_handler = Null
        self.thor_interface = Null
        self.web_server = Null

        self.options = Null

        self.stopping_event = Event()
        self.stopped_event = Event()
        self.failed = False

    def start(self, options):
        self.options = options
        if self.options.enable_bonjour:
            ServerConfig.enable_bonjour = True

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, name='ThorNetworkGotFatalError')

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        SIPSimpleSettings.register_extension(SylkServerSettingsExtension)

        super(SylkServer, self).start(MemoryStorage())

    def run(self, options):
        """Start the server and wait for it to finish before returning control to the caller"""

        self.start(options)
        while not self.stopping_event.wait(9999):
            pass
        self.stopped_event.wait(5)

    def _initialize_core(self):
        # SylkServer needs to listen for extra events and request types

        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        # initialize core
        options = dict(# general
                       ip_address=SIPConfig.local_ip,
                       user_agent=settings.user_agent,
                       # SIP
                       detect_sip_loops=False,
                       udp_port=settings.sip.udp_port if 'udp' in settings.sip.transport_list else None,
                       tcp_port=settings.sip.tcp_port if 'tcp' in settings.sip.transport_list else None,
                       tls_port=None,
                       # TLS
                       tls_verify_server=False,
                       tls_ca_file=None,
                       tls_cert_file=None,
                       tls_privkey_file=None,
                       # rtp
                       rtp_port_range=(settings.rtp.port_range.start, settings.rtp.port_range.end),
                       # audio
                       codecs=list(settings.rtp.audio_codec_list),
                       # video
                       video_codecs=list(settings.rtp.video_codec_list),
                       enable_colorbar_device=True,
                       # logging
                       log_level=settings.logs.pjsip_level if settings.logs.trace_pjsip else 0,
                       trace_sip=settings.logs.trace_sip,
                       # events and requests to handle
                       events={'conference': ['application/conference-info+xml'],
                               'presence': ['application/pidf+xml'],
                               'refer': ['message/sipfrag;version=2.0']},
                       incoming_events={'conference', 'presence'},
                       incoming_requests={'MESSAGE'})
        notification_center.add_observer(self, sender=self.engine)
        self.engine.start(**options)

    @run_in_green_thread
    def _initialize_subsystems(self):
        notification_center = NotificationCenter()

        with self._lock:
            stop_pending = self._stop_pending
            if stop_pending:
                self.state = 'stopping'
        if stop_pending:
            notification_center.post_notification('SIPApplicationWillEnd', sender=self)
            reactor.stop()
            return

        account_manager = AccountManager()
        dns_manager = DNSManager()
        session_manager = SessionManager()
        settings = SIPSimpleSettings()

        # Initialize default account
        default_account = DefaultAccount()
        account_manager.default_account = default_account

        # initialize TLS
        self._initialize_tls()

        # initialize PJSIP internal resolver
        self.engine.set_nameservers(['8.8.8.8'])

        # initialize audio objects
        voice_mixer = AudioMixer(None, None, settings.audio.sample_rate, 0, 9999)
        self.voice_audio_device = AudioDevice(voice_mixer)
        self.voice_audio_bridge = RootAudioBridge(voice_mixer)
        self.voice_audio_bridge.add(self.voice_audio_device)

        # initialize video objects
        self.video_device = VideoDevice(u'Colorbar generator', settings.video.resolution, settings.video.framerate)

        # initialize instance id
        settings.instance_id = uuid4().urn
        settings.save()

        # initialize ZRTP cache
        makedirs(ServerConfig.spool_dir.normalized)
        self.engine.zrtp_cache = os.path.join(ServerConfig.spool_dir.normalized, 'zrtp.db')

        # initialize middleware components
        dns_manager.start()
        account_manager.start()
        session_manager.start()

        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

        self.state = 'started'
        notification_center.post_notification('SIPApplicationDidStart', sender=self)

        # start SylkServer components
        self.web_server = WebServer()
        self.web_server.start()
        self.request_handler = IncomingRequestHandler()
        self.request_handler.start()
        if ThorNodeConfig.enabled:
            from sylk.interfaces.sipthor import ConferenceNode
            self.thor_interface = ConferenceNode()
            thor_roles = []
            if 'conference' in self.request_handler.application_registry:
                thor_roles.append('conference_server')
            if 'xmppgateway' in self.request_handler.application_registry:
                thor_roles.append('xmpp_gateway')
            if 'webrtcgateway' in self.request_handler.application_registry:
                thor_roles.append('webrtc_gateway')
            self.thor_interface.start(thor_roles)

    @run_in_green_thread
    def _shutdown_subsystems(self):
        dns_manager = DNSManager()
        account_manager = AccountManager()
        session_manager = SessionManager()

        # terminate all sessions
        p = proc.spawn(session_manager.stop)
        p.wait()

        # shutdown SylkServer components
        procs = [proc.spawn(self.web_server.stop), proc.spawn(self.request_handler.stop), proc.spawn(self.thor_interface.stop)]
        proc.waitall(procs)

        # shutdown other middleware components
        procs = [proc.spawn(dns_manager.stop), proc.spawn(account_manager.stop)]
        proc.waitall(procs)

        # shutdown engine
        self.engine.stop()
        self.engine.join(timeout=5)

        # stop threads
        thread_manager = ThreadManager()
        thread_manager.stop()

        # stop the reactor
        reactor.stop()

    def _NH_AudioDevicesDidChange(self, notification):
        pass

    def _NH_DefaultAudioDeviceDidChange(self, notification):
        pass

    def _NH_SIPApplicationFailedToStartTLS(self, notification):
        log.fatal('Could not set TLS options: %s' % notification.data.error)
        sys.exit(1)

    def _NH_SIPApplicationWillStart(self, notification):
        tracelog_manager = TraceLogManager()
        tracelog_manager.start()

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        local_ip = SIPConfig.local_ip
        log.info('SylkServer started; listening on:')
        for transport in settings.sip.transport_list:
            try:
                log.info('  %s:%d (%s)' % (local_ip, getattr(self.engine, '%s_port' % transport), transport.upper()))
            except TypeError:
                pass

    def _NH_SIPApplicationWillEnd(self, notification):
        log.info('Stopping SylkServer...')
        self.stopping_event.set()

    def _NH_SIPApplicationDidEnd(self, notification):
        log.info('SIP application ended')
        tracelog_manager = TraceLogManager()
        tracelog_manager.stop()
        if not self.stopping_event.is_set():
            log.warning('SIP application ended without shutting down all subsystems')
            self.stopping_event.set()
        self.stopped_event.set()

    def _NH_SIPEngineDidFail(self, notification):
        log.error('SIP engine failed')
        self.failed = True
        super(SylkServer, self)._NH_SIPEngineDidFail(notification)

    def _NH_ThorNetworkGotFatalError(self, notification):
        log.error('All Thor Event Servers have unrecoverable errors')

