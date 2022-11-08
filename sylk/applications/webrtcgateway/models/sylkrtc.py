
from application.python import subclasses

from .jsonobjects import BooleanProperty, IntegerProperty, StringProperty, ArrayProperty, ObjectProperty, FixedValueProperty, LimitedChoiceProperty, AbstractObjectProperty
from .jsonobjects import JSONObject, JSONArray, StringArray, CompositeValidator
from .validators import AORValidator, DisplayNameValidator, LengthValidator, UniqueItemsValidator

from sipsimple.util import ISOTimestamp

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


class AccountMarkConversationReadEventData(JSONObject):
    contact = StringProperty()


class AccountConversationRemoveEventData(JSONObject):
    contact = StringProperty()


class AccountDispositionNotificationEventData(JSONObject):
    message_id = StringProperty()
    state = LimitedChoiceProperty(['accepted', 'delivered', 'displayed', 'failed', 'processed', 'stored', 'forbidden', 'error'])
    message_timstamp = StringProperty()
    code = IntegerProperty()
    reason = StringProperty()

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


# Session events

class SessionProgressEvent(SessionStateEvent):
    state = FixedValueProperty('progress')


class SessionEarlyMediaEvent(SessionStateEvent):
    state = FixedValueProperty('early-media')
    sdp = StringProperty(optional=True)
    call_id = StringProperty(optional=True)


class SessionAcceptedEvent(SessionStateEvent):
    state = FixedValueProperty('accepted')
    sdp = StringProperty(optional=True)  # missing for incoming sessions
    call_id = StringProperty(optional=True)


class SessionEstablishedEvent(SessionStateEvent):
    state = FixedValueProperty('established')


class SessionTerminatedEvent(SessionStateEvent):
    state = FixedValueProperty('terminated')
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


class VideoroomRaisedHandsEvent(VideoroomEventBase):
    event = FixedValueProperty('raised-hands')
    raised_hands = ArrayProperty(VideoroomRaisedHands)


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


class AccountDispositionNotificationRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-disposition-notification')
    uri = StringProperty(validator=AORValidator())
    message_id = StringProperty()
    state = LimitedChoiceProperty(['delivered', 'failed', 'displayed', 'forbidden', 'error'])
    timestamp = StringProperty()


class AccountSyncConversationsRequest(AccountRequestBase):
    sylkrtc = FixedValueProperty('account-sync-conversations')
    message_id = StringProperty(optional=True)


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


# Session request models

class SessionCreateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-create')
    account = StringProperty(validator=AORValidator())
    uri = StringProperty(validator=AORValidator())
    sdp = StringProperty()


class SessionAnswerRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-answer')
    sdp = StringProperty()


class SessionTrickleRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-trickle')
    candidates = ArrayProperty(ICECandidates)  # type: ICECandidates


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-terminate')


class SessionMessageRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-message')
    message_id = StringProperty()
    content = StringProperty()
    content_type = StringProperty()
    timestamp = StringProperty()


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
