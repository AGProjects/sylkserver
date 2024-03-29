
from .jsonobjects import IntegerProperty, StringProperty, FixedValueProperty, LimitedChoiceProperty
from .jsonobjects import JSONObject


# Event base classes (abstract, should not be used directly)

class SylkRTCEventBase(JSONObject):
    platform = StringProperty(optional=True)
    app_id = StringProperty(optional=True)
    token = StringProperty()
    device_id = StringProperty(optional=True)
    silent = IntegerProperty(optional=True, default=0)
    call_id = StringProperty()

    def __init__(self, **kw):
        super(SylkRTCEventBase, self).__init__(**kw)



class CallEventBase(SylkRTCEventBase):
    event = None  # specified by subclass
    originator = StringProperty()
    from_display_name = StringProperty(optional=True, default=None)
    media_type = LimitedChoiceProperty(['audio', 'video', 'sms'])


# Events to use used in a SylkPushRequest

class ConferenceInviteEvent(CallEventBase):
    event = FixedValueProperty('incoming_conference_request')
    to = StringProperty()

    @property
    def __data__(self):
        data = super(ConferenceInviteEvent, self).__data__
        for key in data:
            # Fixup keys
            data[key.replace('_', '-')] = data.pop(key)
        data['from'] = data.pop('originator')
        return data


class MessageEvent(CallEventBase):
    event = FixedValueProperty('message')
    to = StringProperty()
    badge = IntegerProperty(default=1)

    @property
    def __data__(self):
        data = super(MessageEvent, self).__data__
        for key in list(data):
            # Fixup keys
            data[key.replace('_', '-')] = data.pop(key)
        data['from'] = data.pop('originator')
        return data
