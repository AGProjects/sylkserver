# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from application import log
from application.notification import NotificationCenter, IObserver
from application.python import Null
from eventlet import api, coros, proc
from eventlet.green import select
from sipsimple.account import bonjour, BonjourRegistrationFile
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import Command, run_in_green_thread
from sipsimple.util import TimestampedNotificationData
from twisted.internet import reactor
from zope.interface import implements


class RestartSelect(Exception): pass

class BonjourServices(object):
    implements(IObserver)

    def __init__(self, service='sipfocus', name='SylkServer', uri_user=None):
        self.account = AccountManager().default_account
        self.service = service
        self.name = name
        self.uri_user = uri_user
        self._stopped = True
        self._files = []
        self._command_channel = coros.queue()
        self._select_proc = None
        self._register_timer = None
        self._update_timer = None
        self._wakeup_timer = None

    @run_in_green_thread
    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SystemIPAddressDidChange')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self._select_proc = proc.spawn(self._process_files)
        proc.spawn(self._handle_commands)
        self._activate()

    @run_in_green_thread
    def stop(self):
        self._deactivate()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SystemIPAddressDidChange')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        self._select_proc.kill()
        self._command_channel.send_exception(api.GreenletExit)

    def _activate(self):
        self._stopped = False
        self._command_channel.send(Command('register'))

    def _deactivate(self):
        command = Command('stop')
        self._command_channel.send(command)
        command.wait()
        self._stopped = True

    def restart_registration(self):
        self._command_channel.send(Command('unregister'))
        self._command_channel.send(Command('register'))

    def update_registrations(self):
        self._command_channel.send(Command('update_registrations'))

    def _register_cb(self, file, flags, error_code, name, regtype, domain):
        notification_center = NotificationCenter()
        file = BonjourRegistrationFile.find_by_file(file)
        if error_code == bonjour.kDNSServiceErr_NoError:
            notification_center.post_notification('BonjourServiceRegistrationDidSucceed', sender=self,
                                                  data=TimestampedNotificationData(name=name, transport=file.transport))
        else:
            error = bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourServiceRegistrationDidFail', sender=self,
                                                  data=TimestampedNotificationData(reason=str(error), transport=file.transport))
            self._files.remove(file)
            self._select_proc.kill(RestartSelect)
            file.close()
            if self._register_timer is None:
                self._register_timer = reactor.callLater(1, self._command_channel.send, Command('register'))

    def _process_files(self):
        while True:
            try:
                ready = select.select([f for f in self._files if not f.active and not f.closed], [], [])[0]
            except RestartSelect:
                continue
            else:
                for file in ready:
                    file.active = True
                self._command_channel.send(Command('process_results', files=[f for f in ready if not f.closed]))

    def _handle_commands(self):
        while True:
            command = self._command_channel.wait()
            if not self._stopped:
                handler = getattr(self, '_CH_%s' % command.name)
                handler(command)

    def _CH_unregister(self, command):
        if self._register_timer is not None and self._register_timer.active():
            self._register_timer.cancel()
        self._register_timer = None
        if self._update_timer is not None and self._update_timer.active():
            self._update_timer.cancel()
        self._update_timer = None
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, BonjourRegistrationFile)):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        notification_center = NotificationCenter()
        for transport in set(file.transport for file in self._files):
            notification_center.post_notification('BonjourServiceRegistrationDidEnd', sender=self, data=TimestampedNotificationData(transport=transport))
        command.signal()

    def _CH_register(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        if self._register_timer is not None and self._register_timer.active():
            self._register_timer.cancel()
        self._register_timer = None
        supported_transports = set(transport for transport in settings.sip.transport_list if transport!='tls' or self.account.tls.certificate is not None)
        registered_transports = set(file.transport for file in self._files if isinstance(file, BonjourRegistrationFile))
        missing_transports = supported_transports - registered_transports
        added_transports = set()
        for transport in missing_transports:
            notification_center.post_notification('BonjourServiceWillRegister', sender=self, data=TimestampedNotificationData(transport=transport))
            try:
                contact_uri = self.account.contact[transport]
                contact_uri.user = self.uri_user
                contact_uri.parameters['isfocus'] = None
                txtdata = dict(txtvers=1, name=self.name, contact="<%s>" % str(contact_uri))
                file = bonjour.DNSServiceRegister(name=str(contact_uri),
                                                  regtype="_%s._%s" % (self.service, transport if transport == 'udp' else 'tcp'),
                                                  port=contact_uri.port,
                                                  callBack=self._register_cb,
                                                  txtRecord=bonjour.TXTRecord(items=txtdata))
            except (bonjour.BonjourError, KeyError), e:
                notification_center.post_notification('BonjourServiceRegistrationDidFail', sender=self,
                                                      data=TimestampedNotificationData(reason=str(e), transport=transport))
            else:
                self._files.append(BonjourRegistrationFile(file, transport))
                added_transports.add(transport)
        if added_transports:
            self._select_proc.kill(RestartSelect)
        if added_transports != missing_transports:
            self._register_timer = reactor.callLater(1, self._command_channel.send, Command('register', command.event))
        else:
            command.signal()

    def _CH_update_registrations(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        if self._update_timer is not None and self._update_timer.active():
            self._update_timer.cancel()
        self._update_timer = None
        available_transports = settings.sip.transport_list
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, BonjourRegistrationFile) and f.transport not in available_transports):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        update_failure = False
        for file in (f for f in self._files if isinstance(f, BonjourRegistrationFile)):
            try:
                contact_uri = self.account.contact[file.transport]
                contact_uri.user = self.user_uri
                contact_uri.parameters['isfocus'] = None
                txtdata = dict(txtvers=1, name=self.name, contact="<%s>" % str(contact_uri))
                bonjour.DNSServiceUpdateRecord(file.file, None, flags=0, rdata=bonjour.TXTRecord(items=txtdata), ttl=0)
            except (bonjour.BonjourError, KeyError), e:
                notification_center.post_notification('BonjourServiceRegistrationUpdateDidFail', sender=self,
                                                      data=TimestampedNotificationData(reason=str(e), transport=file.transport))
                update_failure = True
        self._command_channel.send(Command('register'))
        if update_failure:
            self._update_timer = reactor.callLater(1, self._command_channel.send, Command('update_registrations', command.event))
        else:
            command.signal()

    def _CH_process_results(self, command):
        for file in (f for f in command.files if not f.closed):
            try:
                bonjour.DNSServiceProcessResult(file.file)
            except:
                # Should we close the file? The documentation doesn't say anything about this. -Luci
                log.err()
        for file in command.files:
            file.active = False
        self._files = [f for f in self._files if not f.closed]
        self._select_proc.kill(RestartSelect)

    def _CH_stop(self, command):
        if self._register_timer is not None and self._register_timer.active():
            self._register_timer.cancel()
        self._register_timer = None
        if self._update_timer is not None and self._update_timer.active():
            self._update_timer.cancel()
        self._update_timer = None
        if self._wakeup_timer is not None and self._wakeup_timer.active():
            self._wakeup_timer.cancel()
        self._wakeup_timer = None
        old_files = self._files
        self._files = []
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        notification_center = NotificationCenter()
        for transport in set(file.transport for file in self._files):
            notification_center.post_notification('BonjourServiceRegistrationDidEnd', sender=self, data=TimestampedNotificationData(transport=transport))
        command.signal()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SystemIPAddressDidChange(self, notification):
        if self._files:
            self.restart_registration()

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self._wakeup_timer is None:
            def wakeup_action():
                if self._files:
                    self.restart_registration()
                self._wakeup_timer = None
            self._wakeup_timer = reactor.callLater(5, wakeup_action) # wait for system to stabilize
