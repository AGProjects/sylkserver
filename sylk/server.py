# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

import sys

from threading import Event

from application import log
from application.notification import NotificationCenter, NotificationData
from eventlib import proc
from sipsimple.account import Account, BonjourAccount, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioDevice, RootAudioBridge
from sipsimple.configuration import ConfigurationError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import AudioMixer, Engine, SIPCoreError
from sipsimple.lookup import DNSManager
from sipsimple.storage import MemoryStorage
from sipsimple.threading import ThreadManager
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from uuid import uuid4

# Load extensions needed for integration with SIP SIMPLE SDK
import sylk.extensions

from sylk.applications import IncomingRequestHandler
from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig
from sylk.configuration.settings import AccountExtension, BonjourAccountExtension, SylkServerSettingsExtension
from sylk.log import Logger
from sylk.session import SessionManager


class SylkServer(SIPApplication):

    def __init__(self):
        self.logger = None
        self.request_handler = None
        self.stop_event = Event()

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, name='ThorNetworkGotFatalError')

        self.logger = Logger()

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        SIPSimpleSettings.register_extension(SylkServerSettingsExtension)

        try:
            SIPApplication.start(self, MemoryStorage())
        except ConfigurationError, e:
            log.fatal("Error loading configuration: ",e)
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

    @run_in_green_thread
    def _initialize_subsystems(self):
        account_manager = AccountManager()
        dns_manager = DNSManager()
        engine = Engine()
        notification_center = NotificationCenter()
        session_manager = SessionManager()
        settings = SIPSimpleSettings()
        self._load_configuration()

        notification_center.post_notification('SIPApplicationWillStart', sender=self)
        if self.state == 'stopping':
            reactor.stop()
            return

        account = account_manager.sylkserver_account

        # initialize core
        notification_center.add_observer(self, sender=engine)
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
                       tls_timeout=3000,
                       # rtp
                       rtp_port_range=(settings.rtp.port_range.start, settings.rtp.port_range.end),
                       # audio
                       codecs=list(settings.rtp.audio_codec_list),
                       # logging
                       log_level=settings.logs.pjsip_level,
                       trace_sip=True,
                       # events and requests to handle
                       events={'conference': ['application/conference-info+xml'],
                               'presence': ['application/pidf+xml'],
                               'refer': ['message/sipfrag;version=2.0']},
                       incoming_events=set(['conference', 'presence']),
                       incoming_requests=set(['MESSAGE'])
                      )
        try:
            engine.start(**options)
        except SIPCoreError:
            self.end_reason = 'engine failed'
            reactor.stop()
            return

        # initialize TLS
        try:
            engine.set_tls_options(port=settings.sip.tls_port if 'tls' in settings.sip.transport_list else None,
                                   verify_server=account.tls.verify_server,
                                   ca_file=settings.tls.ca_list.normalized if settings.tls.ca_list else None,
                                   cert_file=account.tls.certificate.normalized if account.tls.certificate else None,
                                   privkey_file=account.tls.certificate.normalized if account.tls.certificate else None,
                                   timeout=settings.tls.timeout)
        except Exception, e:
            notification_center.post_notification('SIPApplicationFailedToStartTLS', sender=self, data=NotificationData(error=e))

        # initialize PJSIP internal resolver
        engine.set_nameservers(dns_manager.nameservers)

        # initialize audio objects
        voice_mixer = AudioMixer(None, None, settings.audio.sample_rate, 0, 9999)
        self.voice_audio_device = AudioDevice(voice_mixer)
        self.voice_audio_bridge = RootAudioBridge(voice_mixer)
        self.voice_audio_bridge.add(self.voice_audio_device)

        # initialize instance id
        if not settings.instance_id:
            settings.instance_id = uuid4().urn
            settings.save()

        # initialize middleware components
        dns_manager.start()
        account_manager.start()
        session_manager.start()

        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

        self.state = 'started'
        notification_center.post_notification('SIPApplicationDidStart', sender=self)

    @run_in_green_thread
    def _shutdown_subsystems(self):
        # cleanup internals
        if self._wakeup_timer is not None and self._wakeup_timer.active():
            self._wakeup_timer.cancel()
        self._wakeup_timer = None

        # shutdown SIPThor interface
        sipthor_proc = proc.spawn(self._stop_sipthor)
        sipthor_proc.wait()

        # shutdown middleware components
        dns_manager = DNSManager()
        account_manager = AccountManager()
        session_manager = SessionManager()
        procs = [proc.spawn(dns_manager.stop), proc.spawn(account_manager.stop), proc.spawn(session_manager.stop)]
        proc.waitall(procs)

        # shutdown engine
        engine = Engine()
        engine.stop()

        # stop threads
        thread_manager = ThreadManager()
        thread_manager.stop()
        # stop the reactor
        reactor.stop()

    def _start_sipthor(self):
        if ThorNodeConfig.enabled:
            from sylk.interfaces.sipthor import ConferenceNode
            ConferenceNode()

    def _stop_sipthor(self):
        if ThorNodeConfig.enabled:
            from sylk.interfaces.sipthor import ConferenceNode
            ConferenceNode().stop()

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
        engine = Engine()
        settings = SIPSimpleSettings()
        local_ip = SIPConfig.local_ip
        log.msg("SylkServer started, listening on:")
        for transport in settings.sip.transport_list:
            try:
                log.msg("%s:%d (%s)" % (local_ip, getattr(engine, '%s_port' % transport), transport.upper()))
            except TypeError:
                pass
        # Start request handler
        self.request_handler = IncomingRequestHandler()
        self.request_handler.start()
        # Start SIPThor interface
        proc.spawn(self._start_sipthor)

    def _NH_SIPApplicationWillEnd(self, notification):
        self.request_handler.stop()

    def _NH_SIPApplicationDidEnd(self, notification):
        self.logger.stop()
        self.stop_event.set()

    def _NH_SIPEngineGotException(self, notification):
        log.error('An exception occured within the SIP core:\n%s\n' % notification.data.traceback)

    def _NH_ThorNetworkGotFatalError(self, notification):
        log.error("All Thor Event Servers have unrecoverable errors.")

