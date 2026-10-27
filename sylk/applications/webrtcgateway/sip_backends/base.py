
"""
Interfaces between the WebRTC gateway connection handler and the SIP
signalling backends.

A backend translates between the sylkrtc call/registration requests coming
from a WebRTC client and a SIP stack. Two objects are involved for every
WebSocket connection:

  - ConnectionHandler (handler.py) owns the sylkrtc side: client requests,
    session/account state, the events sent to the client. Backends report
    what happened on the SIP side by calling its sip_* methods (see
    SIPBackendListener below for the full list).

  - SIPBackend: one instance per connection and backend type. It creates the
    per-account (SIPAccountBackend) and per-call (SIPCallBackend) objects
    the handler talks to.

Backend events must reach the handler serialized with the client requests,
so backends never call the sip_* methods directly from a foreign context:
they go through handler.post_backend_event(), which runs the callable from
the connection's operations queue.
"""

__all__ = 'SIPBackendError', 'SIPBackend', 'SIPAccountBackend', 'SIPCallBackend', 'SIPBackendListener'


class SIPBackendError(Exception):
    """A request cannot be carried out by the backend; the message is returned to the client"""


class SIPBackend(object):
    """Per-connection SIP signalling backend"""

    name = None

    def __init__(self, handler):
        self.handler = handler

    @property
    def log(self):
        return self.handler.log

    def create_account(self, account_info):
        """Return the SIPAccountBackend handling account_info"""
        raise NotImplementedError

    def create_outgoing_session(self, account_info, session_info, uri, sdp, headers):
        """
        Start an outgoing call from account_info to uri using the WebRTC
        offer in sdp. headers is a list of {'name': ..., 'value': ...}
        dictionaries to add to the INVITE. Must set session_info.janus_handle
        and return the SIPCallBackend for the call. Called in a green thread.
        """
        raise NotImplementedError

    def connection_lost(self):
        """The WebSocket connection went away, the Janus session is about to be destroyed"""
        pass


class SIPAccountBackend(object):
    """Registration side of an account"""

    def __init__(self, backend, account_info):
        self.backend = backend
        self.account_info = account_info
        self.proxy = None  # where the last REGISTER was sent

    @property
    def handler(self):
        return self.backend.handler

    @property
    def log(self):
        return self.backend.handler.log

    def lookup_proxy(self):
        """Find where to send the REGISTER. Blocks (green thread)"""
        raise NotImplementedError

    def reset(self):
        """Drop any existing registration resources before a new attempt"""
        raise NotImplementedError

    def register(self, proxy):
        raise NotImplementedError

    def release(self):
        """The account was unregistered or removed by the client"""
        raise NotImplementedError

    def connection_lost(self):
        """The WebSocket connection went away; do not wait for anything"""
        raise NotImplementedError


class SIPCallBackend(object):
    """Signalling side of one call"""

    def __init__(self, backend, session_info):
        self.backend = backend
        self.session_info = session_info

    @property
    def handler(self):
        return self.backend.handler

    @property
    def log(self):
        return self.backend.handler.log

    def answer(self, sdp, headers=None):
        raise NotImplementedError

    def decline(self, code):
        raise NotImplementedError

    def hangup(self):
        raise NotImplementedError

    def update(self, sdp, jsep_type, headers=None):
        raise NotImplementedError

    def trickle(self, candidates):
        raise NotImplementedError

    def send_message(self, content_type, content):
        raise NotImplementedError

    def send_dtmf_info(self, digit, duration=None):
        raise NotImplementedError

    @property
    def chat_active(self):
        """True when the call has an MSRP chat stream that messages can be sent on"""
        return False

    def send_chat_message(self, content, content_type, message_id, timestamp=None, additional_headers=None):
        """
        Send a message on the MSRP chat stream of the call. message_id is the
        client's id, used to report the delivery (sip_session_chat_message_delivery).
        """
        raise SIPBackendError('the session has no chat stream')

    def cleanup(self):
        """The session terminated and was removed from the handler"""
        raise NotImplementedError

    def connection_lost(self):
        """The WebSocket connection went away; do not wait for anything"""
        raise NotImplementedError


class SIPBackendListener(object):
    """
    Documentation only: the methods ConnectionHandler implements for the
    backends. All of them must be called from the operations queue (see
    ConnectionHandler.post_backend_event).
    """

    def post_backend_event(self, function, *args, **kw): pass

    # accounts
    def sip_account_registering(self, account_info): pass
    def sip_account_registered(self, account_info): pass
    def sip_account_registration_failed(self, account_info, reason): pass
    def sip_account_incoming_session(self, account_info, session_info, originator, sdp, call_id, headers=None): pass
    def sip_account_missed_session(self, account_info, originator): pass

    # sessions
    def sip_session_progress(self, session_info): pass
    def sip_session_proceeding(self, session_info, code): pass
    def sip_session_ringing(self, session_info): pass
    def sip_session_early_media(self, session_info, sdp, call_id=None): pass
    def sip_session_accepted(self, session_info, sdp=None, call_id=None, headers=None): pass
    def sip_session_established(self, session_info): pass
    def sip_session_update_received(self, session_info, sdp): pass
    def sip_session_updated(self, session_info, sdp=None): pass
    def sip_session_update_failed(self, session_info, reason, sdp=None): pass
    def sip_session_terminated(self, session_info, reason, code=None): pass
    def sip_session_slow_link(self, session_info, uplink): pass
    def sip_session_media(self, session_info, medium, receiving): pass
    def sip_session_message(self, session_info, content, content_type, sender_uri, sender_display_name=None): pass
    def sip_session_message_delivery(self, session_info, code, reason): pass
    def sip_session_chat_message(self, session_info, message, msrp_message_id): pass  # message: sipsimple.streams.msrp.chat.Message
    def sip_session_chat_message_delivery(self, session_info, message_id, code, reason): pass
