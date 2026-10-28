# Devices of an account

SylkServer keeps, per account, the list of devices that use it: which device,
when it was used first and last, and from which IP address. The list lives in
the account storage (`/webrtcgateway/storage/<account>`, see
`account_storage.py`), namespace `devices`, one object per device. The server
writes it; clients read it.

Implementation: `sylk/applications/webrtcgateway/devices.py`.

## The object

`devices/<device id>`, `application/json`:

```json
{"device_id": "7c6150fd-7978-4838-a1a6-422883ab496c",
 "device_name": "Blink Qt on ag-xps (Linux)",
 "user_agent": "Blink Qt 6.2.0 (Linux)",
 "first_seen": 1793130000000, "first_ip": "148.227.105.17",
 "last_seen": 1793139000000, "last_ip": "148.227.105.17",
 "last_via": "sip"}
```

| Field | |
|---|---|
| `device_id` | The id the device gave (`X-Sylk-Device-Id`, `deviceId`), or the object id |
| `device_name` | For humans, as the device names itself. No version in it |
| `user_agent` | The client and its version, from SIP or the WebSocket. Updated on every recorded use |
| `first_seen`, `first_ip` | The first recorded use. Never changed |
| `last_seen`, `last_ip`, `last_via` | The latest recorded use |

Times are milliseconds since the epoch, server time.

## When a device is recorded

A device is recorded when it proves it is the account's:

| `last_via` | When | Device id and name | IP |
|---|---|---|---|
| `websocket` | `account-register` succeeded | `account-add` fields `deviceId`, `deviceName` | the WebSocket peer |
| `sip` | an `application/sylk-api-token` MESSAGE (SIP-authenticated) | SIP headers `X-Sylk-Device-Id`, `X-Sylk-Device-Name` | the bottom Via: its `received` parameter, else its host |
| `https` | a request with a valid API token (message history, account storage) | HTTP headers `X-Sylk-Device-Id`, `X-Sylk-Device-Name`, `X-Sylk-User-Agent` (the client's SIP/WebRTC user agent; `User-Agent` is ignored, it is the HTTP library's) | the first `X-Forwarded-For` entry, else the peer |

- `X-Sylk-Device-Name` is UTF-8, percent-encoded (a header is latin-1).
- Over HTTPS only a request carrying `X-Sylk-Device-Id` is recorded, and it
  does not change `user_agent` (that would be the HTTP library's).
- A client that does not say which device it is (an older build) is kept
  under `ua-<sha1 of its User-Agent>`. Its User-Agent usually carries the
  version, so each version of such a build gets its own entry. A device id
  that is not a valid storage name is kept under `id-<sha1 of it>`.

## Writing and announcing

- The first recorded use writes the object and is announced to the account's
  devices with `application/sylk-storage-changed` (namespace `devices`, op
  `put`): a new device used the account.
- After that, `last_seen` and the rest are written at most once every
  10 minutes per device (per SylkServer process), unannounced.
- Writes are unconditional; with several SylkServers the latest write wins.

## Rules of the namespace

- Written by the server only: a client PUT gets 403.
- A client may list it, read an object, and DELETE one to forget a device.
  A device that is used again is recorded again, as new.
- Outside the account quota, at most 4 KB per object.
- The admin account purge removes it with the rest of the account storage.

## What the clients send

| Client | Device id | Name | How |
|---|---|---|---|
| Blink Qt | the SIP instance id | `Blink Qt on <host> (Linux)` | headers on the token request and on every HTTPS request with the token (journal, storage, file transfer, data management) |
| Blink for macOS | the SIP instance id | `Blink on <computer name> (macOS)` | the same |
| Sylk Mobile | `getUniqueIdSync()` | `Sylk on <model> (Android)` / `(iOS)` | `account-add` (react-native-sylkrtc patch: `connection.addAccount` options `deviceId`, `deviceName`) |
| sylk-account | its own, in `~/.sylkclient/device_id` | `sylk-account on <host>` | headers on every request |

The ids do not change across upgrades, so an upgraded device keeps its entry;
only `user_agent` follows the version. The same id and name are sent with
every write to the account storage (who wrote an object: `device_id`,
`device_name` in its metadata).

## Reading it

```
sylk-account devices                        the list: first and last use, IP, how (* this one)
sylk-account get devices <id>               one entry
sylk-account delete devices <id>            forget a device
sylk-account -j devices                     the records as JSON
```

## Deploying

No schema change: the objects go in the existing `account_storage` table.
A gateway without the list ignores the new `account-add` fields and headers,
so the clients can be updated in any order.
