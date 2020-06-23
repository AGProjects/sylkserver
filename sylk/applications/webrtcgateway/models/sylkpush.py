
from .jsonobjects import IntegerProperty, StringProperty, FixedValueProperty, ObjectProperty, AbstractObjectProperty
from .jsonobjects import JSONObject


class PushBody(JSONObject):
    _content = AbstractObjectProperty(optional=True)


class PushData(JSONObject):
    body = ObjectProperty(PushBody)
    call_id = StringProperty()
    token = StringProperty()
    reason = StringProperty()
    url = StringProperty()


class PushReply(JSONObject):
    code = IntegerProperty()
    description = StringProperty()
    data = ObjectProperty(PushData)


# Event base classes (abstract, should not be used directly)

class SylkRTCEventBase(JSONObject):
    platform = StringProperty(optional=True)
    app_id = StringProperty(optional=True)
    token = StringProperty()
    device_id = StringProperty(optional=True)
    media_type = FixedValueProperty('video')
    silent = IntegerProperty(optional=True, default=0)
    call_id = StringProperty()

    def __init__(self, **kw):
        super(SylkRTCEventBase, self).__init__(**kw)



class CallEventBase(SylkRTCEventBase):
    event = None  # specified by subclass
    originator = StringProperty()
    from_display_name = StringProperty(optional=True, default=None)


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

