"""Log in to the HTTP API with SIP credentials, for a page that has no API token.

The browser sends the HA1 of the account, md5(user:domain:password). The
server asks the SIP proxy for the account's registrations with a REGISTER that
has no Contact (RFC 3261 10.2.3: a query, nothing changes), answering its
challenge with the HA1: a 200 proves the credentials. The account's API token is
then handed out, as application/sylk-api-token does.

Failed logins are limited per account and per address.
"""

import re
import secrets
import time

from application.notification import IObserver, NotificationCenter
from eventlib.twistedutil import block_on, deferToGreenThread
from sipsimple.core import SIPURI, Credentials, FromHeader, Request, RouteHeader, ToHeader
from twisted.internet import defer, reactor
from zope.interface import implementer

from .configuration import GeneralConfig
from .logger import log, connections_log
from .storage import MessageStorage

__all__ = 'sip_login', 'LoginError', 'TooManyAttempts'

HA1_RE = re.compile(r'^[0-9a-f]{32}$')
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60        # seconds
QUERY_TIMEOUT = 10              # seconds


class LoginError(Exception): pass
class TooManyAttempts(Exception): pass


@implementer(IObserver)
class _RegisterQuery(object):
    """A REGISTER without Contact; deferred: the final response code (0 when none came)."""

    def __init__(self, account, ha1, route):
        aor = SIPURI.parse('sip:' + account)
        self.deferred = defer.Deferred()
        credentials = Credentials(username=aor.user, password=ha1.encode(), digest=True)
        self.request = Request('REGISTER', SIPURI(host=aor.host), FromHeader(aor), ToHeader(aor), RouteHeader(route.uri), credentials=credentials)
        NotificationCenter().add_observer(self, sender=self.request)

    def send(self):
        self.request.send(timeout=QUERY_TIMEOUT)
        return self.deferred

    def handle_notification(self, notification):
        if notification.name not in ('SIPRequestDidSucceed', 'SIPRequestDidFail'):
            return
        NotificationCenter().remove_observer(self, sender=notification.sender)
        code = getattr(notification.data, 'code', 0) or 0
        reactor.callFromThread(self._done, code)

    def _done(self, code):
        if not self.deferred.called:
            self.deferred.callback(code)


class SIPLogin(object):
    def __init__(self):
        self._failures = {}         # key -> [times]

    def _recent(self, key, now):
        times = [stamp for stamp in self._failures.get(key, ()) if now - stamp < FAILURE_WINDOW]
        if times:
            self._failures[key] = times
        else:
            self._failures.pop(key, None)
        return times

    def check_allowed(self, account, address):
        now = time.monotonic()
        if any(len(self._recent(key, now)) >= MAX_FAILURES for key in (('account', account), ('address', address))):
            raise TooManyAttempts('too many failed logins, try again later')

    def _failed(self, account, address):
        now = time.monotonic()
        for key in (('account', account), ('address', address)):
            self._failures.setdefault(key, []).append(now)

    def login(self, account, ha1, address):
        """Deferred: the API token of the account. Fails with LoginError or TooManyAttempts."""
        account = (account or '').strip().lower()
        if account.startswith('sip:'):
            account = account[4:]
        user, _, domain = account.partition('@')
        if not user or not domain:
            return defer.fail(LoginError('invalid account'))
        if not {'*', domain}.intersection(GeneralConfig.sip_domains):
            return defer.fail(LoginError('domain not allowed'))
        ha1 = (ha1 or '').strip().lower()
        if not HA1_RE.match(ha1):
            return defer.fail(LoginError('invalid credentials'))
        try:
            self.check_allowed(account, address)
        except TooManyAttempts as e:
            connections_log.warning(f'SIP login for {account} from {address} refused: too many failed attempts')
            return defer.fail(e)

        def query():
            from .sip_handlers import MessageHandler
            route = MessageHandler()._lookup_sip_target_route(account, sender=account)
            return block_on(_RegisterQuery(account, ha1, route).send())

        def checked(code):
            if code == 200:
                connections_log.info(f'SIP login for {account} from {address} succeeded')
                return self._token(account)
            if code in (401, 403, 407):
                self._failed(account, address)
                connections_log.warning(f'SIP login for {account} from {address} failed: wrong credentials ({code})')
                raise LoginError('wrong account or password')
            connections_log.warning(f'SIP login for {account} from {address} failed: the proxy answered {code or "nothing"}')
            raise LoginError(f'the SIP proxy answered {code}' if code else 'the SIP proxy did not answer')

        deferred = deferToGreenThread(query)
        deferred.addCallback(checked)
        return deferred

    @staticmethod
    def _token(account):
        storage = MessageStorage()

        def issue(token):
            if token:
                return token
            token = secrets.token_urlsafe()
            connections_log.info(f'Generating new API token for {account} (SIP login)')
            storage.add_account(account)
            storage.add_account_token(account=account, token=token)
            return token
        return defer.maybeDeferred(storage.get_account_token, account).addCallback(issue)


_login = None


def sip_login():
    global _login
    if _login is None:
        _login = SIPLogin()
    return _login
