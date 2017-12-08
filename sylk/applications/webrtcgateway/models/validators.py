
from sipsimple.core import SIPURI, SIPCoreError

from .jsonobjects import Validator


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


class DisplayNameValidator(Validator):
    def validate(self, value):
        return value.strip('" ')  # strip quotes if present


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
