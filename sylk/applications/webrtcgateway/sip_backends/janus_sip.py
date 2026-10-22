
"""
SIP backend using the Janus SIP plugin (Sofia-SIP).

Janus owns both the media and the SIP signalling: every registered account
has a SIP plugin handle (plus helper handles for additional calls) and every
outgoing call gets a handle of its own. This module translates between the
plugin requests/events and the ConnectionHandler sip_* interface.
"""

from ..janus import JanusBackend, SIPPluginHandle
from ..models import janus, sylkrtc
from .base import SIPAccountBackend, SIPBackend, SIPCallBackend


__all__ = 'JanusSIPBackend',


class JanusSIPAccount(SIPAccountBackend):
    def __init__(self, backend, account_info):
        super(JanusSIPAccount, self).__init__(backend, account_info)
        self.janus_handle = None  # type: SIPPluginHandle
        self.janus_helpers = []   # type: list[SIPPluginHandle]

    def lookup_proxy(self):
        return self.handler._lookup_sip_proxy(self.account_info.id)

    def reset(self):
        if self.janus_handle is not None:
            self.release()
        # Create a plugin handle
        self.janus_handle = SIPPluginHandle(self.handler.janus_session, event_handler=self.backend.janus_event_handler)
        self.backend.account_handles_map[self.janus_handle.id] = self

    def register(self, proxy):
        self.janus_handle.register(self.account_info, proxy=proxy)

    def release(self):
        if self.janus_handle is not None:
            # Destroy the existing plugin handles
            self.janus_handle.detach()
            self.backend.account_handles_map.pop(self.janus_handle.id)
            self.janus_handle = None
            for helper in self.janus_helpers:
                helper.detach()
                self.backend.account_handles_map.pop(helper.id, None)
            self.janus_helpers = []

    def connection_lost(self):
        # Do not explicitly detach the plugin handles, janus will do it when destroying the session
        if self.janus_handle is not None:
            JanusBackend().set_event_handler(self.janus_handle.id, None)
            for helper in self.janus_helpers:
                self.backend.account_handles_map.pop(helper.id, None)
            self.janus_helpers = []

    def add_helper(self, master_id):
        helper = SIPPluginHandle(self.handler.janus_session, event_handler=self.backend.janus_event_handler)
        helper.register_helper(self.account_info, master_id=master_id)
        self.backend.account_handles_map[helper.id] = self
        self.janus_helpers.append(helper)

    def get_handle(self, handle_id):
        if self.janus_handle is not None and handle_id == self.janus_handle.id:
            return self.janus_handle
        return next((helper for helper in self.janus_helpers if helper.id == handle_id), None)


class JanusSIPCall(SIPCallBackend):
    def __init__(self, backend, session_info, janus_handle, owns_handle):
        super(JanusSIPCall, self).__init__(backend, session_info)
        self.janus_handle = janus_handle
        # Outgoing calls get a plugin handle of their own. Incoming calls
        # arrive on the account's handle (or one of its helpers), which must
        # outlive the call.
        self.owns_handle = owns_handle

    def answer(self, sdp, headers=None):
        self.janus_handle.accept(sdp=sdp, headers=headers)

    def decline(self, code):
        self.janus_handle.decline(code)

    def hangup(self):
        self.janus_handle.hangup()

    def update(self, sdp, jsep_type, headers=None):
        self.janus_handle.update(sdp=sdp, jsep_type=jsep_type, headers=headers)

    def trickle(self, candidates):
        self.janus_handle.trickle(candidates)

    def send_message(self, content_type, content):
        self.janus_handle.send_message(content_type=content_type, content=content)

    def send_dtmf_info(self, digit, duration=None):
        self.janus_handle.send_dtmf_info(digit=digit, duration=duration)

    def cleanup(self):
        if self.owns_handle:
            self.janus_handle.detach()

    def connection_lost(self):
        JanusBackend().set_event_handler(self.janus_handle.id, None)


class JanusSIPBackend(SIPBackend):
    name = 'janus'

    def __init__(self, handler):
        super(JanusSIPBackend, self).__init__(handler)
        self.account_handles_map = {}  # Janus handle ID -> JanusSIPAccount

    def create_account(self, account_info):
        return JanusSIPAccount(self, account_info)

    def create_outgoing_session(self, account_info, session_info, uri, sdp, headers):
        proxy = self.handler._lookup_sip_proxy(uri, sender=account_info.id)
        # Create a new plugin handle and 'register' it, without actually doing so
        janus_handle = SIPPluginHandle(self.handler.janus_session, event_handler=self.janus_event_handler)
        try:
            janus_handle.call(account_info, uri=uri, sdp=sdp, proxy=proxy, headers=headers)
        except Exception:
            janus_handle.detach()
            raise
        session_info.janus_handle = janus_handle
        return JanusSIPCall(self, session_info, janus_handle, owns_handle=True)

    def connection_lost(self):
        self.account_handles_map.clear()

    # Janus events

    def janus_event_handler(self, event):
        # Called by the Janus client protocol; process it from the connection's operations queue
        self.handler.post_backend_event(self._handle_event, event)

    def _handle_event(self, event):
        log = self.log
        if isinstance(event, janus.PluginEvent):
            event_id = event.plugindata.data.__id__
            try:
                handler = getattr(self, '_EH_janus_' + '_'.join(event_id))
            except AttributeError:
                log.warning('unhandled Janus SIP event: {event_name}'.format(event_name=event_id[-1]))
            else:
                log.debug('janus SIP event: {event_name} (handle_id={event.sender})'.format(event=event, event_name=event_id[-1]))
                handler(event)
        else:  # janus.CoreEvent
            try:
                handler = getattr(self, '_EH_janus_sip_' + event.janus)
            except AttributeError:
                log.warning('unhandled Janus SIP event: {event.janus}'.format(event=event))
            else:
                log.debug('janus SIP event: {event.janus} (handle_id={event.sender})'.format(event=event))
                handler(event)

    def _find_session(self, event, event_name=None):
        try:
            return self.handler.sip_sessions[event.sender]
        except KeyError:
            if event_name is not None:
                self.log.warning('could not find SIP session with handle ID {event.sender} for {name} event'.format(event=event, name=event_name))
            return None

    def _find_account(self, event, event_name):
        try:
            return self.account_handles_map[event.sender]
        except KeyError:
            self.log.warning('could not find account with handle ID {event.sender} for {name} event'.format(event=event, name=event_name))
            return None

    # Core events

    def _EH_janus_sip_error(self, event):
        # fixme: implement error handling
        self.log.error('got SIP error event: {}'.format(event.__data__))
        handle_id = event.sender
        if handle_id in self.handler.sip_sessions:
            pass  # this is a session related event
        elif handle_id in self.account_handles_map:
            pass  # this is an account related event

    def _EH_janus_sip_webrtcup(self, event):
        session_info = self._find_session(event, 'webrtcup')
        if session_info is not None:
            self.handler.sip_session_established(session_info)

    def _EH_janus_sip_hangup(self, event):
        session_info = self._find_session(event)
        if session_info is not None:
            self.handler.sip_session_terminated(session_info, reason=event.reason or 'unspecified reason')

    def _EH_janus_sip_slowlink(self, event):
        session_info = self._find_session(event, 'slowlink')
        if session_info is not None:
            self.handler.sip_session_slow_link(session_info, uplink=event.uplink)

    def _EH_janus_sip_media(self, event):
        # Janus 'media' event: it (re)started or stopped receiving RTP from
        # the WebRTC peer (the phone) for one medium.
        session_info = self._find_session(event)
        if session_info is not None:
            medium = getattr(event, 'type', None) or 'audio'
            receiving = bool(getattr(event, 'receiving', False))
            self.handler.sip_session_media(session_info, medium, receiving)

    def _EH_janus_sip_detached(self, event):
        pass

    # Plugin events: registration

    def _EH_janus_sip_event_registering(self, event):
        account = self._find_account(event, 'registering')
        if account is not None:
            self.handler.sip_account_registering(account.account_info)

    def _EH_janus_sip_event_registered(self, event):
        if event.sender in self.handler.sip_sessions:  # skip 'registered' events from outgoing session handles
            return
        account = self._find_account(event, 'registered')
        if account is None:
            return
        if account.account_info.registration_state != 'registered':
            account.add_helper(master_id=event.plugindata.data.result.master_id)
            self.handler.sip_account_registered(account.account_info)

    def _EH_janus_sip_event_registration_failed(self, event):
        account = self._find_account(event, 'registration failed')
        if account is not None:
            reason = '{result.code} {result.reason}'.format(result=event.plugindata.data.result)
            self.handler.sip_account_registration_failed(account.account_info, reason)

    # Plugin events: calls

    def _EH_janus_sip_event_incomingcall(self, event):
        # Janus re-emits 'incomingcall' for in-dialog re-INVITEs in
        # some builds (instead of using a separate 'updatingcall'
        # event). Detect that case by checking whether event.sender
        # corresponds to an already-known session handle — if so,
        # this is a mid-call renegotiation and we route through the
        # same logic as the explicit 'updatingcall' event handler.
        # Without this guard, an in-dialog re-INVITE would land here,
        # fall through the account_handles_map lookup with a warning,
        # and the client (callee) would never get a SessionUpdateEvent
        # — symptom on the wire: A's session-update gets accepted, B's
        # PeerConnection is never told to expect new media, A's video
        # frames arrive but B has no receiver wired to render them.
        if event.sender in self.handler.sip_sessions:
            self.log.info('incomingcall on existing session — routing as re-INVITE')
            return self._EH_janus_sip_event_updatingcall(event)
        account = self._find_account(event, 'incoming call')
        if account is None:
            return
        assert event.jsep is not None
        data = event.plugindata.data.result  # type: janus.SIPResultIncomingCall
        call_id = event.plugindata.data.call_id
        originator = sylkrtc.SIPIdentity(uri=data.username, display_name=data.displayname)

        handle = account.get_handle(event.sender)
        if handle is None:
            self.log.warning('could not find handle ID {event.sender} for incoming call event'.format(event=event))
            return

        from ..handler import SIPSessionInfo
        session_info = SIPSessionInfo(self.handler._callid_to_uuid(call_id))
        session_info.janus_handle = handle
        session_info.backend = JanusSIPCall(self, session_info, handle, owns_handle=False)
        session_info.init_incoming(account.account_info, originator.uri, originator.display_name)
        session_info.call_id = call_id
        session_info.update_media(event.jsep.sdp, remote=True)
        self.handler.sip_account_incoming_session(account.account_info, session_info, originator, sdp=event.jsep.sdp, call_id=call_id, headers=data.headers or None)

    def _EH_janus_sip_event_missed_call(self, event):
        account = self._find_account(event, 'missed call')
        if account is not None:
            data = event.plugindata.data.result  # type: janus.SIPResultMissedCall
            originator = sylkrtc.SIPIdentity(uri=data.caller, display_name=data.displayname)
            self.handler.sip_account_missed_session(account.account_info, originator)

    def _EH_janus_sip_event_calling(self, event):
        session_info = self._find_session(event, 'calling')
        if session_info is not None:
            self.handler.sip_session_calling(session_info)

    def _EH_janus_sip_event_accepted(self, event):
        session_info = self._find_session(event, 'accepted')
        if session_info is None:
            return
        call_id = event.plugindata.data.call_id
        if call_id:
            session_info.call_id = call_id
        data = event.plugindata.data.result  # type: janus.SIPResultAccepted
        sdp = event.jsep.sdp if event.jsep is not None else None
        self.handler.sip_session_accepted(session_info, sdp=sdp, call_id=call_id, headers=getattr(data, 'headers', None) or None)

    def _EH_janus_sip_event_updating(self, event):
        session_info = self._find_session(event)
        if session_info is not None:
            self.log.debug('{session.direction} session {session.id} update in progress'.format(session=session_info))

    def _EH_janus_sip_event_updatingcall(self, event):
        session_info = self._find_session(event, 'updatingcall')
        if session_info is None:
            return
        if event.jsep is None:
            self.log.warning('updatingcall event without JSEP for session {session.id}'.format(session=session_info))
            return
        self.handler.sip_session_update_received(session_info, event.jsep.sdp)

    def _EH_janus_sip_event_updated(self, event):
        session_info = self._find_session(event, 'updated')
        if session_info is not None:
            self.handler.sip_session_updated(session_info, sdp=event.jsep.sdp if event.jsep is not None else None)

    def _EH_janus_sip_event_hangup(self, event):
        session_info = self._find_session(event, 'hangup')
        if session_info is not None:
            data = event.plugindata.data.result  # type: janus.SIPResultHangup
            self.handler.sip_session_terminated(session_info, reason='{0.code} {0.reason}'.format(data), code=data.code)

    def _EH_janus_sip_event_declining(self, event):
        pass

    def _EH_janus_sip_event_hangingup(self, event):
        pass

    def _EH_janus_sip_event_proceeding(self, event):
        session_info = self._find_session(event)
        if session_info is not None:
            data = event.plugindata.data.result  # type: janus.SIPResultProceeding
            self.handler.sip_session_proceeding(session_info, data.code)

    def _EH_janus_sip_event_progress(self, event):
        session_info = self._find_session(event, 'progress')
        if session_info is None:
            return
        # The 183 progress is the earliest event that carries the SIP
        # Call-ID for an outgoing call. Remember it so a later cancel /
        # terminate can report it to the caller (SessionTerminatedEvent).
        call_id = event.plugindata.data.call_id
        if call_id:
            session_info.call_id = call_id
        if event.jsep:
            self.handler.sip_session_early_media(session_info, event.jsep.sdp, call_id=call_id)

    def _EH_janus_sip_event_ringing(self, event):
        session_info = self._find_session(event)
        if session_info is not None:
            self.handler.sip_session_ringing(session_info)

    def _EH_janus_sip_event_message(self, event):
        data = event.plugindata.data.result  # type: janus.SIPResultMessage
        session_info = self._find_session(event)
        if session_info is None:
            return
        if not event.plugindata.data.call_id:
            return
        if not data.content:
            return
        self.handler.sip_session_message(session_info, data.content, data.content_type, data.sender, data.displayname)

    def _EH_janus_sip_event_messagesent(self, event):
        pass

    def _EH_janus_sip_event_messagedelivery(self, event):
        session_info = self._find_session(event, 'delivery')
        if session_info is not None:
            data = event.plugindata.data.result
            self.handler.sip_session_message_delivery(session_info, data.code, data.reason)

    def _EH_janus_sip_event_dtmfsent(self, event):
        pass
