"""The account's PGP private key backup for WebSocket clients.

The key lives in account storage (account_storage.py) as keys/pgp-private:
the clients' escrow record, JSON with the private key encrypted with the
account password and the public key in clear, never a plaintext key. The
server does not look inside it. Blink talks to the storage over HTTP; WebSocket
clients (Sylk Mobile, sylk-desktop) use two requests:

- account-fetch-private-key {etag?}: answered with a `private-key` event,
  action `fetched`, carrying `privateKey` = {etag, contentType, size,
  modified, deviceId, code}, plus `data` (base64) when the client's etag
  differs (code 200). Same etag: no data, code 304. None stored: code 404.
- account-store-private-key {data, contentType?, deviceId?, ifMatch?,
  ifNoneMatch?}: answered with a `private-key` event, action `stored`,
  carrying the stored meta (code 201 or 200, no data) or {code, error}
  (412 when the precondition failed: another device replaced it first).

Every store is announced with application/sylk-storage-changed, as for the
HTTP routes, so the other devices fetch it. See docs/PRIVATE-KEY-STORAGE.md.
"""

import base64

from twisted.internet import defer, threads

from . import account_storage
from .configuration import AccountStorageConfig
from .logger import log
from .models import sylkrtc

__all__ = 'NAMESPACE', 'OBJECT_ID', 'fetch_private_key', 'store_private_key'

NAMESPACE = 'keys'
OBJECT_ID = 'pgp-private'
CONTENT_TYPE = 'application/json'

_ERRORS = ((account_storage.InvalidName, 400), (account_storage.NotFound, 404),
           (account_storage.PreconditionFailed, 412), (account_storage.TooLarge, 413),
           (account_storage.QuotaExceeded, 507))


def _code(exception):
    for error_type, code in _ERRORS:
        if isinstance(exception, error_type):
            return code
    return 500


def _key(meta, data=None, code=200):
    return sylkrtc.PrivateKey(etag=meta['etag'], contentType=meta.get('content_type'),
                              size=meta.get('size'), modified=meta.get('modified'),
                              deviceId=meta.get('device_id'), code=code,
                              data=base64.b64encode(data).decode() if data is not None else None)


def _failed(account_info, what):
    def failed(failure):
        code = _code(failure.value)
        if code == 500:
            log.error('Cannot %s the private key of %s: %s' % (what, account_info.id, failure.getErrorMessage()))
        return sylkrtc.PrivateKey(code=code, error=str(failure.value) or failure.value.__class__.__name__)
    return failed


def _get(account, etag):
    storage = account_storage.AccountStorage()
    meta, data = storage.get(account_storage.normalize_account(account), NAMESPACE, OBJECT_ID)
    if etag and account_storage.etag_matches(etag, meta['etag']):
        return _key(meta, code=304)
    return _key(meta, data)


def fetch_private_key(account_info, etag=None):
    """Deferred: the PrivateKey to send. Never fails."""
    if not AccountStorageConfig.enabled:
        return defer.succeed(sylkrtc.PrivateKey(code=404, error='account storage disabled'))
    deferred = threads.deferToThread(_get, account_info.id, etag)
    deferred.addErrback(_failed(account_info, 'fetch'))
    return deferred


def _put(account, data, content_type, if_match, if_none_match, device_id):
    storage = account_storage.AccountStorage()
    account = account_storage.normalize_account(account)
    try:
        data = base64.b64decode(data or '', validate=True)
    except (ValueError, TypeError):
        raise account_storage.InvalidName('private key data is not base64')
    if not data:
        raise account_storage.InvalidName('private key data is empty')
    meta, created = storage.put(account, NAMESPACE, OBJECT_ID, data, content_type or CONTENT_TYPE,
                                if_match=if_match, if_none_match=if_none_match, device_id=device_id)
    return account, meta, created


def store_private_key(account_info, request):
    """Store the key a client sent. Deferred: the PrivateKey to echo. Never fails."""
    if not AccountStorageConfig.enabled:
        return defer.succeed(sylkrtc.PrivateKey(code=404, error='account storage disabled'))
    device_id = request.deviceId

    def stored(result):
        account, meta, created = result
        log.info('Account storage: %s %s %s/%s (%d bytes, WebSocket) by device %s'
                 % (account, 'created' if created else 'updated', NAMESPACE, OBJECT_ID, meta['size'],
                    account_storage.normalize_device_id(device_id) or 'unknown'))
        account_storage.announce_change(account, NAMESPACE, OBJECT_ID, 'put', meta, device_id)
        return _key(meta, code=201 if created else 200)

    deferred = threads.deferToThread(_put, account_info.id, request.data, request.contentType,
                                     request.ifMatch, request.ifNoneMatch, device_id)
    deferred.addCallback(stored)
    deferred.addErrback(_failed(account_info, 'store'))
    return deferred
