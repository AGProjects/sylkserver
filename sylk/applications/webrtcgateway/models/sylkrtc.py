
from application.python import subclasses
from sipsimple.core import SIPURI, SIPCoreError

from .jsonobjects import Validator, CompositeValidator
from .jsonobjects import JSONObject, BooleanProperty, IntegerProperty, StringProperty, ArrayProperty, ObjectProperty, FixedValueProperty
from .jsonobjects import JSONArray, StringArray


# Validators

class AORValidator(Validator):
    def validate(self, value):
        prefix, sep, suffix = value.partition(':')
        if sep and prefix in ('sip', 'sips'):
            aor = suffix
        else:
            aor = value
        try:
            SIPURI.parse('sip:' + aor)
        except SIPCoreError:
            raise ValueError('invalid SIP URI: {}'.format(value))
        return aor


class URIValidator(Validator):
    def validate(self, value):
        prefix, sep, suffix = value.partition(':')
        if sep and prefix in ('sip', 'sips'):
            uri = 'sip:' + suffix
        else:
            uri = 'sip:' + value
        try:
            SIPURI.parse(uri)
        except SIPCoreError:
            raise ValueError('invalid SIP URI: {}'.format(value))
        return uri


class UniqueItemsValidator(Validator):
    def validate(self, sequence):
        seen = set()
        unique = []
        for item in sequence:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique


class LengthValidator(Validator):
    def __init__(self, minimum=0, maximum=float('inf')):
        self.minimum = minimum
        self.maximum = maximum

    def validate(self, value):
        if self.minimum <= len(value) <= self.maximum:
            return value
        else:
            raise ValueError("the value's length must be between {0.minimum} and {0.maximum} inclusive".format(self))


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
    display_name = StringProperty(optional=True)


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


# Response models

class AckResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueProperty('ack')


class ErrorResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueProperty('error')
    error = StringProperty()


# Connection events

class ReadyEvent(JSONObject):
    sylkrtc = FixedValueProperty('ready-event')


# Account events

class AccountIncomingSessionEvent(AccountEventBase):
    event = FixedValueProperty('incoming-session')
    session = StringProperty()
    originator = ObjectProperty(SIPIdentity)
    sdp = StringProperty()


class AccountMissedSessionEvent(AccountEventBase):
    event = FixedValueProperty('missed-session')
    originator = ObjectProperty(SIPIdentity)


class AccountConferenceInviteEvent(AccountEventBase):
    event = FixedValueProperty('conference-invite')
    room = StringProperty(validator=AORValidator())
    originator = ObjectProperty(SIPIdentity)


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


class SessionAcceptedEvent(SessionStateEvent):
    state = FixedValueProperty('accepted')
    sdp = StringProperty(optional=True)  # missing for incoming sessions


class SessionEstablishedEvent(SessionStateEvent):
    state = FixedValueProperty('established')


class SessionTerminatedEvent(SessionStateEvent):
    state = FixedValueProperty('terminated')
    reason = StringProperty(optional=True)


# Video room events

class VideoroomConfigureEvent(VideoroomEventBase):
    event = FixedValueProperty('configure')
    originator = StringProperty()
    active_participants = ArrayProperty(VideoroomActiveParticipants)


class VideoroomSessionProgressEvent(VideoroomSessionStateEvent):
    state = FixedValueProperty('progress')


class VideoroomSessionAcceptedEvent(VideoroomSessionStateEvent):
    state = FixedValueProperty('accepted')
    sdp = StringProperty()


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
    publishers = ArrayProperty(VideoroomPublishers)


class VideoroomPublishersJoinedEvent(VideoroomEventBase):
    event = FixedValueProperty('publishers-joined')
    publishers = ArrayProperty(VideoroomPublishers)


class VideoroomPublishersLeftEvent(VideoroomEventBase):
    event = FixedValueProperty('publishers-left')
    publishers = ArrayProperty(StringArray)


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
    old_token = StringProperty(optional=True)
    new_token = StringProperty(optional=True)


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
    candidates = ArrayProperty(ICECandidates)


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-terminate')


# Videoroom request models

class VideoroomJoinRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-join')
    account = StringProperty(validator=AORValidator())
    uri = StringProperty(validator=AORValidator())
    sdp = StringProperty()


class VideoroomLeaveRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-leave')


class VideoroomConfigureRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-configure')
    active_participants = ArrayProperty(VideoroomActiveParticipants)


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
    participants = ArrayProperty(AORList)


class VideoroomSessionTrickleRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-session-trickle')
    candidates = ArrayProperty(ICECandidates)


class VideoroomSessionUpdateRequest(VideoroomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-session-update')
    options = ObjectProperty(VideoroomSessionOptions)


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
