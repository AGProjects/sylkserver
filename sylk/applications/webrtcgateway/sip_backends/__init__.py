
"""SIP signalling backends for the WebRTC gateway"""

from .base import SIPBackend, SIPAccountBackend, SIPCallBackend

__all__ = 'SIPBackend', 'SIPAccountBackend', 'SIPCallBackend', 'backend_names', 'get_backend_class'


def _janus_backend():
    from .janus_sip import JanusSIPBackend
    return JanusSIPBackend


_backend_factories = {
    'janus': _janus_backend,
}

backend_names = tuple(_backend_factories)


def get_backend_class(name):
    try:
        factory = _backend_factories[name]
    except KeyError:
        raise ValueError('unknown SIP backend: {!r}'.format(name))
    return factory()
