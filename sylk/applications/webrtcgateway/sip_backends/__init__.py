
"""SIP signalling backends for the WebRTC gateway"""

from .base import SIPBackendError, SIPBackend, SIPAccountBackend, SIPCallBackend

__all__ = 'SIPBackendError', 'SIPBackend', 'SIPAccountBackend', 'SIPCallBackend', 'backend_names', 'get_backend_class'


def _janus_backend():
    from .janus_sip import JanusSIPBackend
    return JanusSIPBackend


def _sipsimple_backend():
    from .sipsimple import SIPSimpleBackend
    return SIPSimpleBackend


# Keep in sync with configuration.SIPBackendName and the sip_backend
# property of sylkrtc.AccountAddRequest
_backend_factories = {
    'janus': _janus_backend,
    'sipsimple': _sipsimple_backend,
}

backend_names = tuple(_backend_factories)


def get_backend_class(name):
    try:
        factory = _backend_factories[name]
    except KeyError:
        raise ValueError('unknown SIP backend: {!r}'.format(name))
    return factory()
