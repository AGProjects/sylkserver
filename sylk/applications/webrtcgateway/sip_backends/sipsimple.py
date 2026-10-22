
"""
SIP backend using SylkServer's own SIP stack (SIP SIMPLE SDK).

SylkServer does the SIP signalling (REGISTER, INVITE through
sylk.session.Session) and Janus is used only for the media, through the
NoSIP plugin, which converts between the WebRTC PeerConnection and plain
RTP/SRTP.

Not implemented yet: accounts using this backend fail to register and calls
are refused, so that it can already be selected without side effects.
"""

from .base import SIPAccountBackend, SIPBackend, SIPBackendError


__all__ = 'SIPSimpleBackend',


_not_implemented = 'the sipsimple SIP backend is not implemented yet'


class SIPSimpleAccount(SIPAccountBackend):
    def lookup_proxy(self):
        return None

    def reset(self):
        pass

    def register(self, proxy):
        self.handler.post_backend_event(self.handler.sip_account_registration_failed, self.account_info, '501 ' + _not_implemented)

    def release(self):
        pass

    def connection_lost(self):
        pass


class SIPSimpleBackend(SIPBackend):
    name = 'sipsimple'

    def create_account(self, account_info):
        return SIPSimpleAccount(self, account_info)

    def create_outgoing_session(self, account_info, session_info, uri, sdp, headers):
        raise SIPBackendError(_not_implemented)
