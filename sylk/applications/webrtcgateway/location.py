
"""The cleartext lifecycle envelope of an ``application/sylk-location-sharing`` tick.

A location tick has two halves: the coordinates, PGP-armoured and readable only
by the recipient, and a cleartext envelope naming what the tick *is* --
``action``, ``sessionId``, ``expires``, ``perm``, ``deviceId``, ``version``.
The server reads only the envelope: to decide whether a tick warrants a push,
to group a share's update trail under its origin, and to label rows in the
admin UI.

There are two payload versions, and the envelope's ``version`` field says which
one a tick uses:

**version 1** -- the envelope and the ciphertext travel together in the message
body. ``metadata``, when a client sets it at all, is only a mirror of the body
and is ignored here::

    content  = {"action":"location_start","value":"<PGP>","sessionId":"…","version":"1.0"}
    metadata = None, or the same envelope minus "value"

**version 2** -- the envelope moves to the ``metadata`` column (see .metadata)
and the body is *nothing but* the armoured blob::

    content  = -----BEGIN PGP MESSAGE----- …
    metadata = {"action":"location_start","sessionId":"…","version":"2.0"}

:func:`location_envelope` reads either one, so every server-side reader keeps
working through the rollover. Metadata is authoritative only from version 2 on
(:data:`METADATA_ENVELOPE_VERSION`) -- a version 1 tick is always read from its
body, whether or not it also carries a metadata mirror.

:func:`location_push_content` goes the other way: it splices the ciphertext
back into the version 2 envelope to rebuild the single JSON body that push
consumers read -- the Sylk push server and the native notification layers on
iOS and Android. They see the same payload shape before and after the
rollover, so no push-side change is needed. The ``version`` field is passed
through untouched, so a rebuilt payload still says which version its sender
spoke.
"""

import json

__all__ = ('LOCATION_CONTENT_TYPE', 'METADATA_ENVELOPE_VERSION',
           'envelope_version', 'location_metadata', 'location_envelope',
           'location_action', 'location_session_id', 'location_coordinates',
           'location_push_content')


LOCATION_CONTENT_TYPE = 'application/sylk-location-sharing'

# The payload version from which the metadata column carries the envelope and
# the message body is the bare ciphertext. Below it the body is authoritative
# and metadata is at best a mirror, so it is not read.
METADATA_ENVELOPE_VERSION = 2

# The one envelope key that never appears in metadata: the PGP-armoured
# coordinates. Metadata is a cleartext classification aid, the ciphertext stays
# in the message body.
VALUE_KEY = 'value'


def _decode(value):
    """Coerce a stored column / header value to str, or None."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode('utf-8', 'ignore')
    return value if isinstance(value, str) else str(value)


def _json_object(value):
    """Parse a value as a JSON object, or return None.

    Anything that is not a JSON *object* -- a bare PGP blob, an array, a
    number, malformed JSON, empty -- yields None rather than raising.
    """
    text = _decode(value)
    if not text:
        return None
    text = text.strip()
    if not text.startswith('{'):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def envelope_version(envelope):
    """The major payload version an envelope declares, or None.

    Tolerates the shapes a ``version`` field turns up in -- "2", "2.0",
    "2.1.3", 2, 2.0 -- since only the major number decides how the payload is
    laid out.
    """
    version = envelope.get('version') if isinstance(envelope, dict) else None
    if version is None or isinstance(version, bool):
        return None
    if isinstance(version, (int, float)):
        return int(version)
    version = _decode(version)
    if not version:
        return None
    try:
        return int(version.strip().split('.')[0])
    except ValueError:
        return None


def location_metadata(metadata):
    """The metadata envelope, but only when it is the authoritative one.

    Returns the parsed metadata for a version 2 (or later) tick, where the
    envelope lives in metadata and the body holds only the ciphertext.
    Returns None for a version 1 tick -- whose metadata is a mirror of a body
    that is itself readable -- and for metadata that is missing, unparseable,
    or declares no version at all.
    """
    envelope = _json_object(metadata)
    if envelope is None:
        return None
    version = envelope_version(envelope)
    if version is None or version < METADATA_ENVELOPE_VERSION:
        return None
    return envelope


def location_coordinates(content):
    """The PGP-armoured coordinates of a tick, or None when it carries none.

    From version 2 on the message body IS the armoured blob and is returned
    as-is. A version 1 body is a JSON envelope, so its ``value`` is returned
    instead. Lifecycle signals -- location_stop, meeting_accept, ... -- carry
    no coordinates at all and yield None.
    """
    body = _json_object(content)
    if body is not None:
        value = body.get(VALUE_KEY)
        return value if isinstance(value, str) and value.strip() else None
    blob = (_decode(content) or '').strip()
    return blob or None


def location_envelope(content_type, content, metadata=None):
    """The cleartext envelope of a location-sharing tick, as a dict.

    Version 2 and later: read from metadata, with the ciphertext spliced back
    in under ``value`` so callers see one complete envelope regardless of the
    version they were handed. Version 1 and anything without usable metadata:
    read from the message body, exactly as before.

    Returns ``{}`` for a non-location row, or a tick with no readable envelope
    on either side.
    """
    if content_type != LOCATION_CONTENT_TYPE:
        return {}
    envelope = location_metadata(metadata)
    if envelope is None:
        # version 1: the body carries the whole envelope, coordinates included
        return _json_object(content) or {}
    envelope = dict(envelope)
    coordinates = location_coordinates(content)
    if coordinates:
        envelope[VALUE_KEY] = coordinates
    else:
        # metadata never carries coordinates; a stray key must not fake them
        envelope.pop(VALUE_KEY, None)
    return envelope


def location_action(content_type, content, metadata=None):
    """The tick's cleartext ``action``, or None when it carries none."""
    action = location_envelope(content_type, content, metadata).get('action')
    return action if isinstance(action, str) and action else None


def location_session_id(content_type, content, metadata=None):
    """The ``sessionId`` shared by every tick of one share, or None.

    A one-shot share omits it (there is no trail to group), as does any
    non-location row.
    """
    session_id = location_envelope(content_type, content, metadata).get('sessionId')
    return session_id or None


def location_push_content(content, metadata):
    """The ``content`` string to put on a push notification for a location tick.

    Push consumers -- the Sylk push server and the native notification layers
    that build the banner (Android ``MyFirebaseMessagingService``, the iOS
    Notification Service Extension) -- read the lifecycle fields straight out
    of the message body. A version 2 body no longer carries them, so the
    single JSON payload is rebuilt here from the metadata envelope plus the
    armoured blob, and the push looks exactly as it always has.

    A version 1 tick already carries that payload in its body, so it goes out
    untouched, byte for byte -- as does anything whose metadata is missing or
    unusable.

    ``version`` is passed through as the sender set it, so the rebuilt payload
    still names the version its sender spoke.
    """
    envelope = location_metadata(metadata)
    if envelope is None:
        return content
    # metadata never carries coordinates; a stray key must not fake them
    envelope = dict(envelope)
    envelope.pop(VALUE_KEY, None)
    coordinates = location_coordinates(content)
    # Reassemble in the sender's original key order -- the envelope leads with
    # `action`, the coordinates follow it, then the rest of the lifecycle
    # fields. Metadata is the envelope minus `value`, so slotting the
    # ciphertext back in right after `action` reproduces the exact layout a
    # version 1 client used to put in the body.
    payload = {}
    for key, value in envelope.items():
        payload[key] = value
        if key == 'action' and coordinates:
            payload[VALUE_KEY] = coordinates
    if coordinates and VALUE_KEY not in payload:  # envelope without an action
        payload[VALUE_KEY] = coordinates
    # compact and unescaped, matching the client-side JSON.stringify that
    # produced it -- a push payload is size-capped (see push._MAX_PAYLOAD_SIZE)
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
