# Private key: from the own XCAP contact to account storage

Today each client escrows the account's PGP keypair on the user's own contact
in the XCAP addressbook, so a new device can restore it. This moves it to
SylkServer account storage (`/webrtcgateway/storage/...`, see
`account_storage.py`), next to the avatars, in Blink Qt, Blink for macOS and
Sylk Mobile. sylk-desktop is not migrated (see "sylk-desktop" below).

## Today (step 0, checked 27 Oct 2026)

The escrow is a JSON `keys` attribute in the
`urn:ag-projects:sipsimple:xml:ns:addressbook` attribute bag, on every contact
that carries the account's own URI (Blink Qt `blink/key_escrow.py`, ported from
Blink for macOS `KeyEscrow.py`; Sylk Mobile `app.js`):

    {"private_key": <armoured PGP message: the private key encrypted
                     symmetrically with the ACCOUNT PASSWORD>,
     "public_key":  <armoured, in clear>,
     "device":      "motorola razr 60 ultra",
     "timestamp":   "2026-07-10T12:44:19.049Z"}

- The newest record (by `timestamp`) wins when several contacts carry one.
- The public key is in clear, so any client tells whose key it is without the
  password.
- When the account password changes, the clients re-encrypt the escrow once a
  registration has succeeded with the new password.
- Much of the client logic exists only because of XCAP: several contacts with
  our URI, one contact carrying the URIs of two of our accounts (one `keys`
  attribute for two keypairs), the self contact having to be created first.
  Storage is per account, so these go away with the XCAP copy.

## Why
- The addressbook is the most widely copied document there is: every device,
  every XCAP tool, every full GET/PUT on any contact edit. The key rides along
  with all of them.
- Healing and merge passes rewrite contacts; the own contact holding the key is
  one bad fold away from losing it.
- Account storage gives one object with its own ETag, conditional writes and a
  change notification, decoupled from contact edits.

## Target convention

| | |
|---|---|
| Namespace | `keys` |
| Object id | `pgp-private` (one per account, in that account's own storage) |
| Body | The escrow record above, the same JSON, byte for byte as a client would write it to XCAP |
| Content-Type | `application/json` |
| Create | `PUT` with `If-None-Match: *` and `X-Sylk-Device-Id` |
| Replace | `PUT` with `If-Match: "<etag>"`. 412: another device replaced it first; fetch and decide again |
| Delete | Only on an explicit user action, with `If-Match` |

The server sees what XCAP sees today: the key encrypted with the account
password, and the public key.

### Client behaviour
- The storage record is one more escrow record next to the XCAP ones. All the
  existing rules (restore, repair, re-encrypt on password change, replace only
  when the user says so, newest timestamp wins) apply to it unchanged.
- Fetched when the addressbook loads and on `sylk-storage-changed` with
  `namespace == "keys"` from another device. The escrow decisions wait for
  that answer (or its failure), as they wait for the addressbook today.
- A write of the escrow goes to storage and to XCAP (Phase 1), storage first.
- 401: refresh the token once. No SylkServer, or storage disabled (404 on the
  listing): XCAP only, as today.

## Server changes (checked against the tree, 27 Oct 2026)

What already works, unchanged:
- `PUT/GET/DELETE /storage/<account>/keys/pgp-private` over HTTPS with the API
  token: names, size (256 KB limit), conditional writes, ETag/304, both
  backends, `Cache-Control: private, no-cache`.
- `application/sylk-storage-changed` is sent to the account and kept out of the
  journal (`sip_handlers.py`).
- The admin account purge (`web.py` `purge_account`) also removes the key
  backup. Intended: a purged account has no backup either.
- `text/pgp-private-key` sent device to device over MESSAGE is already never
  journaled (`storage.py`, both backends). Not affected by this change.

Changed (27 Oct 2026):

1. **Quota exemption for `keys`** (`account_storage.py`, `check_object()`,
   used by `put()` of both backends). Objects in `keys` are outside the account
   byte/object quota, so an account full of avatars still stores its key.
2. **`keys` is restricted** (`RESERVED_NAMESPACES`): only the id
   `pgp-private` (400 otherwise), at most 64 KB (413).
3. **WebSocket access for Sylk Mobile** (`private_key.py`,
   models in `models/sylkrtc.py`, handlers in `handler.py`). Two requests of
   their own rather than a field of the addressbook fetch, so a
   `sylk-storage-changed` for `keys` refetches only the key:
   - `account-fetch-private-key {account, etag?}` → event `private-key`,
     action `fetched`, `privateKey` = {etag, contentType, size, modified,
     deviceId, code, data?}. `data` (base64) only with code 200; the client's
     etag unchanged: 304 without data; nothing stored: 404.
   - `account-store-private-key {account, data, contentType?, deviceId?,
     ifMatch?, ifNoneMatch?}` (data base64; `ifNoneMatch: "*"` creates only,
     `ifMatch` replaces only that etag) → event `private-key`, action
     `stored`, with the stored meta and code 201/200, or {code, error}
     (400 bad data, 412 another device replaced it first, 413 too large).
   - A store is announced with `application/sylk-storage-changed` like an
     HTTP PUT, with the device id. WebSocket accounts receive that MESSAGE
     live (`sip_handlers.py` `_store_message_for_receiver`).
   - Client side: the two requests go into Sylk Mobile's
     `patches/react-native-sylkrtc+2.1.0.patch`. Not added to sylkrtc.js:
     sylk-desktop is not migrated.
4. **Logging**: writes to `keys` are logged with the writing device
   (`by device <id>`, or `unknown`), over HTTP (`web.py`) and WebSocket.

5. Not needed: no new Cassandra table, no server-side migration (the server has
   no XCAP credentials for SIP-only clients, so migration is a client job).

Side note: the token reply still contains `storage_url` (`sip_handlers.py`
`_handle_generate_token`), though the protocol doc says clients derive it.
Harmless; drop it or update the doc.

## Migration phases

### Phase 1: storage and XCAP, kept equal
Every client:
- Reads both; the newest record wins, wherever it is.
- Any escrow write goes to both: storage first, then XCAP.
- Keeps the two equal, without the password and without holding the key (the
  record is copied as it is):
  - XCAP has a record, storage none → upload it (`If-None-Match: *`; 412
    means another device did, fine).
  - XCAP newer than storage → an old client (or sylk-desktop) wrote it:
    upload it (`If-Match` the storage etag).
  - Storage newer than XCAP → write it to the own contact(s), with the same
    guards as any escrow write (a contact shared with another of our accounts
    is never written).

Old clients keep working unchanged: they read and write XCAP, and Phase 1
clients follow them.

### Phase 2: storage only
- Clients stop writing the key to XCAP; they still read it when storage has
  none.

### Phase 3: remove from XCAP
- A client that sees the key in storage and the attribute still on the own
  contact removes the attribute (normal addressbook write, so the update
  notification goes out).
- Healing rule in all clients and `sylk-addressbook --check`: the own contact
  carries no private key when storage has one.

## Deployment sequence

| Step | What | Gate |
|---|---|---|
| 0 | Confirm today's XCAP attribute and blob format (done: same record in Qt, Cocoa, mobile) | |
| 1 | SylkServer: changes 1–4. Deployed 27 Oct 2026 | curl PUT/GET/DELETE on `keys/pgp-private`; quota full of avatars still accepts the key; WebSocket fetch/store from a test client |
| 2 | Blink Qt, Phase 1 (done 27 Oct 2026) | Key appears in storage, XCAP unchanged, clean profile restores from storage |
| 3 | Blink for macOS, Phase 1, ported from Qt (done 27 Oct 2026; from scratch: copied the phone's escrow, same etag as Qt's copy) | Replace the key on Mac → Qt sees the newer record live; write one from an old build → both copy it to storage |
| 4 | Sylk Mobile, Phase 1 (react-native-sylkrtc patch + app/keyStorage.js + app.js; tested 27 Oct 2026), store release | Live on both stores |
| 5 | Wait ~3 months, or until no XCAP-only key writers are seen | |
| 6 | Phase 2, same client order | Mobile Phase 2 live |
| 7 | Wait ~1 month | |
| 8 | Phase 3 in all clients and `sylk-addressbook` (see sylk-desktop below) | |

Server first because every client phase depends on it and it is backwards
compatible (new namespace rules, optional WebSocket fields). Desktops next
because their rollout is under our control; mobile last because store releases
and user updates are the slow edge, and every later phase waits on it.

## Who wrote it

Each object keeps the device that wrote it: `X-Sylk-Device-Id` (an id) and
`X-Sylk-Device-Name` (for humans, percent-encoded UTF-8; WebSocket: `deviceName`),
column `device_name` in Cassandra. Both come back in the listing, as GET headers
and in `sylk-storage-changed`. The clients send:

| Client | Name |
|---|---|
| Blink Qt | `Blink Qt on <host> (Linux)` |
| Blink for macOS | `Blink on <computer name> (macOS)` |
| Sylk Mobile | `Sylk on <model> (Android)` / `(iOS)` |
| sylk-account | `sylk-account on <host>` |

The escrow record also says which device escrowed the key (`device`), which is
not always the one that stored the object: a copy keeps the record as it is.

`sylk-account` (python3-sylkrtc) lists, shows and deletes the objects.

## Devices

The list of the devices that use the account (namespace `devices`) is in
`docs/ACCOUNT-DEVICES.md`.

## sylk-desktop

Not migrated: it keeps reading and writing the key on the own XCAP contact.

- Phase 1 copes with it both ways: a key it writes to XCAP is newer and is
  copied to storage, and a key written by another client reaches XCAP too.
- From Phase 2 on, a key changed on another client no longer reaches XCAP, so
  sylk-desktop would restore a stale key.
- Phase 3 removes the XCAP copy, so sylk-desktop could not restore at all.

Before Phase 2, either keep writing the XCAP copy for as long as sylk-desktop
is in use (Phase 2 and 3 postponed), or accept that sylk-desktop has no key
backup.

## Open decisions
- Sylk Mobile replaces an escrow of a key it does not hold with its own key
  ("superseded, replacing it"); Blink leaves that to the user. With the newest
  record deciding, this now applies to a newer key in the storage too. Align
  the two rules?
- sylk-desktop: keep the XCAP copy for it (postpone Phase 2/3), or drop its
  key backup.
- Key reset: PUT the new key over the old one with `If-Match` (one code path),
  or DELETE first.
- Phase 2 gate: time-based, log-based, or both.
