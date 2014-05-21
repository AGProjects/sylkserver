# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import sys

from threading import Event
from uuid import uuid4

from application import log
from application.notification import NotificationCenter
from application.python import Null
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
from twisted.internet import reactor

# Load extensions needed for integration with SIP SIMPLE SDK
import sylk.extensions

from sylk.applications import IncomingRequestHandler
from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig
from sylk.configuration.settings import AccountExtension, BonjourAccountExtension, SylkServerSettingsExtension
from sylk.log import Logger
from sylk.session import SessionManager


class SylkServer(SIPApplication):

    def __init__(self):
        self.request_handler = Null
        self.thor_interface = Null

        self.logger = Logger()

        self.stopping_event = Event()
        self.stop_event = Event()

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, name='ThorNetworkGotFatalError')

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        SIPSimpleSettings.register_extension(SylkServerSettingsExtension)

        try:
            super(SylkServer, self).start(MemoryStorage())
        except Exception, e:
            log.fatal("Error starting SIP Application: %s" % e)
            sys.exit(1)

    def _load_configuration(self):
        if '--enable-bonjour' in sys.argv:
            ServerConfig.enable_bonjour = True
        account_manager = AccountManager()
        account = Account("account@example.com")     # an account is required by AccountManager
        account.message_summary.enabled = False
        account.presence.enabled = False
        account.sip.register = False
        account.xcap.enabled = False
        # Disable MSRP ACM if we are using Bonjour
        account.msrp.connection_model = 'relay' if ServerConfig.enable_bonjour else 'acm'
        account.save()
        account_manager.sylkserver_account = account

    def _initialize_core(self):
        # SylkServer needs to listen for extra events and request types

        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        # initialize core
        options = dict(# general
                       user_agent=settings.user_agent,
                       # SIP
                       detect_sip_loops=True,
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
                       # logging
                       log_level=settings.logs.pjsip_level if settings.logs.trace_pjsip else 0,
                       trace_sip=settings.logs.trace_sip,
                       # events and requests to handle
                       events={'conference': ['application/conference-info+xml'],
                               'presence': ['application/pidf+xml'],
                               'refer': ['message/sipfrag;version=2.0']},
                       incoming_events=set(['conference', 'presence']),
                       incoming_requests=set(['MESSAGE']))
        with self.engine._lock:
            # make sure we add the observer before the engine thread actually runs
            self.engine.start(**options)
            notification_center.add_observer(self, sender=self.engine)

    @run_in_green_thread
    def _initialize_subsystems(self):
        account_manager = AccountManager()
        dns_manager = DNSManager()
        notification_center = NotificationCenter()
        session_manager = SessionManager()
        settings = SIPSimpleSettings()
        self._load_configuration()

        notification_center.post_notification('SIPApplicationWillStart', sender=self)
        if self.state == 'stopping':
            reactor.stop()
            return

        account = account_manager.sylkserver_account
        account_manager.default_account = account

        # initialize TLS
        self._initialize_tls()

        # initialize PJSIP internal resolver
        self.engine.set_nameservers(dns_manager.nameservers)

        # initialize audio objects
        voice_mixer = AudioMixer(None, None, settings.audio.sample_rate, 0, 9999)
        self.voice_audio_device = AudioDevice(voice_mixer)
        self.voice_audio_bridge = RootAudioBridge(voice_mixer)
        self.voice_audio_bridge.add(self.voice_audio_device)

        # initialize instance id
        settings.instance_id = uuid4().urn
        settings.save()

        # initialize middleware components
        dns_manager.start()
        account_manager.start()
        session_manager.start()

        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

        self.state = 'started'
        notification_center.post_notification('SIPApplicationDidStart', sender=self)

        # start SylkServer components
        if ThorNodeConfig.enabled:
            from sylk.interfaces.sipthor import ConferenceNode
            self.thor_interface = ConferenceNode()
        self.request_handler = IncomingRequestHandler()
        self.request_handler.start()

    @run_in_green_thread
    def _shutdown_subsystems(self):
        # shutdown SylkServer components
        procs = [proc.spawn(self.request_handler.stop), proc.spawn(self.thor_interface.stop)]
        proc.waitall(procs)

        # shutdown middleware components
        dns_manager = DNSManager()
        account_manager = AccountManager()
        session_manager = SessionManager()
        procs = [proc.spawn(dns_manager.stop), proc.spawn(account_manager.stop), proc.spawn(session_manager.stop)]
        proc.waitall(procs)

        # shutdown engine
        self.engine.stop()
        self.engine.join()

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
        log.fatal("Couldn't set TLS options: %s" % notification.data.error)

    def _NH_SIPApplicationWillStart(self, notification):
        self.logger.start()
        settings = SIPSimpleSettings()
        if settings.logs.trace_sip and self.logger._siptrace_filename is not None:
            log.msg('Logging SIP trace to file "%s"' % self.logger._siptrace_filename)
        if settings.logs.trace_msrp and self.logger._msrptrace_filename is not None:
            log.msg('Logging MSRP trace to file "%s"' % self.logger._msrptrace_filename)
        if settings.logs.trace_pjsip and self.logger._pjsiptrace_filename is not None:
            log.msg('Logging PJSIP trace to file "%s"' % self.logger._pjsiptrace_filename)
        if settings.logs.trace_notifications and self.logger._notifications_filename is not None:
            log.msg('Logging notifications trace to file "%s"' % self.logger._notifications_filename)

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        local_ip = SIPConfig.local_ip
        log.msg("SylkServer started, listening on:")
        for transport in settings.sip.transport_list:
            try:
                log.msg("%s:%d (%s)" % (local_ip, getattr(self.engine, '%s_port' % transport), transport.upper()))
            except TypeError:
                pass

    def _NH_SIPApplicationWillEnd(self, notification):
        log.msg('SIP application will end: %s' % self.end_reason)
        self.stopping_event.set()

    def _NH_SIPApplicationDidEnd(self, notification):
        log.msg('SIP application ended')
        self.logger.stop()
        if not self.stopping_event.is_set():
            log.warning('SIP application ended without shutting down all subsystems')
            self.stopping_event.set()
        self.stop_event.set()

    def _NH_SIPEngineGotException(self, notification):
        log.error('An exception occured within the SIP core:\n%s\n' % notification.data.traceback)

    def _NH_SIPEngineDidFail(self, notification):
        log.error('SIP engine failed')
        super(SylkServer, self)._NH_SIPEngineDidFail(notification)

    def _NH_ThorNetworkGotFatalError(self, notification):
        log.error("All Thor Event Servers have unrecoverable errors.")

