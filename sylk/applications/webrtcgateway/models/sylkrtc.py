
__all__ = ['AccountAddRequest', 'AccountRemoveRequest', 'AccountRegisterRequest', 'AccountUnregisterRequest',
           'SessionCreateRequest', 'SessionAnswerRequest', 'SessionTrickleRequest', 'SessionTerminateRequest',
           'AckResponse', 'ErrorResponse',
           'ReadyEvent']

import re

from jsonmodels import models, fields, errors, validators
from sipsimple.core import SIPURI, SIPCoreError

SIP_PREFIX_RE = re.compile('^sips?:')


class DefaultValueField(fields.BaseField):
    def __init__(self, value):
        self.default_value = value
        super(DefaultValueField, self).__init__()

    def validate(self, value):
        if value != self.default_value:
            raise errors.ValidationError('%s doesn\'t match the expected value %s' % (value, self.default_value))

    def get_default_value(self):
        return self.default_value


def URIValidator(value):
    account = SIP_PREFIX_RE.sub('', value)
    try:
        SIPURI.parse('sip:%s' % account)
    except SIPCoreError:
        raise errors.ValidationError('invalid account: %s' % value)


# Base models

class SylkRTCRequestBase(models.Base):
    transaction = fields.StringField(required=True)


class SylkRTCResponseBase(models.Base):
    transaction = fields.StringField(required=True)


# Miscelaneous models

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
    account = fields.StringField(required=True,
                                 validators=[URIValidator])


class AccountAddRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-add')
    password = fields.StringField(required=True,
                                  validators=[validators.Length(minimum_value=1, maximum_value=9999)])


class AccountRemoveRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-remove')


class AccountRegisterRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-register')


class AccountUnregisterRequest(AccountRequestBase):
    sylkrtc = DefaultValueField('account-unregister')


# Session models

class SessionRequestBase(SylkRTCRequestBase):
    session = fields.StringField(required=True)


class SessionCreateRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-create')
    account = fields.StringField(required=True,
                                 validators=[URIValidator])
    uri = fields.StringField(required=True,
                             validators=[URIValidator])
    sdp = fields.StringField(required=True)


class SessionAnswerRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-answer')
    sdp = fields.StringField(required=True)


class SessionTrickleRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-trickle')
    candidates = fields.ListField([ICECandidate])


class SessionTerminateRequest(SessionRequestBase):
    sylkrtc = DefaultValueField('session-terminate')


