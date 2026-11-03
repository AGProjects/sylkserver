"""Whether an account takes anonymous calls: the reject_anonymous attribute of
its own contact, copied whenever a WebSocket client fetches or writes the
addressbook, so that calls and pushes are screened without asking XCAP. Kept in
memory, and in Cassandra (or files) so that every server knows it. A record is
kept only for an account that rejects them.
"""

import json
import os
import threading
import time
import uuid

from application.python.types import Singleton
from twisted.internet import defer, threads

from .configuration import CassandraConfig, GeneralConfig
from .logger import log
from .storage import CASSANDRA_MODULES_AVAILABLE

__all__ = 'CallPolicies', 'is_anonymous', 'is_guest'

FIELDS = ('reject_anonymous',)
CACHE_TTL = 60                  # seconds a policy read from storage is trusted
DROPPED_TTL = 120               # seconds the Call-ID of a dropped push is remembered, for its cancel


def _aor(uri):
    uri = str(uri or '').strip().lower()
    prefix, sep, rest = uri.partition(':')
    if sep and prefix in ('sip', 'sips'):
        uri = rest
    return uri.split(';', 1)[0].split('?', 1)[0].split('>', 1)[0].lstrip('<')


def is_guest(uri):
    return '@guest.' in _aor(uri)


def is_anonymous(uri):
    uri = _aor(uri)
    return '@guest.' in uri or '@anonymous' in uri or uri.startswith('anonymous@')


def _flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in ('1', 'true')


class FileBackend(object):
    def __init__(self):
        self._lock = threading.Lock()

    def _path(self, account):
        return os.path.join(GeneralConfig.application_dir.normalized, 'call_policies', account[0], account + '.json')

    def load(self, account):
        try:
            with open(self._path(account)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save(self, account, policy):
        path = self._path(account)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f'{path}.tmp-{uuid.uuid4().hex}'
            with open(tmp, 'w') as f:
                json.dump(policy, f)
            os.replace(tmp, path)

    def remove(self, account):
        try:
            os.remove(self._path(account))
        except OSError:
            pass


class CassandraBackend(object):
    @property
    def model(self):
        from .models.storage.cassandra import CallPolicy
        return CallPolicy

    def load(self, account):
        row = self.model.objects(account=account).first()
        if row is None:
            return None
        policy = {name: bool(getattr(row, name)) for name in FIELDS}
        policy['updated'] = row.updated
        return policy

    def save(self, account, policy):
        self.model.create(account=account, **policy)

    def remove(self, account):
        self.model.objects(account=account).delete()


class CallPolicies(object, metaclass=Singleton):
    def __init__(self):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            self.backend = CassandraBackend()
        else:
            self.backend = FileBackend()
        self._cache = {}        # account -> (policy or None, time cached)
        self._dropped = {}      # Call-ID -> time its incoming_session push was dropped

    # learning, in the reactor

    def learn_from_addressbook(self, account, addressbook):
        account = _aor(account)
        for contact in getattr(addressbook, 'contacts', None) or []:
            if self._is_own(account, contact):
                self.learn(account, contact)
                return

    def learn_from_contact(self, account, contact):
        account = _aor(account)
        if self._is_own(account, contact):
            self.learn(account, contact)

    @staticmethod
    def _is_own(account, contact):
        return any(_aor(getattr(item, 'uri', None)) == account for item in getattr(contact, 'uris', None) or [])

    def learn(self, account, contact):
        attributes = getattr(contact, 'attributes', None)
        attributes = attributes.__data__ if attributes is not None else {}
        values = {name: _flag(attributes.get(name)) for name in FIELDS}
        cached = self._cache.get(account)
        if not any(values.values()):
            # nothing rejected: no record at all
            if cached is not None and cached[0] is None:
                return
            self._cache[account] = (None, time.monotonic())
            if cached is not None:
                log.info('Call policy of {}: anonymous calls accepted, record removed'.format(account))
                self._remove(account)
            else:
                threads.deferToThread(self.backend.load, account).addCallback(
                    lambda stored: self._remove(account) if stored is not None else None).addErrback(
                    lambda failure: log.warning('Cannot read the call policy of {}: {}'.format(account, failure.getErrorMessage())))
            return
        if cached is not None and cached[0] is not None and all(cached[0].get(name) == values[name] for name in FIELDS):
            return
        policy = dict(values, updated=int(time.time() * 1000))
        self._cache[account] = (policy, time.monotonic())
        log.info('Call policy of {}: {}'.format(account, ', '.join(f'{name}={values[name]}' for name in FIELDS)))
        threads.deferToThread(self.backend.save, account, policy).addErrback(
            lambda failure: log.warning('Cannot store the call policy of {}: {}'.format(account, failure.getErrorMessage())))

    def _remove(self, account):
        threads.deferToThread(self.backend.remove, account).addErrback(
            lambda failure: log.warning('Cannot remove the call policy of {}: {}'.format(account, failure.getErrorMessage())))

    # reading

    def get(self, account):
        """Deferred: the policy of the account, or None."""
        account = _aor(account)
        cached = self._cache.get(account)
        if cached is not None and time.monotonic() - cached[1] < CACHE_TTL:
            return defer.succeed(cached[0])
        deferred = threads.deferToThread(self.backend.load, account)

        def loaded(policy):
            self._cache[account] = (policy, time.monotonic())
            return policy

        def failed(failure):
            log.warning('Cannot read the call policy of {}: {}'.format(account, failure.getErrorMessage()))
            return cached[0] if cached is not None else None
        deferred.addCallbacks(loaded, failed)
        return deferred

    def rejects_anonymous(self, account):
        """Deferred: True when the account does not take anonymous calls."""
        return self.get(account).addCallback(lambda policy: bool(policy and policy.get('reject_anonymous')))

    # pushes

    def screen_push(self, account, event, originator, call_id):
        """Deferred: the reason to drop a push, or None to send it.

        Guests are screened by the gateway when they call, so their pushes pass."""
        now = time.monotonic()
        for key, dropped_at in list(self._dropped.items()):
            if now - dropped_at > DROPPED_TTL:
                del self._dropped[key]
        if event == 'cancel':
            return defer.succeed('the call was rejected' if call_id and call_id in self._dropped else None)
        if event != 'incoming_session' or not is_anonymous(originator) or is_guest(originator):
            return defer.succeed(None)

        def decide(rejects):
            if not rejects:
                return None
            if call_id:
                self._dropped[call_id] = time.monotonic()
            return 'anonymous calls are not accepted'
        return self.rejects_anonymous(account).addCallback(decide)

    def forget(self, account):
        """Blocking: remove the stored policy of the account."""
        account = _aor(account)
        self._cache.pop(account, None)
        self.backend.remove(account)
