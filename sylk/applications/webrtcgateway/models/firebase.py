
from sipsimple.util import ISOTimestamp

from .jsonobjects import BooleanProperty, IntegerProperty, StringProperty, ObjectProperty, FixedValueProperty, LimitedChoiceProperty, AbstractObjectProperty
from .jsonobjects import JSONObject


class NotificationData(JSONObject):
    body = StringProperty(optional=True)
    sound = StringProperty(optional=True)
    title = StringProperty(optional=True)
    subtitle = StringProperty(optional=True)


class ApplicationData(JSONObject):
    sylkrtc = AbstractObjectProperty()


class FirebaseRequest(JSONObject):
    priority = LimitedChoiceProperty(['normal', 'high'], optional=True, default='high')
    content_available = BooleanProperty(optional=True, default=True)
    time_to_live = IntegerProperty(optional=True)  # for how long should the system try to deliver the notification (default is 4 weeks)
    to = StringProperty()
    notification = ObjectProperty(NotificationData)
    data = ObjectProperty(ApplicationData)

    def __init__(self, token, event, **kw):
        super(FirebaseRequest, self).__init__(to=token, data=dict(sylkrtc=event), notification=event.notification, **kw)


# Event base classes (abstract, should not be used directly)

class SylkRTCEventBase(JSONObject):
    timestamp = StringProperty()

    def __init__(self, **kw):
        kw['timestamp'] = str(ISOTimestamp.utcnow())
        super(SylkRTCEventBase, self).__init__(**kw)
        self.notification = NotificationData(body=self.notification_body)
        if self.notification_sound is not None:
            self.notification.sound = self.notification_sound

    @property
    def notification_body(self):
        raise NotImplementedError

    notification_sound = None


class CallEventBase(SylkRTCEventBase):
    event = None  # specified by subclass
    originator = StringProperty()
    destination = StringProperty()

    @property
    def notification_body(self):
        return '{0.event_description} from {0.originator}'.format(self)

    @property
    def event_description(self):
        return self.event.replace('-', ' ').capitalize()


# Events to use used in a FirebaseRequest

class IncomingCallEvent(CallEventBase):
    event = FixedValueProperty('incoming-call')


class MissedCallEvent(CallEventBase):
    event = FixedValueProperty('missed-call')


class ConferenceInviteEvent(CallEventBase):
    event = FixedValueProperty('conference-invite')
    room = StringProperty()

    @property
    def notification_body(self):
        return '{0.event_description} from {0.originator} to room {0.room}'.format(self)
