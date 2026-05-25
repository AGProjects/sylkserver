
from application.python import subclasses
from sipsimple.util import ISOTimestamp

from .jsonobjects import (AbstractObjectProperty, AbstractProperty,
                          ArrayProperty, BooleanProperty, CompositeValidator,
                          FixedValueProperty, IntegerProperty, JSONArray,
                          JSONObject, LimitedChoiceProperty, ObjectProperty,
                          StringArray, StringProperty)
from .validators import (AORValidator, DisplayNameValidator, LengthValidator,
                         UniqueItemsValidator)
from .xcap import AddressBook, XCAPMapper

# Base models (these are abstract and should not be used directly)

class SylkRTCRequestBase(JSONObject):
    transaction = StringProperty()


class SylkRTCResponseBase(JSONObject):
    transaction = StringProperty()


class AccountRequestBase(SylkRTCRequestBase):
    account = StringProperty(validator=AORValidator())


class SessionRequestBase(SylkRTCRequestBase):
    session = StringProperty()


class VideoroomRequestBase(SylkRTCRequestBase):
    session = StringProperty()


class AccountEventBase(JSONObject):
    sylkrtc = FixedValueProperty('account-event')
    account = StringProperty(validator=AORValidator())


class SessionEventBase(JSONObject):
    sylkrtc = FixedValueProperty('session-event')
    session = StringProperty()


class VideoroomEventBase(JSONObject):
    sylkrtc = FixedValueProperty('videoroom-event')
    session = StringProperty()


class AccountRegistrationStateEvent(AccountEventBase):
    event = FixedValueProperty('registration-state')


class SessionStateEvent(SessionEventBase):
    event = FixedValueProperty('state')


class VideoroomSessionStateEvent(VideoroomEventBase):
    event = FixedValueProperty('session-state')


# Miscellaneous models

class Header(JSONObject):
    name = StringProperty()
    value = StringProperty()


class Headers(JSONArray):
    item_type = Header


class SIPIdentity(JSONObject):
    uri = StringProperty(validator=AORValidator())
    display_name = StringProperty(optional=True, validator=DisplayNameValidator())


class ICECandidate(JSONObject):
    candidate = StringProperty()
    sdpMLineIndex = IntegerProperty()
    sdpMid = StringProperty()


class ICECandidates(JSONArray):
    item_type = ICECandidate


class AORList(StringArray):
    list_validator = UniqueItemsValidator()
    item_validator = AORValidator()


class VideoroomPublisher(JSONObject):
    id = StringProperty()
    uri = StringProperty(validator=AORValidator())
    display_name = StringProperty(optional=True)


class VideoroomPublishers(JSONArray):
    item_type = VideoroomPublisher


class VideoroomActiveParticipants(StringArray):
    list_validator = CompositeValidator(UniqueItemsValidator(), LengthValidator(maximum=2))


class VideoroomSessionOptions(JSONObject):
    audio = BooleanProperty(optional=True)
    video = BooleanProperty(optional=True)
    bitrate = IntegerProperty(optional=True)


class VideoroomRaisedHands(StringArray):
    list_validator = UniqueItemsValidator()


class SharedFile(JSONObject):
    filename = StringProperty()
    filesize = IntegerProperty()
    uploader = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    session = StringProperty()


class SharedFiles(JSONArray):
    item_type = SharedFile


class TransferredFile(JSONObject):
    filename = StringProperty()
    filesize = IntegerProperty()
    sender = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    receiver = ObjectProperty(SIPIdentity)
    transfer_id = StringProperty()
    prefix = StringProperty()
    path = StringProperty()
    timestamp = StringProperty()
    until = StringProperty(optional=True)
    url = StringProperty(optional=True)
    filetype = StringProperty(optional=True)
    hash = StringProperty(optional=True)


class FileTransferMessage(JSONObject):
    filename = StringProperty()
    filesize = IntegerProperty()
    sender = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    receiver = ObjectProperty(SIPIdentity)
    transfer_id = StringProperty()
    timestamp = StringProperty()
    until = StringProperty(optional=True)
    url = StringProperty(optional=True)
    filetype = StringProperty(optional=True)
    hash = StringProperty(optional=True)


class DispositionNotifications(StringArray):
    list_validator = UniqueItemsValidator()


class Message(JSONObject):
    contact = StringProperty(validator=AORValidator())
    timestamp = StringProperty()
    disposition = ArrayProperty(DispositionNotifications, optional=True)
    message_id = StringProperty()
    content_type = StringProperty()
    content = StringProperty()
    direction = StringProperty(optional=True)
    state = LimitedChoiceProperty(['delivered', 'failed', 'displayed', 'forbidden', 'error', 'accepted', 'pending', 'received'], optional=True)

    def __init__(self, **kw):
        if 'msg_timestamp' in kw:
            kw['timestamp'] = str(ISOTimestamp(kw['msg_timestamp']))
            del kw['msg_timestamp']
        super(Message, self).__init__(**kw)


class ContactMessages(JSONArray):
    item_type = Message


class MessageHistoryData(JSONObject):
    account = StringProperty(validator=AORValidator())
    messages = ArrayProperty(ContactMessages)


class AccountMessageRemoveEventData(JSONObject):
    contact = StringProperty()
    message_id = StringProperty()
    direction = StringProperty(optional=True, default="outgoing")


class AccountMarkConversationReadEventData(JSONObject):
    contact = StringProperty()


class AccountConversationRemoveEventData(JSONObject):
    contact = StringProperty()
    timestamp = StringProperty()


class AccountDispositionNotificationEventData(JSONObject):
    message_id = StringProperty()
    state = LimitedChoiceProperty(['accepted', 'delivered', 'displayed', 'failed', 'processed', 'stored', 'forbidden', 'error'])
    message_timstamp = StringProperty()
    code = IntegerProperty()
    reason = StringProperty()


class IncomingHeaderPrefixes(StringArray):
    list_validator = UniqueItemsValidator()


# Response models

class AckResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueProperty('ack')


class ErrorResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueProperty('error')
    error = StringProperty()


# Connection events

class ReadyEvent(JSONObject):
    sylkrtc = FixedValueProperty('ready-event')


class LookupPublicKeyEvent(JSONObject):
    sylkrtc = FixedValueProperty('lookup-public-key-event')
    uri = StringProperty(validator=AORValidator())
    public_key = StringProperty(optional=True)


# Account events

class AccountIncomingSessionEvent(AccountEventBase):
    event = FixedValueProperty('incoming-session')
    session = StringProperty()
    originator = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    sdp = StringProperty()
    call_id = StringProperty()
    headers = AbstractProperty(optional=True)


class AccountMissedSessionEvent(AccountEventBase):
    event = FixedValueProperty('missed-session')
    originator = ObjectProperty(SIPIdentity)  # type: SIPIdentity


class AccountConferenceInviteEvent(AccountEventBase):
    event = FixedValueProperty('conference-invite')
    room = StringProperty(validator=AORValidator())
    session_id = StringProperty()
    originator = ObjectProperty(SIPIdentity)  # type: SIPIdentity


class AccountMessageEvent(AccountEventBase):
    event = FixedValueProperty('message')
    sender = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    timestamp = StringProperty()
    disposition_notification = ArrayProperty(DispositionNotifications, optional=True)
    message_id = StringProperty()
    content_type = StringProperty()
    content = StringProperty()
    direction = StringProperty(optional=True)


class AccountDispositionNotificationEvent(AccountEventBase):
    event = FixedValueProperty('disposition-notification')
    message_id = StringProperty()
    message_timestamp = StringProperty()
    state = LimitedChoiceProperty(['accepted', 'delivered', 'displayed', 'failed', 'processed', 'stored', 'forbidden', 'error'])
    code = IntegerProperty()
    reason = StringProperty()


class AccountSyncConversationsEvent(AccountEventBase):
    event = FixedValueProperty('sync-conversations')
    messages = ArrayProperty(ContactMessages)


class AccountSyncEvent(AccountEventBase):
    event = FixedValueProperty('sync')
    type = StringProperty()
    action = StringProperty()
    content = AbstractObjectProperty()


class AccountRegisteringEvent(AccountRegistrationStateEvent):
    state = FixedValueProperty('registering')


class AccountRegisteredEvent(AccountRegistrationStateEvent):
    state = FixedValueProperty('registered')


class AccountRegistrationFailedEvent(AccountRegistrationStateEvent):
    state = FixedValueProperty('failed')
    reason = StringProperty(optional=True)


class AccountAddressBookFetchedEvent(AccountEventBase):
    event = FixedValueProperty('addressbook-fetched')
    addressbook = ObjectProperty(AddressBook)


class AccountAddressBookUpdatedEvent(AccountEventBase):
    event = FixedValueProperty('addressbook-updated')
    type = LimitedChoiceProperty(["contact", "group", "policy"])
    action = StringProperty()
    contact = AbstractObjectProperty(optional=True)
    group = AbstractObjectProperty(optional=True)
    policy = AbstractObjectProperty(optional=True)

    def __init__(self, **kwargs):
        if kwargs['type'] in ('contact', 'group', 'policy') and kwargs['data'] is not None:
            kwargs[kwargs['type']] = kwargs['data']
        del kwargs['data']
        super().__init__(**kwargs)


class AccountAddressBookUpdateFailedEvent(AccountEventBase):
    event = FixedValueProperty('addressbook-update-failed')
    type = LimitedChoiceProperty(["contact", "group", "policy"])
    action = StringProperty()
    error = StringProperty()
    id = StringProperty()


# Session events

class SessionProgressEvent(SessionStateEvent):
    state = FixedValueProperty('progress')


class ProceedingEvent(SessionStateEvent):
    state = FixedValueProperty('proceeding')
    code = IntegerProperty()


class RingingEvent(SessionStateEvent):
    state = FixedValueProperty('ringing')


class SessionEarlyMediaEvent(SessionStateEvent):
    state = FixedValueProperty('early-media')
    sdp = StringProperty(optional=True)
    call_id = StringProperty(optional=True)


class SessionAcceptedEvent(SessionStateEvent):
    state = FixedValueProperty('accepted')
    sdp = StringProperty(optional=True)  # missing for incoming sessions
    call_id = StringProperty(optional=True)
    headers = AbstractProperty(optional=True)


class SessionEstablishedEvent(SessionStateEvent):
    state = FixedValueProperty('established')


class SessionTerminatedEvent(SessionStateEvent):
    state = FixedValueProperty('terminated')
    reason = StringProperty(optional=True)


class SessionUpdateEvent(SessionEventBase):
    event = FixedValueProperty('update')
    state = LimitedChoiceProperty(['received', 'accepted', 'failed'])
    sdp = StringProperty(optional=True)
    reason = StringProperty(optional=True)


class SessionMessageEvent(SessionEventBase):
    event = FixedValueProperty('message')
    sender = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    timestamp = StringProperty()
    disposition_notification = ArrayProperty(DispositionNotifications, optional=True)
    message_id = StringProperty()
    content_type = StringProperty()
    content = StringProperty()
    direction = StringProperty(optional=True)


class SessionMessageDispositionNotificationEvent(SessionEventBase):
    event = FixedValueProperty('disposition-notification')
    message_id = StringProperty()
    message_timestamp = StringProperty()
    state = LimitedChoiceProperty(['accepted', 'delivered', 'displayed', 'failed', 'processed', 'stored', 'forbidden', 'error'])
    code = IntegerProperty()
    reason = StringProperty()


# Video room events

class VideoroomConfigureEvent(VideoroomEventBase):
    event = FixedValueProperty('configure')
    originator = StringProperty()
    active_participants = ArrayProperty(VideoroomActiveParticipants)  # type: VideoroomActiveParticipants


class VideoroomSessionProgressEvent(VideoroomSessionStateEvent):
    state = FixedValueProperty('progress')


class VideoroomSessionAcceptedEvent(VideoroomSessionStateEvent):
    state = FixedValueProperty('accepted')
    sdp = StringProperty()
    video = BooleanProperty(optional=True, default=True)
    audio = BooleanProperty(optional=True, default=True)
    # Seconds since the videoroom was created on the webrtcgateway.
    # Late joiners read this once and add their own local elapsed time
    # to maintain a running counter. Computed locally by the gateway
    # so the number is independent of any clock on the SIP focus side.
    duration = IntegerProperty(optional=True)


class VideoroomSessionEstablishedEvent(VideoroomSessionStateEvent):
    state = FixedValueProperty('established')


class VideoroomSessionTerminatedEvent(VideoroomSessionStateEvent):
    state = FixedValueProperty('terminated')
    reason = StringProperty(optional=True)


class VideoroomFeedAttachedEvent(VideoroomEventBase):
    event = FixedValueProperty('feed-attached')
    feed = StringProperty()
    sdp = StringProperty()


class VideoroomFeedEstablishedEvent(VideoroomEventBase):
    event = FixedValueProperty('feed-established')
    feed = StringProperty()


class VideoroomInitialPublishersEvent(VideoroomEventBase):
    event = FixedValueProperty('initial-publishers')
    publishers = ArrayProperty(VideoroomPublishers)  # type: VideoroomPublishers


class VideoroomPublishersJoinedEvent(VideoroomEventBase):
    event = FixedValueProperty('publishers-joined')
    publishers = ArrayProperty(VideoroomPublishers)  # type: VideoroomPublishers


class VideoroomPublishersLeftEvent(VideoroomEventBase):
    event = FixedValueProperty('publishers-left')
    publishers = ArrayProperty(StringArray)          # type: StringArray


class VideoroomFileSharingEvent(VideoroomEventBase):
    event = FixedValueProperty('file-sharing')
    files = ArrayProperty(SharedFiles)               # type: SharedFiles


class VideoroomMessageEvent(VideoroomEventBase):
    event = FixedValueProperty('message')
    type = LimitedChoiceProperty(['normal', 'status'])
    content = StringProperty()
    content_type = StringProperty()
    sender = ObjectProperty(SIPIdentity)  # type: SIPIdentity
    timestamp = StringProperty()


class VideoroomComposingIndicationEvent(VideoroomEventBase):
    event = FixedValueProperty('composing-indication')
    state = StringProperty()
    refresh = IntegerProperty()
    content_type = StringProperty()
    sender = ObjectProperty(SIPIdentity)  # type: SIPIdentity


class VideoroomMessageDeliveryEvent(VideoroomEventBase):
    event = FixedValueProperty('message-delivery')
    message_id = StringProperty()
    delivered = BooleanProperty()
    code = IntegerProperty()
    reason = StringProperty()


class VideoroomMuteAudioEvent(VideoroomEventBase):
    event = FixedValueProperty('mute-audio')
    originator = StringProperty()


class VideoroomMuteRequestEvent(VideoroomEventBase):
    # Per-participant moderator-driven mute request, sent by the
    # webrtcgateway to a single WebRTC publisher session when another
    # client invokes the `videoroom-mute-participant` RPC targeting
    # *this* participant. The recipient is expected to flip the mute
    # state of its local microphone capture to `muted` and update its
    # UI accordingly. SIP-side participants reachable only via the
    # audio bridge are NOT notified through this event — for those the
    # gateway proxies the request to the conference focus's admin HTTP
    # API instead, so the mix-side mute is authoritative there.
    event = FixedValueProperty('mute-request')
    muted = BooleanProperty()
    # The WS session that asked for the mute, so the recipient can
    # render a "Muted by <name>" hint or filter out self-initiated
    # mutes that were already applied locally.
    originator = StringProperty(optional=True)


class VideoroomRaisedHandsEvent(VideoroomEventBase):
    event = FixedValueProperty('raised-hands')
    raised_hands = ArrayProperty(VideoroomRaisedHands)


class VideoroomConferenceMedia(JSONObject):
    type = StringProperty(optional=True)
    status = StringProperty(optional=True)


class VideoroomConferenceMediaList(JSONArray):
    item_type = VideoroomConferenceMedia


class VideoroomConferenceEndpoint(JSONObject):
    uri = StringProperty(optional=True)
    display_name = StringProperty(optional=True)
    status = StringProperty(optional=True)
    media = ArrayProperty(VideoroomConferenceMediaList, optional=True)
    # Sylk-specific extensions surfaced from the conference-info NOTIFY:
    # the stable participant_id token published per endpoint, and the
    # server-side input mute flag set on every endpoint except the bridge.
    participant_id = StringProperty(optional=True)
    muted = BooleanProperty(optional=True)


class VideoroomConferenceEndpoints(JSONArray):
    item_type = VideoroomConferenceEndpoint


class VideoroomConferenceParticipant(JSONObject):
    type = StringProperty(optional=True)
    uri = StringProperty()
    display_name = StringProperty(optional=True)
    endpoints = ArrayProperty(VideoroomConferenceEndpoints, optional=True)
    # Bridge-only fields. When the participant is the sylk-janus-audio-bridge
    # the conference focus advertises the per-room admin API endpoint URL
    # and the matching auth token; relayed verbatim so the WebRTC client
    # can drive the same admin API to mute participants and watch audio
    # levels. Both empty/absent for non-bridge participants.
    admin_endpoint_url = StringProperty(optional=True)
    admin_endpoint_token = StringProperty(optional=True)
    # 'host:port' of the conference focus's audio-level UDP server. The
    # webrtcgateway subscribes to it and forwards level updates over
    # this same WebSocket. Clients receive the WS event regardless of
    # whether they care about this field; surfaced here for transparency
    # and so an out-of-band tool could subscribe directly if desired.
    audio_levels_udp_endpoint = StringProperty(optional=True)


class VideoroomConferenceParticipants(JSONArray):
    item_type = VideoroomConferenceParticipant


class VideoroomConferenceParticipantsEvent(VideoroomEventBase):
    event = FixedValueProperty('conference-participants')
    participants = ArrayProperty(VideoroomConferenceParticipants)
    # Seconds elapsed since the conference room was created on the
    # focus, computed server-side at event generation. Late joiners
    # read it once and add their own local elapsed time to maintain a
    # running counter — no timezone, no clock-skew correction needed.
    duration = IntegerProperty(optional=True)


class VideoroomConferenceAudioLevel(JSONObject):
    # Stable per-session token from the conference focus. Matches the
    # `participant_id` field already published per endpoint in
    # VideoroomConferenceParticipantsEvent, so JS tiles can join the
    # two streams cleanly.
    participant_id = StringProperty()
    # PJMedia signal levels, 0–255 (µ-law-companded). `tx` / `rx` are
    # the per-window mean; `tx_peak` / `rx_peak` are the per-window
    # max — use the peak for VU-meter style display since speech is
    # bursty and the mean undersells perceived loudness. `rx` is the
    # level INTO the conference bridge from this participant (speech
    # going INTO the mix); `tx` is the level FROM the bridge to this
    # participant (what the participant is hearing).
    tx = IntegerProperty(optional=True)
    rx = IntegerProperty(optional=True)
    tx_peak = IntegerProperty(optional=True)
    rx_peak = IntegerProperty(optional=True)


class VideoroomConferenceAudioLevels(JSONArray):
    item_type = VideoroomConferenceAudioLevel


class VideoroomConferenceAudioLevelsEvent(VideoroomEventBase):
    event = FixedValueProperty('conference-audio-levels')
    levels = ArrayProperty(VideoroomConferenceAudioLevels)
    # Wall-clock timestamp (UTC ms since epoch) at which the server
    # rolled up this window. Useful for clients that want to detect
    # stale data after a network glitch.
    ts = IntegerProperty(optional=True)


class VideoroomInviteStatusEvent(VideoroomEventBase):
    event = FixedValueProperty('invite-status')
    participant = StringProperty()
    state = StringProperty()
    code = IntegerProperty(optional=True)
    reason = StringProperty(optional=True)


# Ping request model, can be used to check connectivity from client

class PingRequest(SylkRTCRequestBase):
    sylkrtc = FixedValueProperty('ping')


# Lookup Public key model

class LookupPublicKeyRequest(SylkRTCRequestBase):
    sylkrtc = FixedValueProperty('lookup-public-key')
    uri = StringProperty(validator=AORValidator())


# Account request models

class AccountAddRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-add')
    password = StringProperty(validator=LengthValidator(minimum=1, maximum=9999))
    display_name = StringProperty(optional=True)
    user_agent = StringProperty(optional=True)
    incoming_header_prefixes = ArrayProperty(IncomingHeaderPrefixes, optional=True)


class AccountRemoveRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-remove')


class AccountRegisterRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-register')


class AccountUnregisterRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-unregister')


class AccountDeviceTokenRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-devicetoken')
    token = StringProperty()
    platform = StringProperty()
    device = StringProperty()
    silent = BooleanProperty(default=False)
    app = StringProperty()


class AccountMessageRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-message')
    uri = StringProperty(validator=AORValidator())
    message_id = StringProperty()
    content = StringProperty()
    content_type = StringProperty()
    timestamp = StringProperty()
    server_generated = BooleanProperty(optional=True)


class AccountDispositionNotificationRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-disposition-notification')
    uri = StringProperty(validator=AORValidator())
    message_id = StringProperty()
    state = LimitedChoiceProperty(['delivered', 'failed', 'displayed', 'forbidden', 'error'])
    timestamp = StringProperty()


class AccountSyncConversationsRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-sync-conversations')
    message_id = StringProperty(optional=True)
    since = StringProperty(optional=True)
    limit = IntegerProperty(optional=True, default=5000)


class AccountMarkConversationReadRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-mark-conversation-read')
    contact = StringProperty(validator=AORValidator())


class AccountMessageRemoveRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-remove-message')
    message_id = StringProperty()
    contact = StringProperty(validator=AORValidator())


class AccountConversationRemoveRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-remove-conversation')
    contact = StringProperty(validator=AORValidator())


class AccountFetchAddressbookRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-fetch-addressbook')


class AccountUpdateAddressBookRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-update-addressbook')
    action = LimitedChoiceProperty(["add", "update", "delete"])
    type = LimitedChoiceProperty(["contact", "group", "policy"])
    data = AbstractObjectProperty()

    def __init__(self, **kwargs):
        kwargs["data"] = XCAPMapper.from_payload(kwargs["data"], kwargs["type"])
        super().__init__(**kwargs)

# Session request models

class SessionCreateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-create')
    account = StringProperty(validator=AORValidator())
    uri = StringProperty(validator=AORValidator())
    sdp = StringProperty()
    headers = ArrayProperty(Headers, optional=True)


class SessionAnswerRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-answer')
    sdp = StringProperty()
    headers = ArrayProperty(Headers, optional=True)


class SessionTrickleRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-trickle')
    candidates = ArrayProperty(ICECandidates)  # type: ICECandidates


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-terminate')


class SessionUpdateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-update')
    sdp = StringProperty()
    headers = ArrayProperty(Headers, optional=True)


class SessionMessageRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-message')
    message_id = StringProperty()
    content = StringProperty()
    content_type = StringProperty()
    timestamp = StringProperty()


class SessionDtmfInfoRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-dtmf-info')
    digit = StringProperty()
    duration = IntegerProperty(optional=True)


# Videoroom request models

class VideoroomJoinRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-join')
    account = StringProperty(validator=AORValidator())
    uri = StringProperty(validator=AORValidator())
    sdp = StringProperty()
    audio = BooleanProperty(optional=True, default=True)
    video = BooleanProperty(optional=True, default=True)


class VideoroomLeaveRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-leave')


class VideoroomConfigureRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-configure')
    active_participants = ArrayProperty(VideoroomActiveParticipants)  # type: VideoroomActiveParticipants


class VideoroomFeedAttachRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-feed-attach')
    publisher = StringProperty()
    feed = StringProperty()


class VideoroomFeedAnswerRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-feed-answer')
    feed = StringProperty()
    sdp = StringProperty()


class VideoroomFeedDetachRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-feed-detach')
    feed = StringProperty()


class VideoroomInviteRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-invite')
    participants = ArrayProperty(AORList)              # type: AORList


class VideoroomRemoveRequest(VideoroomRequestBase):
    # Request the conference focus to remove the named participants
    # from the room via SIP REFER ;method=BYE (RFC 4579). The gateway
    # spawns one SipFocusReferralHandler(method='BYE') per URI; the
    # focus BYEs the participant's existing call leg in the room. Used
    # both for explicit client-driven kicks and for the server-side
    # cleanup when the last WebRTC publisher leaves the room.
    sylkrtc = FixedValueProperty('videoroom-remove')
    participants = ArrayProperty(AORList)              # type: AORList


class VideoroomSessionTrickleRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-session-trickle')
    candidates = ArrayProperty(ICECandidates)          # type: ICECandidates


class VideoroomSessionUpdateRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-session-update')
    options = ObjectProperty(VideoroomSessionOptions)  # type: VideoroomSessionOptions


class VideoroomMessageRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-message')
    message_id = StringProperty()
    content = StringProperty()
    content_type = StringProperty()


class VideoroomMuteParticipantRequest(VideoroomRequestBase):
    # Asks the webrtcgateway to mute/unmute a participant in the SIP
    # conference. The gateway forwards the request to the conference
    # focus's admin HTTP API (POST /rooms/<uri>/participants/<pid>/mute
    # with the cached per-room bearer token), so the conference's own
    # web handler remains the canonical implementation. participant_id
    # is the stable per-session token published in conference-participants.
    sylkrtc = FixedValueProperty('videoroom-mute-participant')
    participant_id = StringProperty()
    muted = BooleanProperty()


class VideoroomComposingIndicationRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-composing-indication')
    state = LimitedChoiceProperty(['active', 'idle'])
    refresh = IntegerProperty(optional=True)


class VideoroomMuteAudioParticipantsRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-mute-audio-participants')


class VideoroomToggleHandRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-toggle-hand')
    session_id = StringProperty(optional=True)


# SylkRTC request to model mapping

class ProtocolError(Exception):
    pass


class SylkRTCRequest(object):
    __classmap__ = {cls.sylkrtc.value: cls for cls in subclasses(SylkRTCRequestBase) if hasattr(cls, 'sylkrtc')}

    @classmethod
    def from_message(cls, message):
        try:
            request_type = message['sylkrtc']
        except KeyError:
            raise ProtocolError('could not get WebSocket message type')
        try:
            request_class = cls.__classmap__[request_type]
        except KeyError:
            raise ProtocolError('unknown WebSocket request: %s' % request_type)
        return request_class(**message)
