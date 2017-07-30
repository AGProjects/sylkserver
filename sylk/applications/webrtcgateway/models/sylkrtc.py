
from jsonmodels import models, fields, errors, validators
from sipsimple.core import SIPURI, SIPCoreError

__all__ = ('AccountAddRequest', 'AccountRemoveRequest', 'AccountRegisterRequest', 'AccountUnregisterRequest',
           'SessionCreateRequest', 'SessionAnswerRequest', 'SessionTrickleRequest', 'SessionTerminateRequest',
           'AckResponse', 'ErrorResponse',
           'ReadyEvent')


class FixedValueField(fields.BaseField):
    def __init__(self, value):
        super(FixedValueField, self).__init__(required=True)
        self.value = value

    def validate(self, value):
        if value != self.value:
            raise errors.ValidationError('field value should be {!r}'.format(self.value))

    # noinspection PyMethodOverriding
    def get_default_value(self):
        return self.value


class LimitedChoiceField(fields.BaseField):
    def __init__(self, values):
        super(LimitedChoiceField, self).__init__(required=True)
        self.values = set(values)

    def validate(self, value):
        if value not in self.values:
            raise errors.ValidationError('field value should be one of: {!s}'.format(', '.join(repr(item) for item in sorted(self.values))))


class URIValidator(object):
    @staticmethod
    def validate(value):
        if value.startswith(('sip:', 'sips:')):
            uri = value
        else:
            uri = 'sip:' + value
        try:
            SIPURI.parse(uri)
        except SIPCoreError:
            raise errors.ValidationError('invalid URI: %s' % value)


class URIListValidator(object):
    @staticmethod
    def validate(values):
        for item in values:
            URIValidator.validate(item)


# Base models

class SylkRTCRequestBase(models.Base):
    transaction = fields.StringField(required=True)


class SylkRTCResponseBase(models.Base):
    transaction = fields.StringField(required=True)


# Miscellaneous models

class AckResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueField('ack')


class ErrorResponse(SylkRTCResponseBase):
    sylkrtc = FixedValueField('error')
    error = fields.StringField(required=True)


class ICECandidate(models.Base):
    candidate = fields.StringField(required=True)
    sdpMLineIndex = fields.IntField(required=True)
    sdpMid = fields.StringField(required=True)


class ReadyEvent(models.Base):
    sylkrtc = FixedValueField('event')
    event = FixedValueField('ready')


# Account models

class AccountRequestBase(SylkRTCRequestBase):
    account = fields.StringField(required=True, validators=[URIValidator])


class AccountAddRequest(AccountRequestBase):
    sylkrtc = FixedValueField('account-add')
    password = fields.StringField(required=True, validators=[validators.Length(minimum_value=1, maximum_value=9999)])
    display_name = fields.StringField(required=False)
    user_agent = fields.StringField(required=False)


class AccountRemoveRequest(AccountRequestBase):
    sylkrtc = FixedValueField('account-remove')


class AccountRegisterRequest(AccountRequestBase):
    sylkrtc = FixedValueField('account-register')


class AccountUnregisterRequest(AccountRequestBase):
    sylkrtc = FixedValueField('account-unregister')


class AccountDeviceTokenRequest(AccountRequestBase):
    sylkrtc = FixedValueField('account-devicetoken')
    old_token = fields.StringField(required=False)
    new_token = fields.StringField(required=False)


# Session models

class SessionRequestBase(SylkRTCRequestBase):
    session = fields.StringField(required=True)


class SessionCreateRequest(SessionRequestBase):
    sylkrtc = FixedValueField('session-create')
    account = fields.StringField(required=True, validators=[URIValidator])
    uri = fields.StringField(required=True, validators=[URIValidator])
    sdp = fields.StringField(required=True)


class SessionAnswerRequest(SessionRequestBase):
    sylkrtc = FixedValueField('session-answer')
    sdp = fields.StringField(required=True)


class SessionTrickleRequest(SessionRequestBase):
    sylkrtc = FixedValueField('session-trickle')
    candidates = fields.ListField([ICECandidate])


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = FixedValueField('session-terminate')


# VideoRoom models

class VideoRoomRequestBase(SylkRTCRequestBase):
    session = fields.StringField(required=True)


class VideoRoomJoinRequest(VideoRoomRequestBase):
    sylkrtc = FixedValueField('videoroom-join')
    account = fields.StringField(required=True, validators=[URIValidator])
    uri = fields.StringField(required=True, validators=[URIValidator])
    sdp = fields.StringField(required=True)


class VideoRoomControlTrickleOptions(models.Base):
    # ID for the subscriber session, if specified, otherwise the publisher is considered
    session = fields.StringField(required=False)
    candidates = fields.ListField([ICECandidate])


class VideoRoomControlUpdateOptions(models.Base):
    audio = fields.BoolField(required=False)
    video = fields.BoolField(required=False)
    bitrate = fields.IntField(required=False)


class VideoRoomControlFeedAttachOptions(models.Base):
    session = fields.StringField(required=True)
    publisher = fields.StringField(required=True)


class VideoRoomControlFeedAnswerOptions(models.Base):
    session = fields.StringField(required=True)
    sdp = fields.StringField(required=True)


class VideoRoomControlFeedDetachOptions(models.Base):
    session = fields.StringField(required=True)


class VideoRoomControlInviteParticipantsOptions(models.Base):
    participants = fields.ListField([str, unicode], validators=[URIListValidator])


class VideoRoomControlRequest(VideoRoomRequestBase):
    sylkrtc = FixedValueField('videoroom-ctl')
    option = LimitedChoiceField({'feed-attach', 'feed-answer', 'feed-detach', 'invite-participants', 'trickle', 'update'})

    # all other options should have optional fields below, and the application needs to do a little validation
    trickle = fields.EmbeddedField(VideoRoomControlTrickleOptions, required=False)
    update = fields.EmbeddedField(VideoRoomControlUpdateOptions, required=False)
    feed_attach = fields.EmbeddedField(VideoRoomControlFeedAttachOptions, required=False)
    feed_answer = fields.EmbeddedField(VideoRoomControlFeedAnswerOptions, required=False)
    feed_detach = fields.EmbeddedField(VideoRoomControlFeedDetachOptions, required=False)
    invite_participants = fields.EmbeddedField(VideoRoomControlInviteParticipantsOptions, required=False)


class VideoRoomTerminateRequest(VideoRoomRequestBase):
    sylkrtc = FixedValueField('videoroom-terminate')
