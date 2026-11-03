"""Temporary call links: <web>/call/<token>@anonymous.invalid calls the account that made the link
until it expires, without the caller learning the account address.

A link is kept twice, by account (to manage them) and by token (to call). With
Cassandra the rows expire by themselves (TTL), the retention period after the
link expiry; with the file backend they are dropped when the account is listed.
"""

import base64
import json
import os
import re
import secrets
import threading
import time
import uuid

from application.python.types import Singleton

from .account_storage import normalize_account, normalize_device_id, normalize_device_name
from .configuration import CallLinksConfig, CassandraConfig
from .logger import log
from .storage import CASSANDRA_MODULES_AVAILABLE

__all__ = ('CallLinks', 'CallLinkError', 'NotFound', 'InvalidRequest', 'LimitExceeded', 'is_token', 'CALL_LINK_HEADER', 'LINK_DOMAIN')

CALL_LINK_HEADER = 'X-Sylk-Call-Link'
LINK_DOMAIN = 'anonymous.invalid'

TOKEN_RE = re.compile(r'^[a-z2-7]{16}$')
LINK_ID_RE = re.compile(r'^[0-9a-f]{32}$')
MAX_NAME_LENGTH = 100
DAY = 86400 * 1000


class CallLinkError(Exception): pass
class NotFound(CallLinkError): pass
class InvalidRequest(CallLinkError): pass
class LimitExceeded(CallLinkError): pass


def now():
    return int(time.time() * 1000)


def is_token(value):
    return isinstance(value, str) and bool(TOKEN_RE.match(value)) and not value.isdigit()


def new_token():
    while True:
        token = base64.b32encode(secrets.token_bytes(10)).decode().lower()
        if is_token(token):
            return token


def check_link_id(link_id):
    if not isinstance(link_id, str) or not LINK_ID_RE.match(link_id):
        raise NotFound()
    return link_id


def check_name(name):
    if not isinstance(name, str):
        raise InvalidRequest('name must be a string')
    name = ' '.join(name.split())
    name = ''.join(char for char in name if char.isprintable())
    if not name:
        raise InvalidRequest('name is empty')
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidRequest(f'name longer than {MAX_NAME_LENGTH} characters')
    return name


def check_expires(expires):
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise InvalidRequest('expires must be a time in milliseconds')
    expires = int(expires)
    current = now()
    if expires <= current:
        raise InvalidRequest('expires is in the past')
    if expires > current + CallLinksConfig.max_lifetime * DAY:
        raise InvalidRequest(f'a link may last at most {CallLinksConfig.max_lifetime} days')
    return expires


def kept_until(record):
    return record['expires'] + CallLinksConfig.retention * DAY


def ttl(record):
    return max(1, (kept_until(record) - now()) // 1000)


def public(record):
    """A link as the clients get it."""
    result = {key: record.get(key) for key in ('id', 'token', 'name', 'created', 'expires', 'device_id', 'device_name', 'calls', 'last_call')}
    result['calls'] = result['calls'] or 0
    result['expired'] = record['expires'] <= now()
    return {key: value for key, value in result.items() if value is not None}


class FileBackend(object):
    def __init__(self):
        self._lock = threading.RLock()

    @property
    def root(self):
        return CallLinksConfig.storage_dir.normalized

    def _account_path(self, account):
        return os.path.join(self.root, 'accounts', account[0], account + '.json')

    def _token_path(self, token):
        return os.path.join(self.root, 'tokens', token[:2], token + '.json')

    @staticmethod
    def _read(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.tmp-{uuid.uuid4().hex}'
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _load(self, account):
        records = self._read(self._account_path(account), {})
        current = now()
        stale = [link_id for link_id, record in records.items() if kept_until(record) <= current]
        if stale:
            for link_id in stale:
                self._remove(self._token_path(records.pop(link_id)['token']))
            self._store(account, records)
        return records

    def _store(self, account, records):
        if records:
            self._write(self._account_path(account), records)
        else:
            self._remove(self._account_path(account))

    def records(self, account):
        with self._lock:
            return list(self._load(account).values())

    def get(self, account, link_id):
        with self._lock:
            return self._load(account).get(link_id)

    def claim_token(self, token):
        return not os.path.exists(self._token_path(token))

    def save(self, account, record):
        with self._lock:
            records = self._load(account)
            records[record['id']] = record
            self._store(account, records)
            self._write(self._token_path(record['token']), {'account': account, 'id': record['id'], 'expires': record['expires']})

    def remove(self, account, record):
        with self._lock:
            records = self._load(account)
            records.pop(record['id'], None)
            self._store(account, records)
            self._remove(self._token_path(record['token']))

    def by_token(self, token):
        entry = self._read(self._token_path(token), None)
        if not isinstance(entry, dict) or not entry.get('account') or not entry.get('id'):
            return None
        return entry['account'], entry['id']

    def count_call(self, account, record):
        with self._lock:
            records = self._load(account)
            if record['id'] in records:
                records[record['id']].update(calls=record['calls'], last_call=record['last_call'])
                self._store(account, records)


class CassandraBackend(object):
    FIELDS = ('token', 'name', 'created', 'expires', 'device_id', 'device_name', 'calls', 'last_call')

    @property
    def models(self):
        from .models.storage.cassandra import CallLink, CallLinkToken
        return CallLink, CallLinkToken

    def _record(self, row):
        record = {name: getattr(row, name) for name in self.FIELDS}
        record['id'] = row.link_id
        return record

    def records(self, account):
        link_model, _ = self.models
        return [self._record(row) for row in link_model.objects(account=account).limit(None)]

    def get(self, account, link_id):
        link_model, _ = self.models
        try:
            return self._record(link_model.objects(account=account, link_id=link_id).get())
        except link_model.DoesNotExist:
            return None

    def claim_token(self, token):
        _, token_model = self.models
        return token_model.objects(token=token).first() is None

    def save(self, account, record):
        link_model, token_model = self.models
        seconds = ttl(record)
        token_model.ttl(seconds).create(token=record['token'], account=account, link_id=record['id'], expires=record['expires'])
        link_model.ttl(seconds).create(account=account, link_id=record['id'], **{name: record.get(name) for name in self.FIELDS})

    def remove(self, account, record):
        link_model, token_model = self.models
        token_model.objects(token=record['token']).delete()
        link_model.objects(account=account, link_id=record['id']).delete()

    def by_token(self, token):
        _, token_model = self.models
        row = token_model.objects(token=token).first()
        return (row.account, row.link_id) if row is not None else None

    def count_call(self, account, record):
        link_model, _ = self.models
        link_model.objects(account=account, link_id=record['id']).ttl(ttl(record)).update(calls=record['calls'], last_call=record['last_call'])


class CallLinks(object, metaclass=Singleton):
    """Blocking: call it from a thread."""

    def __init__(self):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            self.backend = CassandraBackend()
        else:
            log.info(f'Using file backend for call links: {CallLinksConfig.storage_dir.normalized}')
            self.backend = FileBackend()

    def list(self, account):
        account = normalize_account(account)
        records = [record for record in self.backend.records(account) if kept_until(record) > now()]
        records.sort(key=lambda record: record.get('created') or 0, reverse=True)
        return [public(record) for record in records]

    def create(self, account, name, expires, device_id=None, device_name=None):
        account = normalize_account(account)
        name, expires = check_name(name), check_expires(expires)
        active = [record for record in self.backend.records(account) if record['expires'] > now()]
        if len(active) >= CallLinksConfig.max_links:
            raise LimitExceeded(f'at most {CallLinksConfig.max_links} links may be active')
        for _ in range(5):
            token = new_token()
            if self.backend.claim_token(token):
                break
        else:
            raise CallLinkError('no free token')
        record = {'id': uuid.uuid4().hex, 'token': token, 'name': name, 'created': now(), 'expires': expires,
                  'device_id': normalize_device_id(device_id), 'device_name': normalize_device_name(device_name), 'calls': 0, 'last_call': None}
        self.backend.save(account, record)
        return public(record)

    def update(self, account, link_id, name=None, expires=None):
        account = normalize_account(account)
        record = self.backend.get(account, check_link_id(link_id))
        if record is None:
            raise NotFound()
        if name is not None:
            record['name'] = check_name(name)
        if expires is not None:
            record['expires'] = check_expires(expires)
        self.backend.save(account, record)
        return public(record)

    def delete(self, account, link_id):
        account = normalize_account(account)
        record = self.backend.get(account, check_link_id(link_id))
        if record is None:
            raise NotFound()
        self.backend.remove(account, record)
        return public(record)

    def resolve(self, token):
        """(account, link) of a token that may be called now, else None."""
        if not is_token(token):
            return None
        found = self.backend.by_token(token)
        if found is None:
            return None
        account, link_id = found
        record = self.backend.get(account, link_id)
        if record is None or record['token'] != token or record['expires'] <= now():
            return None
        return account, record

    def purge(self, account):
        account = normalize_account(account)
        records = self.backend.records(account)
        for record in records:
            self.backend.remove(account, record)
        return len(records)

    def count_call(self, account, record):
        record['calls'] = (record.get('calls') or 0) + 1
        record['last_call'] = now()
        self.backend.count_call(account, record)
