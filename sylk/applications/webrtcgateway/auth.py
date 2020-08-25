import imaplib
import socket
import ssl
from hashlib import md5
from eventlib.twistedutil import deferToGreenThread

from .logger import log
from .models import sylkrtc
from .configuration import ExternalAuthConfig, get_auth_config

class AuthHandler(object):
    def __init__(self, account_info, connection):
        if ExternalAuthConfig.enable:
            self.domain = account_info.id.partition('@')[2]
            self.user = account_info.id.partition('@')[0]
            self.password = account_info.password
            self.auth_conf = get_auth_config(self.domain)
        self.account_info = account_info
        self.connection = connection

    @property
    def type(self):
        if ExternalAuthConfig.enable:
            return self.auth_conf.auth_type
        else:
            return 'SIP'

    def authenticate(self, proxy):
        deferred = deferToGreenThread(self._authenticate, proxy)
        deferred.addCallback(self._auth_finished)

    def _authenticate(self, proxy):
        if self.auth_conf.auth_type == 'SIP':
            # for sip to continue we need to apply a ha1 hash
            self.account_info.password = md5('{u}:{d}:{p}'.format(
                    u=self.user,d=self.domain,p=self.password)).hexdigest()
            return (True, proxy)
        elif self.auth_conf.auth_type == 'IMAP':
            try:
                imap_con = imaplib.IMAP4_SSL(self.auth_conf.imap_server)
            except ssl.SSLError:
                log.error('SSL handshake failed for server {server}. Check your ca config!'.format(server=self.auth_conf.imap_server))
                return (False, None)
            log.debug('trying imap login for {user}'.format(user=self.user))
            try:
                rv, data = imap_con.login(self.user, self.password)
            except imaplib.IMAP4.error:
                log.info('imap auth failed for {user}'.format(user=self.user))
                return (False, None)
            return (True, proxy)

    def _auth_finished(self, ret):
        success, proxy = ret
        if success:
            # callout to janus
            self.account_info.janus_handle.register(self.account_info, proxy=proxy)
        else:
            if self.account_info.registration_state != 'failed':
                self.account_info.registration_state = 'failed'
            reason = 'external authentication failed (wrong username or password?)'
            self.connection.send(sylkrtc.AccountRegistrationFailedEvent(
                            account=self.account_info.id, reason=reason))
            log.info('registration for {account.id} failed: {reason}'.format(
                            account=self.account_info, reason=reason))

# ca checks for imap4 ssl

ca_cert_file = ExternalAuthConfig.imap_ca_cert_file

def IMAP4SSL_open(self, host = '', port = imaplib.IMAP4_SSL_PORT):
    self.host = host
    self.port = port
    self.sock = socket.create_connection((host, port))
    self.sslobj = ssl.wrap_socket(self.sock, keyfile=self.keyfile, certfile=self.certfile,
                                  server_side=False, cert_reqs=ssl.CERT_REQUIRED,
                                  ca_certs=ca_cert_file)
    self.file = self.sslobj.makefile('rb')

imaplib.IMAP4_SSL.__dict__['open']=IMAP4SSL_open
