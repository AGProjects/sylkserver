"""The devices that use an account, kept in its account storage.

Namespace `devices` (account_storage.py), one object per device, written by
the server only (clients read it, and may delete an entry to forget a device):

    {"device_id": "7c6150fd-...", "device_name": "Blink Qt on ag-xps (Linux)",
     "user_agent": "Blink Qt 6.2.0 (Linux)",
     "first_seen": <ms>, "first_ip": "...", "last_seen": <ms>, "last_ip": "...",
     "last_via": "websocket" | "sip" | "https"}

A device is seen when it proves it is the account's:
  websocket  account-register succeeded (account-add may carry deviceId, deviceName)
  sip        an application/sylk-api-token request (headers X-Sylk-Device-Id,
             X-Sylk-Device-Name; the client address is the bottom Via)
  https      a request with a valid API token (history, storage) carrying
             X-Sylk-Device-Id (and X-Sylk-Device-Name); its user agent is
             X-Sylk-User-Agent, never User-Agent (the HTTP library's)

A client that says nothing about itself is kept under an id made from its
User-Agent (ua-<hash>). The first sighting is written at once and announced
with application/sylk-storage-changed (a new device used the account); after
that last_seen is refreshed at most every REFRESH_INTERVAL, unannounced.
See docs/ACCOUNT-DEVICES.md.
"""

import hashlib
import json
import threading
import time

from twisted.internet import threads

from . import account_storage
from .configuration import AccountStorageConfig
from .logger import log

__all__ = 'NAMESPACE', 'seen', 'device_key', 'forwarded_ip'

NAMESPACE = 'devices'
CONTENT_TYPE = 'application/json'
REFRESH_INTERVAL = 600 * 1000       # ms between two last_seen writes of a device

_written = {}                       # (account, device id) -> ms of the last write by this server
_lock = threading.Lock()


def device_key(device_id, user_agent):
    """The object id of a device: its id when it is a valid name, else one made from it, else
    from its User-Agent; None when there is nothing to tell it by."""
    device_id = account_storage.normalize_device_id(device_id)
    if device_id:
        if account_storage.NAME_RE.match(device_id) and device_id not in ('.', '..'):
            return device_id
        return 'id-' + hashlib.sha1(device_id.encode('utf-8')).hexdigest()[:20]
    user_agent = (user_agent or '').strip()
    if user_agent:
        return 'ua-' + hashlib.sha1(user_agent.encode('utf-8')).hexdigest()[:20]
    return None


def forwarded_ip(header, peer):
    """The client address of an HTTP request: the first X-Forwarded-For entry, else the peer."""
    if header:
        first = header.split(',')[0].strip()
        if first:
            return first
    return peer


def _update(account, key, device_id, device_name, user_agent, ip, via, now):
    storage = account_storage.AccountStorage()
    try:
        meta, data = storage.get(account, NAMESPACE, key)
        entry = json.loads(data.decode('utf-8'))
        if not isinstance(entry, dict):
            entry = {}
        new = False
    except account_storage.NotFound:
        meta, entry, new = None, {}, True
    except (ValueError, UnicodeDecodeError):
        meta, entry, new = None, {}, False
    entry.setdefault('device_id', account_storage.normalize_device_id(device_id) or key)
    entry.setdefault('first_seen', now)
    if ip:
        entry.setdefault('first_ip', ip)
        entry['last_ip'] = ip
    entry['last_seen'] = now
    entry['last_via'] = via
    name = account_storage.normalize_device_name(device_name)
    if name:
        entry['device_name'] = name
    if user_agent:
        entry['user_agent'] = ' '.join(str(user_agent).split())[:200]
    body = json.dumps(entry, sort_keys=True).encode('utf-8')
    meta, created = storage.put(account, NAMESPACE, key, body, CONTENT_TYPE,
                                device_id=device_id, device_name=name or entry.get('device_name'))
    return meta, created and new, entry


def seen(account, device_id=None, device_name=None, user_agent=None, ip=None, via=None):
    """A device of the account was seen (in any thread). Writes in the background; never fails."""
    if not AccountStorageConfig.enabled:
        return
    try:
        account = account_storage.normalize_account(account)
    except account_storage.InvalidName:
        return
    key = device_key(device_id, user_agent)
    if key is None:
        return
    now = int(time.time() * 1000)
    with _lock:
        last = _written.get((account, key))
        if last is not None and now - last < REFRESH_INTERVAL:
            return
        _written[(account, key)] = now

    def done(result):
        meta, new, entry = result
        if new:
            log.info('Devices of %s: new device %s (%s, %s, via %s)'
                     % (account, account_storage.writer(device_id, entry.get('device_name')),
                        entry.get('user_agent') or '-', entry.get('last_ip') or '-', via))
            from twisted.internet import reactor
            reactor.callFromThread(account_storage.announce_change, account, NAMESPACE, key, 'put', meta,
                                   device_id, entry.get('device_name'))
        else:
            log.debug('Devices of %s: %s seen again via %s from %s' % (account, key, via, ip or '-'))

    def failed(failure):
        with _lock:
            _written.pop((account, key), None)
        log.warning('Devices of %s: cannot record %s: %s' % (account, key, failure.getErrorMessage()))

    deferred = threads.deferToThread(_update, account, key, device_id, device_name, user_agent, ip, via or '-', now)
    deferred.addCallbacks(done, failed)
