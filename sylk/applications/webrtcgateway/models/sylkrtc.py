
from application.python import subclasses
from sipsimple.core import SIPURI, SIPCoreError

from .jsonobjects import Validator, CompositeValidator
from .jsonobjects import JSONObject, AbstractProperty, BooleanProperty, IntegerProperty, StringProperty, ArrayProperty, ObjectProperty
from .jsonobjects import JSONArray, StringArray


# Validators

class URIValidator(Validator):
    def validate(self, value):
        if value.startswith(('sip:', 'sips:')):
            uri = value
        else:
            uri = 'sip:' + value
        try:
            SIPURI.parse(uri)
        except SIPCoreError:
            raise ValueError('invalid SIP URI: {}'.format(value))
        return value


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


# Custom JSONObject properties

class FixedValueProperty(AbstractProperty):
    def __init__(self, value):
        super(FixedValueProperty, self).__init__(optional=True, default=value)
        self.value = value

    def _parse(self, value):
        if value != self.value:
            raise ValueError('Invalid value for {property.name!r} property: {value!r} (should be {property.value!r})'.format(property=self, value=value))
        return value


class LimitedChoiceProperty(AbstractProperty):
    def __init__(self, *values):
        if len(values) == 0:
            raise ValueError('{.__class__.__name__} needs at least one argument'.format(self))
        super(LimitedChoiceProperty, self).__init__()
        self.values = frozenset(values)
        self.values_string = ' or '.join(', '.join(sorted(values)).rsplit(', ', 1))

    def _parse(self, value):
        if value not in self.values:
            raise ValueError('Invalid value for {property.name!r} property: {value!r} (expected: {property.values_string})'.format(property=self, value=value))
        return value


# Miscellaneous models

class ICECandidate(JSONObject):
    candidate = StringProperty()
    sdpMLineIndex = IntegerProperty()
    sdpMid = StringProperty()


class ICECandidates(JSONArray):
    item_type = ICECandidate


class URIList(StringArray):
    list_validator = UniqueItemsValidator()
    item_validator = URIValidator()


class VideoRoomActiveParticipants(StringArray):
    list_validator = CompositeValidator(UniqueItemsValidator(), LengthValidator(maximum=2))


# Events

class ReadyEvent(JSONObject):
    sylkrtc = FixedValueProperty('event')
    event = FixedValueProperty('ready')


class VideoRoomConfigurationEvent(JSONObject):
    sylkrtc = FixedValueProperty('videoroom_event')  # todo: rename with dashes or underscores?
    event = FixedValueProperty('configure-room')
    session = StringProperty()
    originator = StringProperty()
    active_participants = ArrayProperty(VideoRoomActiveParticipants)


# Base models

class SylkRTCRequestBase(JSONObject):
    transaction = StringProperty()


class SylkRTCResponseBase(JSONObject):
    transaction = StringProperty()


class AccountRequestBase(SylkRTCRequestBase):
    account = StringProperty(validator=URIValidator())


class SessionRequestBase(SylkRTCRequestBase):
    session = StringProperty()


class VideoRoomRequestBase(SylkRTCRequestBase):
    session = StringProperty()


# Response models

class AckResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueProperty('ack')


class ErrorResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueProperty('error')
    error = StringProperty()


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
    account = StringProperty(validator=URIValidator())
    uri = StringProperty(validator=URIValidator())
    sdp = StringProperty()


class SessionAnswerRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-answer')
    sdp = StringProperty()


class SessionTrickleRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-trickle')
    candidates = ArrayProperty(ICECandidates)


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = FixedValueProperty('session-terminate')


# VideoRoomControlRequest embedded models

class VideoRoomControlConfigureRoomOptions(JSONObject):
    active_participants = ArrayProperty(VideoRoomActiveParticipants)


class VideoRoomControlFeedAttachOptions(JSONObject):
    session = StringProperty()
    publisher = StringProperty()


class VideoRoomControlFeedAnswerOptions(JSONObject):
    session = StringProperty()
    sdp = StringProperty()


class VideoRoomControlFeedDetachOptions(JSONObject):
    session = StringProperty()


class VideoRoomControlInviteParticipantsOptions(JSONObject):
    participants = ArrayProperty(URIList)


class VideoRoomControlTrickleOptions(JSONObject):
    # ID for the subscriber session, if specified, otherwise the publisher is considered
    session = StringProperty(optional=True)
    candidates = ArrayProperty(ICECandidates)


class VideoRoomControlUpdateOptions(JSONObject):
    audio = BooleanProperty(optional=True)
    video = BooleanProperty(optional=True)
    bitrate = IntegerProperty(optional=True)


# VideoRoom request models

class VideoRoomJoinRequest(VideoRoomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-join')
    account = StringProperty(validator=URIValidator())
    uri = StringProperty(validator=URIValidator())
    sdp = StringProperty()


class VideoRoomControlRequest(VideoRoomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-ctl')
    option = LimitedChoiceProperty('configure-room', 'feed-attach', 'feed-answer', 'feed-detach', 'invite-participants', 'trickle', 'update')

    # all other options should have optional fields below, and the application needs to do a little validation
    configure_room = ObjectProperty(VideoRoomControlConfigureRoomOptions, optional=True)
    feed_attach = ObjectProperty(VideoRoomControlFeedAttachOptions, optional=True)
    feed_answer = ObjectProperty(VideoRoomControlFeedAnswerOptions, optional=True)
    feed_detach = ObjectProperty(VideoRoomControlFeedDetachOptions, optional=True)
    invite_participants = ObjectProperty(VideoRoomControlInviteParticipantsOptions, optional=True)
    trickle = ObjectProperty(VideoRoomControlTrickleOptions, optional=True)
    update = ObjectProperty(VideoRoomControlUpdateOptions, optional=True)


class VideoRoomTerminateRequest(VideoRoomRequestBase):
    sylkrtc = FixedValueProperty('videoroom-terminate')


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
