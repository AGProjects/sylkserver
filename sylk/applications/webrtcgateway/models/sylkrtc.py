
import re

from jsonmodels import models, fields, errors, validators
from sipsimple.core import SIPURI, SIPCoreError

__all__ = ('AccountAddRequest', 'AccountRemoveRequest', 'AccountRegisterRequest', 'AccountUnregisterRequest',
           'SessionCreateRequest', 'SessionAnswerRequest', 'SessionTrickleRequest', 'SessionTerminateRequest',
           'AckResponse', 'ErrorResponse',
           'ReadyEvent')

SIP_PREFIX_RE = re.compile('^sips?:')


class DefaultValueField(fields.BaseField):
    def __init__(self, value):
        self.default_value = value
        super(DefaultValueField, self).__init__()

    def validate(self, value):
        if value != self.default_value:
            raise errors.ValidationError('%s does not match the expected value %s' % (value, self.default_value))

    # noinspection PyMethodOverriding
    def get_default_value(self):
        return self.default_value


class URIValidator(object):
    @staticmethod
    def validate(value):
        uri = SIP_PREFIX_RE.sub('', value)
        try:
            SIPURI.parse('sip:%s' % uri)
        except SIPCoreError:
            raise errors.ValidationError('invalid URI: %s' % value)


class URIListValidator(object):
    @staticmethod
    def validate(values):
        for item in values:
            URIValidator.validate(item)


class OptionsValidator(object):
    def __init__(self, options):
        self.options = options

    def validate(self, value):
        if value not in self.options:
            raise errors.ValidationError('invalid option: %s' % value)


# Base models

class SylkRTCRequestBase(models.Base):
    transaction = fields.StringField(required=True)


class SylkRTCResponseBase(models.Base):
    transaction = fields.StringField(required=True)


# Miscellaneous models

class AckResponse(SylkRTCResponseBase):
    sylkrtc = DefaultValueField('ack')


class ErrorResponse(SylkRTCResponseBase):
    sylkrtc = DefaultValueField('error')
    error = fields.StringField(required=True)


class ICECandidate(models.Base):
    candidate = fields.StringField(required=True)
    sdpMLineIndex = fields.IntField(required=True)
    sdpMid = fields.StringField(required=True)


class ReadyEvent(models.Base):
    sylkrtc = DefaultValueField('event')
    event = DefaultValueField('ready')


# Account models

class AccountRequestBase(SylkRTCRequestBase):
    account = fields.StringField(required=True, validators=[URIValidator])


class AccountAddRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-add')
    password = fields.StringField(required=True, validators=[validators.Length(minimum_value=1, maximum_value=9999)])
    display_name = fields.StringField(required=False)
    user_agent = fields.StringField(required=False)


class AccountRemoveRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-remove')


class AccountRegisterRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-register')


class AccountUnregisterRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-unregister')


class AccountDeviceTokenRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-devicetoken')
    old_token = fields.StringField(required=False)
    new_token = fields.StringField(required=False)


# Session models

class SessionRequestBase(SylkRTCRequestBase):
    session = fields.StringField(required=True)


class SessionCreateRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-create')
    account = fields.StringField(required=True, validators=[URIValidator])
    uri = fields.StringField(required=True, validators=[URIValidator])
    sdp = fields.StringField(required=True)


class SessionAnswerRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-answer')
    sdp = fields.StringField(required=True)


class SessionTrickleRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-trickle')
    candidates = fields.ListField([ICECandidate])


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-terminate')


# VideoRoom models

class VideoRoomRequestBase(SylkRTCRequestBase):
    session = fields.StringField(required=True)


class VideoRoomJoinRequest(VideoRoomRequestBase):
    sylkrtc = DefaultValueField('videoroom-join')
    account = fields.StringField(required=True, validators=[URIValidator])
    uri = fields.StringField(required=True, validators=[URIValidator])
    sdp = fields.StringField(required=True)


class VideoRoomControlTrickleRequest(models.Base):
    # ID for the subscriber session, if specified, otherwise the publisher is considered
    session = fields.StringField(required=False)
    candidates = fields.ListField([ICECandidate])


class VideoRoomControlUpdateRequest(models.Base):
    audio = fields.BoolField(required=False)
    video = fields.BoolField(required=False)
    bitrate = fields.IntField(required=False)


class VideoRoomControlFeedAttachRequest(models.Base):
    session = fields.StringField(required=True)
    publisher = fields.StringField(required=True)


class VideoRoomControlFeedAnswerRequest(models.Base):
    session = fields.StringField(required=True)
    sdp = fields.StringField(required=True)


class VideoRoomControlFeedDetachRequest(models.Base):
    session = fields.StringField(required=True)


class VideoRoomControlInviteParticipantsRequest(models.Base):
    participants = fields.ListField([str, unicode], validators=[URIListValidator])


class VideoRoomControlRequest(VideoRoomRequestBase):
    sylkrtc = DefaultValueField('videoroom-ctl')
    option = fields.StringField(required=True, validators=[OptionsValidator(['trickle', 'update', 'feed-attach', 'feed-answer', 'feed-detach', 'invite-participants'])])

    # all other options should have optional fields below, and the application needs to do a little validation
    trickle = fields.EmbeddedField(VideoRoomControlTrickleRequest, required=False)
    update = fields.EmbeddedField(VideoRoomControlUpdateRequest, required=False)
    feed_attach = fields.EmbeddedField(VideoRoomControlFeedAttachRequest, required=False)
    feed_answer = fields.EmbeddedField(VideoRoomControlFeedAnswerRequest, required=False)
    feed_detach = fields.EmbeddedField(VideoRoomControlFeedDetachRequest, required=False)
    invite_participants = fields.EmbeddedField(VideoRoomControlInviteParticipantsRequest, required=False)


class VideoRoomTerminateRequest(VideoRoomRequestBase):
    sylkrtc = DefaultValueField('videoroom-terminate')
