"""Private per-account object storage.

Objects are opaque bytes addressed by <account>/<namespace>/<id>, each with a
content type and a strong ETag (sha256 of the content). Only the account may
read or write its own objects.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid

from application.python.types import Singleton

from .configuration import AccountStorageConfig, CassandraConfig
from .logger import log
from .storage import CASSANDRA_MODULES_AVAILABLE

__all__ = 'AccountStorage', 'AccountStorageError', 'NotFound', 'PreconditionFailed', 'TooLarge', 'QuotaExceeded', 'InvalidName', 'RESERVED_NAMESPACES', 'announce_change', 'check_object'

STORAGE_CHANGED_CONTENT_TYPE = 'application/sylk-storage-changed'


class AccountStorageError(Exception): pass
class NotFound(AccountStorageError): pass
class PreconditionFailed(AccountStorageError): pass
class TooLarge(AccountStorageError): pass
class QuotaExceeded(AccountStorageError): pass
class InvalidName(AccountStorageError): pass


NAME_RE = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$')
ACCOUNT_RE = re.compile(r'^[^/\\\x00@]+@[A-Za-z0-9.-]+$')
CONTENT_TYPE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$')
META_PREFIX = '.meta-'

# Namespaces with fixed object ids and their own size limit. They are outside
# the account quota, so a namespace full of avatars can never block them, and
# the fixed ids keep the exemption from being used as free storage.
#   keys/pgp-private: the account's PGP private key, encrypted by the client
#   with the user's backup passphrase (docs/PRIVATE-KEY-STORAGE.md).
RESERVED_NAMESPACES = {'keys': {'ids': frozenset({'pgp-private'}), 'max_object_size': 65536}}


def normalize_account(account):
    account = (account or '').strip().lower()
    if account.startswith('sip:'):
        account = account[4:]
    if not ACCOUNT_RE.match(account) or account.startswith('.'):
        raise InvalidName('invalid account')
    return account


def check_name(name, what):
    if not isinstance(name, str) or not NAME_RE.match(name) or name in ('.', '..'):
        raise InvalidName(f'invalid {what}')
    return name


def check_object(namespace, object_id, data):
    """Validate an object about to be stored. Returns True when it is
    outside the account quota (a reserved namespace)."""
    rules = RESERVED_NAMESPACES.get(namespace)
    if rules is None:
        if len(data) > AccountStorageConfig.max_object_size:
            raise TooLarge(f'object larger than {AccountStorageConfig.max_object_size} bytes')
        return False
    if object_id not in rules['ids']:
        raise InvalidName(f'invalid id for namespace {namespace}')
    if len(data) > rules['max_object_size']:
        raise TooLarge(f'object larger than {rules["max_object_size"]} bytes')
    return True


def normalize_content_type(content_type):
    content_type = (content_type or '').split(';', 1)[0].strip().lower()
    return content_type if CONTENT_TYPE_RE.match(content_type) else 'application/octet-stream'


def etag_matches(header, etag):
    """True when an If-Match / If-None-Match header value matches etag ('*' matches any existing object)."""
    if header is None:
        return False
    for value in header.split(','):
        value = value.strip()
        if value.startswith('W/'):
            value = value[2:]
        if value == '*' or value.strip('"') == etag:
            return True
    return False


class FileAccountStorage(object):
    def __init__(self):
        self._locks = {}
        self._locks_lock = threading.Lock()

    @property
    def root(self):
        return AccountStorageConfig.storage_dir.normalized

    def _lock(self, account):
        with self._locks_lock:
            return self._locks.setdefault(account, threading.Lock())

    @staticmethod
    def _limits():
        return {'max_object_size': AccountStorageConfig.max_object_size,
                'max_account_size': AccountStorageConfig.max_account_size,
                'max_objects': AccountStorageConfig.max_objects}

    def account_dir(self, account):
        account = normalize_account(account)
        return os.path.join(self.root, account[0], account)

    def _paths(self, account, namespace, object_id):
        folder = os.path.join(self.account_dir(account), check_name(namespace, 'namespace'))
        check_name(object_id, 'id')
        return folder, os.path.join(folder, object_id), os.path.join(folder, META_PREFIX + object_id)

    @staticmethod
    def _read_meta(meta_path):
        try:
            with open(meta_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_atomic(path, data, mode='wb'):
        tmp = f'{path}.tmp-{uuid.uuid4().hex}'
        with open(tmp, mode) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def usage(self, account):
        """(bytes, objects) used by the account."""
        size = count = 0
        base = self.account_dir(account)
        if not os.path.isdir(base):
            return 0, 0
        for namespace in os.listdir(base):
            folder = os.path.join(base, namespace)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.startswith('.'):
                    continue
                try:
                    size += os.path.getsize(os.path.join(folder, name))
                    count += 1
                except OSError:
                    pass
        return size, count

    def namespaces(self, account):
        base = self.account_dir(account)
        result = []
        if os.path.isdir(base):
            for namespace in sorted(os.listdir(base)):
                folder = os.path.join(base, namespace)
                if not os.path.isdir(folder) or not NAME_RE.match(namespace):
                    continue
                objects = [name for name in os.listdir(folder) if not name.startswith('.')]
                size = sum(os.path.getsize(os.path.join(folder, name)) for name in objects if os.path.isfile(os.path.join(folder, name)))
                result.append({'namespace': namespace, 'objects': len(objects), 'size': size})
        size, count = self.usage(account)
        return {'account': normalize_account(account),
                'namespaces': result,
                'usage': {'size': size, 'objects': count},
                'limits': self._limits()}

    def index(self, account, namespace):
        folder = os.path.join(self.account_dir(account), check_name(namespace, 'namespace'))
        objects = []
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                if name.startswith('.'):
                    continue
                meta = self._read_meta(os.path.join(folder, META_PREFIX + name))
                if meta is None:
                    continue
                objects.append(meta)
        return {'account': normalize_account(account), 'namespace': namespace, 'objects': objects}

    def get(self, account, namespace, object_id):
        """(meta, data) of an object."""
        folder, path, meta_path = self._paths(account, namespace, object_id)
        meta = self._read_meta(meta_path)
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            raise NotFound()
        if meta is None:
            raise NotFound()
        return meta, data

    def put(self, account, namespace, object_id, data, content_type, if_match=None, if_none_match=None, device_id=None):
        """Store an object. Returns (meta, created)."""
        folder, path, meta_path = self._paths(account, namespace, object_id)
        exempt = check_object(namespace, object_id, data)
        with self._lock(normalize_account(account)):
            current = self._read_meta(meta_path) if os.path.isfile(path) else None
            if if_match is not None and (current is None or not etag_matches(if_match, current['etag'])):
                raise PreconditionFailed()
            if if_none_match is not None and current is not None and etag_matches(if_none_match, current['etag']):
                raise PreconditionFailed()
            if not exempt:
                used, count = self.usage(account)
                if current is not None:
                    used -= current.get('size', 0)
                    count -= 1
                if used + len(data) > AccountStorageConfig.max_account_size or count + 1 > AccountStorageConfig.max_objects:
                    raise QuotaExceeded()
            meta = _new_meta(object_id, data, content_type, device_id)
            os.makedirs(folder, exist_ok=True)
            self._write_atomic(path, data)
            self._write_atomic(meta_path, json.dumps(meta), mode='w')
        return meta, current is None

    def delete(self, account, namespace, object_id, if_match=None):
        folder, path, meta_path = self._paths(account, namespace, object_id)
        with self._lock(normalize_account(account)):
            current = self._read_meta(meta_path)
            if current is None or not os.path.isfile(path):
                raise NotFound()
            if if_match is not None and not etag_matches(if_match, current['etag']):
                raise PreconditionFailed()
            for name in (path, meta_path):
                try:
                    os.remove(name)
                except OSError:
                    pass
            try:
                os.rmdir(folder)
            except OSError:
                pass
        return current

    def purge(self, account):
        """Remove everything stored for the account. Returns (files, bytes) removed."""
        from shutil import rmtree
        base = self.account_dir(account)
        size, count = self.usage(account)
        if os.path.isdir(base):
            rmtree(base, ignore_errors=True)
        return count, size


def announce_change(account, namespace, object_id, op, meta, device_id=None):
    """Tell every device of the account that an object changed: a MESSAGE
    from the account to itself, never journalled. Used by the HTTP routes and
    the WebSocket requests alike."""
    from .sip_handlers import MessageHandler
    payload = {'namespace': namespace, 'id': object_id, 'op': op,
               'etag': meta.get('etag'), 'size': meta.get('size'),
               'content_type': meta.get('content_type'), 'modified': meta.get('modified')}
    device_id = normalize_device_id(device_id)
    if device_id:
        payload['device_id'] = device_id
    uri = f'sip:{account}'
    MessageHandler().outgoing_message(uri, json.dumps(payload), STORAGE_CHANGED_CONTENT_TYPE, uri)


def normalize_device_id(device_id):
    """The X-Sylk-Device-Id of a writer as stored, or None."""
    value = (device_id or '').strip()[:128]
    return value or None


def _new_meta(object_id, data, content_type, device_id=None):
    meta = {'id': object_id,
            'etag': hashlib.sha256(data).hexdigest()[:40],
            'size': len(data),
            'content_type': normalize_content_type(content_type),
            'modified': int(time.time() * 1000)}
    # Which device uploaded the object; clients show it next to `modified`.
    device_id = normalize_device_id(device_id)
    if device_id:
        meta['device_id'] = device_id
    return meta


class CassandraAccountStorage(object):
    """One partition per account; concurrent writers on other servers are serialised with lightweight transactions."""

    META_FIELDS = ('namespace', 'object_id', 'etag', 'size', 'content_type', 'modified', 'device_id')

    @property
    def model(self):
        from .models.storage.cassandra import AccountObject
        return AccountObject

    @staticmethod
    def _meta(row):
        meta = {'id': row.object_id, 'etag': row.etag, 'size': row.size, 'content_type': row.content_type, 'modified': row.modified}
        if row.device_id:
            meta['device_id'] = row.device_id
        return meta

    def _rows(self, account, namespace=None):
        query = self.model.objects(account=account)
        if namespace is not None:
            query = query.filter(namespace=namespace)
        return query.only(list(self.META_FIELDS)).limit(None)

    def _current(self, account, namespace, object_id):
        try:
            return self.model.objects(account=account, namespace=namespace, object_id=object_id).only(list(self.META_FIELDS)).get()
        except self.model.DoesNotExist:
            return None

    def usage(self, account):
        account = normalize_account(account)
        size = count = 0
        for row in self._rows(account):
            size += row.size or 0
            count += 1
        return size, count

    def namespaces(self, account):
        account = normalize_account(account)
        totals = {}
        for row in self._rows(account):
            entry = totals.setdefault(row.namespace, {'namespace': row.namespace, 'objects': 0, 'size': 0})
            entry['objects'] += 1
            entry['size'] += row.size or 0
        result = [totals[name] for name in sorted(totals)]
        return {'account': account,
                'namespaces': result,
                'usage': {'size': sum(item['size'] for item in result), 'objects': sum(item['objects'] for item in result)},
                'limits': FileAccountStorage._limits()}

    def index(self, account, namespace):
        account = normalize_account(account)
        check_name(namespace, 'namespace')
        return {'account': account, 'namespace': namespace, 'objects': [self._meta(row) for row in self._rows(account, namespace)]}

    def get(self, account, namespace, object_id):
        """(meta, data) of an object."""
        account = normalize_account(account)
        check_name(namespace, 'namespace')
        check_name(object_id, 'id')
        try:
            row = self.model.objects(account=account, namespace=namespace, object_id=object_id).get()
        except self.model.DoesNotExist:
            raise NotFound()
        return self._meta(row), bytes(row.data or b'')

    def put(self, account, namespace, object_id, data, content_type, if_match=None, if_none_match=None, device_id=None):
        from cassandra.cqlengine.query import LWTException
        account = normalize_account(account)
        check_name(namespace, 'namespace')
        check_name(object_id, 'id')
        exempt = check_object(namespace, object_id, data)
        current = self._current(account, namespace, object_id)
        if if_match is not None and (current is None or not etag_matches(if_match, current.etag)):
            raise PreconditionFailed()
        if if_none_match is not None and current is not None and etag_matches(if_none_match, current.etag):
            raise PreconditionFailed()
        if not exempt:
            used, count = self.usage(account)
            if current is not None:
                used -= current.size or 0
                count -= 1
            if used + len(data) > AccountStorageConfig.max_account_size or count + 1 > AccountStorageConfig.max_objects:
                raise QuotaExceeded()
        meta = _new_meta(object_id, data, content_type, device_id)
        values = dict(etag=meta['etag'], size=meta['size'], content_type=meta['content_type'], modified=meta['modified'],
                      device_id=meta.get('device_id'), data=data)
        try:
            if current is None:
                self.model.if_not_exists().create(account=account, namespace=namespace, object_id=object_id, **values)
            else:
                self.model.objects(account=account, namespace=namespace, object_id=object_id).iff(etag=current.etag).update(**values)
        except LWTException:
            raise PreconditionFailed()
        return meta, current is None

    def delete(self, account, namespace, object_id, if_match=None):
        from cassandra.cqlengine.query import LWTException
        account = normalize_account(account)
        check_name(namespace, 'namespace')
        check_name(object_id, 'id')
        current = self._current(account, namespace, object_id)
        if current is None:
            raise NotFound()
        if if_match is not None and not etag_matches(if_match, current.etag):
            raise PreconditionFailed()
        try:
            self.model.objects(account=account, namespace=namespace, object_id=object_id).iff(etag=current.etag).delete()
        except LWTException:
            raise PreconditionFailed()
        return self._meta(current)

    def purge(self, account):
        account = normalize_account(account)
        size, count = self.usage(account)
        self.model.objects(account=account).delete()
        return count, size


class AccountStorage(object, metaclass=Singleton):
    def __new__(cls):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            # log.info('Using cassandra backend for account storage')
            return CassandraAccountStorage()
        log.info(f'Using file backend for account storage: {AccountStorageConfig.storage_dir.normalized}')
        return FileAccountStorage()
