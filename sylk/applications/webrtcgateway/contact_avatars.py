"""Contact avatars carried on the addressbook contacts of WebSocket clients.

The pictures live in account storage (account_storage.py, namespace
`avatars`, object id = contact id), never in XCAP. Blink talks to the storage
over HTTP; WebSocket clients (Sylk Mobile, sylk-desktop) get them through the
addressbook they already use:

- account-fetch-addressbook may carry `avatars`: [{id, etag}], the pictures
  the client has cached. Every contact with a stored picture then gets
  `avatar` = {etag, contentType, size, modified, deviceId}, plus `data`
  (base64) when the client's etag differs. Without `avatars` in the request
  nothing changes, so older clients are unaffected.
- account-update-addressbook for a contact may carry `avatar` = {data,
  contentType, deviceId} to store a picture, or {remove: true, deviceId}
  to delete it. Deleting the contact deletes its picture. The avatar is
  never written to XCAP. The result echoed to the devices carries the
  stored avatar with its data, {code: 204} after a removal, or {code, error}
  when that failed.

Every change is announced with application/sylk-storage-changed, as for the
HTTP routes, so Blink sees it too. See docs/CONTACT-AVATARS.md in Blink.
"""

import base64
import time

from twisted.internet import defer, threads

from . import account_storage
from .configuration import AccountStorageConfig
from .logger import log
from .models import xcap

__all__ = 'NAMESPACE', 'attach_avatars', 'apply_contact_avatar', 'remove_contact_avatar', 'strip_avatar'

NAMESPACE = 'avatars'

# An object no contact owns is deleted once it is this old: the contact of a
# fresh upload may not have reached the addressbook yet.
GC_MIN_AGE_MS = 7 * 24 * 3600 * 1000

_ERRORS = ((account_storage.InvalidName, 400), (account_storage.NotFound, 404),
           (account_storage.PreconditionFailed, 412), (account_storage.TooLarge, 413),
           (account_storage.QuotaExceeded, 507))


def _code(exception):
    for error_type, code in _ERRORS:
        if isinstance(exception, error_type):
            return code
    return 500


def _avatar(meta, data=None):
    return xcap.ContactAvatar(etag=meta['etag'], contentType=meta.get('content_type'),
                              size=meta.get('size'), modified=meta.get('modified'),
                              deviceId=meta.get('device_id'),
                              data=base64.b64encode(data).decode() if data is not None else None)


def strip_avatar(payload):
    """The contact payload as XCAP must see it: without `avatar`."""
    if isinstance(payload, dict):
        payload.pop('avatar', None)
    return payload


def _attach(account, addressbook, known):
    """In a thread: avatar meta on every contact with a stored picture, data
    where `known` (contact id -> etag the client has) differs. Returns the
    etags the client holds afterwards."""
    storage = account_storage.AccountStorage()
    account = account_storage.normalize_account(account)
    objects = {meta['id']: meta for meta in storage.index(account, NAMESPACE)['objects']}
    contacts = list(addressbook.contacts or [])
    sent = 0
    held = {}
    for contact in contacts:
        meta = objects.get(contact.id)
        if meta is None:
            contact.avatar = None
            continue
        data = None
        if known.get(contact.id) != meta['etag']:
            try:
                meta, data = storage.get(account, NAMESPACE, contact.id)
                sent += 1
            except account_storage.AccountStorageError:
                contact.avatar = None
                continue
        contact.avatar = _avatar(meta, data)
        held[contact.id] = meta['etag']

    # Garbage collection, only on a document that has contacts: an empty one
    # may be a failed fetch, and would sweep every picture.
    collected = 0
    if contacts:
        ids = {contact.id for contact in contacts}
        now = int(time.time() * 1000)
        for object_id, meta in objects.items():
            if object_id in ids or now - int(meta.get('modified') or 0) < GC_MIN_AGE_MS:
                continue
            try:
                storage.delete(account, NAMESPACE, object_id, if_match='"%s"' % meta['etag'])
                collected += 1
            except account_storage.AccountStorageError:
                pass
    log.info('Addressbook avatars for %s: %d stored, %d sent with data, %d orphans deleted'
             % (account, len(objects), sent, collected))
    return held


def attach_avatars(account_info, addressbook, known):
    """Deferred: the addressbook with avatars attached. Never fails: on an
    error the addressbook goes out without them."""
    if not AccountStorageConfig.enabled or addressbook is None:
        return defer.succeed(addressbook)

    def done(held):
        account_info.avatar_etags = held
        return addressbook

    def failed(failure):
        log.warning('Cannot attach avatars for %s: %s' % (account_info.id, failure.getErrorMessage()))
        return addressbook

    deferred = threads.deferToThread(_attach, account_info.id, addressbook, dict(known or {}))
    deferred.addCallbacks(done, failed)
    return deferred


def _put(account, object_id, avatar):
    storage = account_storage.AccountStorage()
    account = account_storage.normalize_account(account)
    try:
        data = base64.b64decode(avatar.data or '', validate=True)
    except (ValueError, TypeError):
        raise account_storage.InvalidName('avatar data is not base64')
    meta, created = storage.put(account, NAMESPACE, object_id, data, avatar.contentType or 'image/jpeg',
                                device_id=avatar.deviceId)
    return account, meta, data


def _remove(account, object_id):
    storage = account_storage.AccountStorage()
    account = account_storage.normalize_account(account)
    try:
        return account, storage.delete(account, NAMESPACE, object_id)
    except account_storage.NotFound:
        return account, None


def apply_contact_avatar(account_info, contact, avatar):
    """Store or remove the picture a contact update carried. Deferred: the
    ContactAvatar to echo -- the stored one with its data, {code: 204} when
    removed, or {code, error} when that failed."""
    if not AccountStorageConfig.enabled:
        return defer.succeed(xcap.ContactAvatar(code=404, error='account storage disabled'))
    device_id = avatar.deviceId

    if avatar.remove:
        def removed(result):
            account, meta = result
            if meta is not None:
                log.info('Account storage: %s removed %s/%s (contact update)' % (account, NAMESPACE, contact.id))
                account_storage.announce_change(account, NAMESPACE, contact.id, 'delete', meta, device_id)
            if account_info.avatar_etags is not None:
                account_info.avatar_etags.pop(contact.id, None)
            return xcap.ContactAvatar(code=204)
        deferred = threads.deferToThread(_remove, account_info.id, contact.id)
        deferred.addCallback(removed)
    else:
        def stored(result):
            account, meta, data = result
            log.info('Account storage: %s stored %s/%s (%d bytes, contact update)' % (account, NAMESPACE, contact.id, meta['size']))
            account_storage.announce_change(account, NAMESPACE, contact.id, 'put', meta, device_id)
            if account_info.avatar_etags is not None:
                account_info.avatar_etags[contact.id] = meta['etag']
            return _avatar(meta, data)
        deferred = threads.deferToThread(_put, account_info.id, contact.id, avatar)
        deferred.addCallback(stored)

    def failed(failure):
        code = _code(failure.value)
        if code == 500:
            log.error('Cannot store the avatar of %s for %s: %s' % (contact.id, account_info.id, failure.getErrorMessage()))
        return xcap.ContactAvatar(code=code, error=str(failure.value) or failure.value.__class__.__name__)
    deferred.addErrback(failed)
    return deferred


def remove_contact_avatar(account_info, contact_id):
    """A contact was deleted: its picture goes too (best effort)."""
    if not AccountStorageConfig.enabled or not contact_id:
        return defer.succeed(None)

    def removed(result):
        account, meta = result
        if meta is not None:
            log.info('Account storage: %s removed %s/%s (contact deleted)' % (account, NAMESPACE, contact_id))
            account_storage.announce_change(account, NAMESPACE, contact_id, 'delete', meta)
        if account_info.avatar_etags is not None:
            account_info.avatar_etags.pop(contact_id, None)

    deferred = threads.deferToThread(_remove, account_info.id, contact_id)
    deferred.addCallback(removed)
    deferred.addErrback(lambda failure: log.warning('Cannot remove the avatar of deleted contact %s: %s'
                                                    % (contact_id, failure.getErrorMessage())))
    return deferred
