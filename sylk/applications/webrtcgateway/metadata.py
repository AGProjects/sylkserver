
"""Per-message application metadata.

A message can carry an opaque, application-defined blob alongside its content.
The server never interprets it -- by convention it is a JSON string. It is:

  * stored in the message journal (the Cassandra ``metadata`` text column, and
    the equivalent key in the file storage backend),
  * returned with every message over the sylkrtc API (``account-message`` /
    ``session-message`` events, ``syncConversations`` and the history HTTP API),
  * relayed to the remote party inside the CPIM envelope, so it survives the
    hop between two SylkServers.

Its first user is ``application/sylk-location-sharing``: the cleartext
lifecycle envelope of a location tick (action, sessionId, expires, ...) is
shipped as metadata while the coordinates stay PGP-encrypted inside the
content. That lets the journal, the push layer and the receiving clients
classify, group and filter a location share without ever decrypting it.

The CPIM header is a single line of UTF-8 (the CPIM grammar allows any
character but CR/LF in a header value), so the only sanitising needed is
folding line breaks away and capping the size.
"""

import re

__all__ = ('METADATA_HEADER_NAME', 'METADATA_NAMESPACE', 'METADATA_MAX_SIZE',
           'sanitize_metadata', 'metadata_cpim_header', 'metadata_from_cpim_headers')


# A run of line breaks collapses to a single space, matching the client-side
# serializer in react-native-sylkrtc (utils.serializeMessageMetadata) so both
# ends normalize a value identically.
_line_break_re = re.compile(r'[\r\n]+')


METADATA_NAMESPACE = 'urn:ag-projects:xml:ns:cpim'
METADATA_NAMESPACE_PREFIX = 'agp'
METADATA_HEADER_NAME = 'Metadata'

# Metadata rides in the SIP MESSAGE body (inside CPIM), not in a SIP header,
# but it is still meant to stay small -- it is a classification aid, not a
# second payload. Anything larger is dropped rather than truncated, since a
# truncated JSON string is worse than none at all.
METADATA_MAX_SIZE = 4096


def sanitize_metadata(metadata):
    """Normalize a metadata value for storage/transport, or return None.

    Accepts str/bytes/None. CR and LF are folded to spaces (they would break
    the single-line CPIM header grammar) and oversized values are rejected.
    """
    if metadata is None:
        return None
    if isinstance(metadata, bytes):
        try:
            metadata = metadata.decode('utf-8')
        except UnicodeDecodeError:
            return None
    elif not isinstance(metadata, str):
        return None
    metadata = _line_break_re.sub(' ', metadata).strip()
    if not metadata or len(metadata) > METADATA_MAX_SIZE:
        return None
    return metadata


def metadata_cpim_header(metadata):
    """Build the CPIM header carrying metadata to the peer, or return None."""
    metadata = sanitize_metadata(metadata)
    if metadata is None:
        return None
    from sipsimple.streams.msrp.chat import CPIMHeader, CPIMNamespace
    namespace = CPIMNamespace(METADATA_NAMESPACE, prefix=METADATA_NAMESPACE_PREFIX)
    return CPIMHeader(METADATA_HEADER_NAME, namespace, metadata)


def metadata_from_cpim_headers(headers):
    """Extract the metadata value from a CPIM message's additional headers."""
    if not headers:
        return None
    return next((header.value for header in headers
                 if header.name == METADATA_HEADER_NAME and header.namespace == METADATA_NAMESPACE), None)
