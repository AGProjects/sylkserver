
"""Per-room SIP registration at a foreign domain.

Some deployments want to expose a conference room as a regular SIP account
on a third-party SIP provider (typically to make the room reachable from
the PSTN via a SIP-PSTN gateway). For each `[user@host]` section in
conference.ini whose `registrar_uri` is set, this module creates a
sipsimple Account at server startup, registers it perpetually with the
provider, and tags the binding so inbound calls hitting the registered
Contact land in the matching conference room.

Modelled after sip-register3 — uses the high-level Account / AccountManager
path (which internally drives sipsimple.core.Registration), so REGISTER
refresh, authentication challenges and Contact rewriting are handled by
the SDK.

The integration points with the rest of the conference application are:

* `RoomRegistrar.start(app)` is called from
  `ConferenceApplication.start()` once the conference application is up
  and the global `AccountManager` is running.

* `RoomRegistrar.stop()` is called from `ConferenceApplication.stop()`;
  it deletes the registered accounts which causes the SDK to send a
  REGISTER with Expires: 0 before tearing them down.

* `RoomRegistrar.room_for_inbound(request_uri, to_uri)` is the lookup
  used by `ConferenceApplication.incoming_session` to map an inbound
  INVITE's AOR back to its room.
"""

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.account import Account, AccountManager
from sipsimple.threading import run_in_twisted_thread
from zope.interface import implementer

from sylk.applications.conference.configuration import iter_registered_rooms
from sylk.applications.conference.logger import log


__all__ = 'RoomRegistrar',


def _normalise_aor(value):
    """Lowercase user@host with the SIP scheme stripped, for map keys."""
    if value is None:
        return None
    s = str(value)
    if s.startswith('sip:'):
        s = s[4:]
    elif s.startswith('sips:'):
        s = s[5:]
    # Strip URI parameters / headers after the host (`;transport=...`, `?…`).
    for sep in (';', '?'):
        i = s.find(sep)
        if i != -1:
            s = s[:i]
    return s.lower()


def _uri_to_aor(uri):
    """Convert a SIPURI / object with .user/.host into a lowercase aor key."""
    if uri is None:
        return None
    user = getattr(uri, 'user', None)
    host = getattr(uri, 'host', None)
    if user is None or host is None:
        return _normalise_aor(uri)
    if isinstance(user, bytes):
        try:
            user = user.decode()
        except Exception:
            return None
    if isinstance(host, bytes):
        try:
            host = host.decode()
        except Exception:
            return None
    return ('%s@%s' % (user, host)).lower()


@implementer(IObserver)
class RoomRegistrar(object):
    """Owns one sipsimple Account per registered conference room.

    The application instantiates a single RoomRegistrar at start, calls
    .start() once and .stop() once. After start, the registrar exposes
    .room_for_inbound() which the application uses on every inbound
    INVITE to decide whether the caller should be routed into one of
    the rooms whose registration matched the INVITE's AOR.
    """

    def __init__(self):
        # Map from normalised foreign AOR (lowercase user@host) to the
        # local room URI (the [section] name in conference.ini).
        self._aor_to_room = {}
        # Map from Account id (lowercase user@host) to the Account we
        # created, so .stop() can clean them up and notification
        # handlers can resolve the originating room URI.
        self._accounts = {}
        # Lowercase set of hostnames we care about for DNS-lookup logging.
        # Populated with the registrar domain and the outbound-proxy host
        # (when set) of every registered room. Used to filter the global
        # DNSLookup notifications down to lookups our REGISTER flows
        # actually trigger.
        self._registered_domains = set()
        # Whether we currently observe the global DNS notifications.
        self._observing_dns = False

    # --- lifecycle -----------------------------------------------------------

    def start(self):
        notification_center = NotificationCenter()
        try:
            registered = list(iter_registered_rooms())
        except Exception:
            log.exception('Failed to enumerate registered rooms')
            return
        if not registered:
            log.info('No rooms configured for SIP registration (no [room] '
                     'section in conference.ini has registrar_uri set).')
            return

        log.info('Conference registrar starting: %d room(s) will register' % len(registered))

        # Subscribe to global DNS notifications so we can log the lookup
        # that the Account does on every (re-)REGISTER. Filter by host in
        # the handlers — multiple sylk subsystems can do DNS lookups.
        for name in ('DNSLookupDidStart', 'DNSLookupDidSucceed', 'DNSLookupDidFail'):
            notification_center.add_observer(self, name=name)
        self._observing_dns = True

        account_manager = AccountManager()
        for room_uri, cfg in registered:
            try:
                self._create_account(account_manager, notification_center, room_uri, cfg)
            except Exception as e:
                log.error('Failed to create registration for room %r at %r: %s' %
                          (room_uri, cfg.registrar_uri, e))

    def stop(self):
        notification_center = NotificationCenter()
        if self._observing_dns:
            for name in ('DNSLookupDidStart', 'DNSLookupDidSucceed', 'DNSLookupDidFail'):
                try:
                    notification_center.remove_observer(self, name=name)
                except KeyError:
                    pass
            self._observing_dns = False
        for aid, account in list(self._accounts.items()):
            notification_center.discard_observer(self, sender=account)
            room = getattr(account, '_sylk_room_uri', None) or aid
            log.info('Room %s: unregistering %s' % (room, aid))
            try:
                # Delete unregisters with Expires: 0 and removes the
                # account from the AccountManager.
                account.delete()
            except Exception:
                log.exception('Failed to delete account %s', aid)
        self._accounts.clear()
        self._aor_to_room.clear()
        self._registered_domains.clear()

    # --- account creation ----------------------------------------------------

    def _create_account(self, account_manager, notification_center, room_uri, cfg):
        registrar_uri = str(cfg.registrar_uri)
        aor_key = _normalise_aor(registrar_uri)

        # If an account with this id already exists (e.g. left over from a
        # previous start, or the same registrar_uri reused on two rooms),
        # log and skip — sipsimple's MemoryStorage is per-process so the
        # first case shouldn't happen, but we refuse to double-bind.
        if aor_key in self._accounts:
            log.warning('Duplicate registrar_uri %r in conference.ini — '
                        'room %r ignored (already bound to %r)' %
                        (registrar_uri, room_uri, self._aor_to_room.get(aor_key)))
            return

        # Account(id) auto-registers itself with the AccountManager via
        # SettingsObject.__init__ → CFGSettingsObjectWasCreated.
        try:
            account = Account(registrar_uri)
        except Exception as e:
            log.error('Cannot create Account(%r) for room %r: %s' %
                      (registrar_uri, room_uri, e))
            return

        # Auth + registration knobs. Keep presence / xcap / mwi off —
        # we only want REGISTER, nothing else from the Account state
        # machine. Same posture as sip-register3.
        account.auth.password = cfg.password or ''
        account.sip.register = True
        if cfg.registrar_outbound_proxy is not None:
            account.sip.outbound_proxy = cfg.registrar_outbound_proxy
        account.presence.enabled = False
        account.message_summary.enabled = False
        account.xcap.enabled = False
        account.enabled = True

        # Stamp the originating room URI on the account so notification
        # handlers can log "Room X: registered ..." without needing a
        # reverse lookup table.
        account._sylk_room_uri = room_uri

        # Observe the account first, THEN save() — save() is what triggers
        # the activation that begins the REGISTER flow.
        notification_center.add_observer(self, sender=account)
        try:
            account.save()
        except Exception as e:
            log.error('Cannot save Account(%r) for room %r: %s' %
                      (registrar_uri, room_uri, e))
            notification_center.discard_observer(self, sender=account)
            try:
                account.delete()
            except Exception:
                pass
            return

        self._accounts[aor_key] = account
        self._aor_to_room[aor_key] = room_uri

        # Remember the hostnames we expect to see in DNS lookups, so the
        # global DNSLookup* handlers can log the right ones and stay
        # quiet about lookups other sylk subsystems initiate.
        domain = registrar_uri.split('@')[-1].lower()
        self._registered_domains.add(domain)
        if cfg.registrar_outbound_proxy is not None:
            self._registered_domains.add(str(cfg.registrar_outbound_proxy.host).lower())

        # User-facing summary of what's being set up. The actual
        # registration attempt is logged separately from
        # SIPAccountWillRegister so we can correlate attempt → DNS →
        # answer → success/fail.
        if cfg.registrar_outbound_proxy is not None:
            proxy_info = '%s:%d;transport=%s' % (
                cfg.registrar_outbound_proxy.host,
                cfg.registrar_outbound_proxy.port,
                cfg.registrar_outbound_proxy.transport,
            )
            log.info('Room %s: configured registration as %s via outbound proxy %s (auth user=%s)' %
                     (room_uri, registrar_uri, proxy_info, account.auth.username or registrar_uri.split('@')[0]))
        else:
            log.info('Room %s: configured registration as %s (DNS lookup of %s; auth user=%s)' %
                     (room_uri, registrar_uri, domain, account.auth.username or registrar_uri.split('@')[0]))

    # --- inbound routing -----------------------------------------------------

    def room_for_inbound(self, request_uri, to_uri):
        """Return the room URI to drop the caller into, or None.

        The match is done on lowercase user@host extracted from the
        Request-URI and the To header, in that order. We match against
        the FOREIGN AOR (the value of registrar_uri) — that's the AOR
        that appears on the wire when the foreign registrar / PSTN
        gateway routes the call back to our Contact.
        """
        for uri in (request_uri, to_uri):
            key = _uri_to_aor(uri)
            if key and key in self._aor_to_room:
                return self._aor_to_room[key]
        return None

    # --- notification fan-out -----------------------------------------------

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @staticmethod
    def _room_label(notification):
        account = notification.sender
        return getattr(account, '_sylk_room_uri', None) or getattr(account, 'id', '?')

    @staticmethod
    def _decode(value):
        if isinstance(value, bytes):
            try:
                return value.decode()
            except Exception:
                return repr(value)
        return value

    @staticmethod
    def _uri_host(uri):
        if uri is None:
            return None
        h = getattr(uri, 'host', None)
        if h is None:
            s = str(uri)
            if '@' in s:
                s = s.split('@', 1)[1]
            for sep in (':', ';', '?', '>'):
                i = s.find(sep)
                if i != -1:
                    s = s[:i]
            return s or None
        if isinstance(h, bytes):
            try:
                return h.decode()
            except Exception:
                return None
        return h

    def _dns_relevant(self, uri):
        host = self._uri_host(uri)
        return bool(host and host.lower() in self._registered_domains)

    # --- DNS lookup (global notifications, filtered to our domains) ---------

    @run_in_twisted_thread
    def _NH_DNSLookupDidStart(self, notification):
        uri = getattr(notification.data, 'uri', None)
        if not self._dns_relevant(uri):
            return
        log.info('Registrar DNS lookup starting for %s' % uri)

    @run_in_twisted_thread
    def _NH_DNSLookupDidSucceed(self, notification):
        uri = getattr(notification.data, 'uri', None)
        if not self._dns_relevant(uri):
            return
        routes = list(getattr(notification.data, 'result', None) or [])
        if not routes:
            log.warning('Registrar DNS lookup for %s succeeded but returned no routes' % uri)
            return
        formatted = ['%s:%d/%s' % (r.address, r.port, r.transport.upper()) for r in routes]
        log.info('Registrar DNS lookup for %s succeeded: %s' % (uri, ', '.join(formatted)))

    @run_in_twisted_thread
    def _NH_DNSLookupDidFail(self, notification):
        uri = getattr(notification.data, 'uri', None)
        if not self._dns_relevant(uri):
            return
        err = self._decode(getattr(notification.data, 'error', 'unknown error'))
        log.warning('Registrar DNS lookup for %s failed: %s' % (uri, err))

    # --- per-account REGISTER lifecycle -------------------------------------

    @run_in_twisted_thread
    def _NH_SIPAccountWillRegister(self, notification):
        # Fires once per (re-)registration attempt, before DNS / network
        # activity starts. Use the attempt counter to distinguish first
        # attempt from refresh.
        account = notification.sender
        room = self._room_label(notification)
        account._sylk_attempt_count = getattr(account, '_sylk_attempt_count', 0) + 1
        attempt = account._sylk_attempt_count
        if attempt == 1:
            log.info('Room %s: REGISTER attempt 1 for %s' % (room, account.id))
        else:
            verb = 'refresh' if getattr(account, '_sylk_registered_once', False) else 'retry'
            log.info('Room %s: REGISTER attempt %d (%s) for %s' % (room, attempt, verb, account.id))

    @run_in_twisted_thread
    def _NH_SIPAccountRegistrationGotAnswer(self, notification):
        # Every response from the registrar passes through here, including
        # auth challenges (401/407) that the SDK retries automatically
        # with the credentials we attached to account.auth. Logging them
        # explicitly makes credential / digest issues visible without
        # having to enable SIP tracing.
        code = notification.data.code
        if code is None:
            return
        room = self._room_label(notification)
        reason = self._decode(notification.data.reason)
        if code in (401, 407):
            log.info('Room %s: registrar challenged with %d %s — retrying with credentials' %
                     (room, code, reason))
            return
        if code >= 300:
            log.warning('Room %s: registrar answered %d %s' % (room, code, reason))
            return
        # 2xx responses are noisy on every refresh; the final success log
        # is emitted from _NH_SIPAccountRegistrationDidSucceed.

    @run_in_twisted_thread
    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        data = notification.data
        account = notification.sender
        room = self._room_label(notification)
        first_time = not getattr(account, '_sylk_registered_once', False)
        account._sylk_registered_once = True
        # Reset attempt counter — next REGISTER (refresh) starts a fresh count.
        account._sylk_attempt_count = 0
        verb = 'REGISTERED' if first_time else 'REFRESHED registration'
        try:
            registrar = data.registrar
            contact = data.contact_header.uri
            log.info('Room %s: %s as %s -> contact %s at %s:%d;transport=%s (expires %ds)' %
                     (room, verb, account.id, contact,
                      registrar.address, registrar.port, registrar.transport, data.expires))
        except Exception:
            log.info('Room %s: %s' % (room, verb))

    @run_in_twisted_thread
    def _NH_SIPAccountRegistrationDidFail(self, notification):
        room = self._room_label(notification)
        account = notification.sender
        err = self._decode(notification.data.error)
        retry = getattr(notification.data, 'retry_after', None)
        # Reset attempt counter so the next attempt logged from
        # SIPAccountWillRegister starts at 1 — clearer in the log when
        # the next round begins.
        account._sylk_attempt_count = 0
        if retry is not None:
            log.warning('Room %s: registration failed: %s (retry in %.2fs)' % (room, err, retry))
        else:
            log.warning('Room %s: registration failed: %s' % (room, err))

    @run_in_twisted_thread
    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        room = self._room_label(notification)
        log.info('Room %s: registration ended' % room)

    @run_in_twisted_thread
    def _NH_SIPAccountRegistrationDidNotEnd(self, notification):
        # Fires when unregister at shutdown didn't get a clean response
        # — usually a network blip or the registrar going away. Worth
        # logging but not actionable.
        room = self._room_label(notification)
        code = getattr(notification.data, 'code', None)
        reason = self._decode(getattr(notification.data, 'reason', None))
        log.warning('Room %s: unregister did not complete cleanly: %s %s' % (room, code, reason))
