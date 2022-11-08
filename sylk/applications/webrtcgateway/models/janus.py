
import os

from application.python import subclasses
from application.python.descriptor import classproperty
from binascii import b2a_hex as hex_encode
from typing import Union

from ..configuration import JanusConfig
from .jsonobjects import AbstractProperty, BooleanProperty, IntegerProperty, StringProperty, ArrayProperty, ObjectProperty
from .jsonobjects import FixedValueProperty, LimitedChoiceProperty, AbstractObjectProperty, JSONObject, JSONArray
from .sylkrtc import ICECandidates
from .validators import URIValidator


# Base models (these are abstract and should not be used directly)

class JanusRequestBase(JSONObject):
    transaction = StringProperty()
    apisecret = FixedValueProperty(JanusConfig.api_secret or None)

    def __init__(self, **kw):
        if 'transaction' not in kw:
            kw['transaction'] = hex_encode(os.urandom(16)).decode()  # uuid4().hex is really slow
        super(JanusRequestBase, self).__init__(**kw)


class JanusCoreMessageBase(JSONObject):    # base class for messages/ sent by the Janus core (responses and events)
    pass


class JanusPluginMessageBase(JSONObject):  # base class for messages sent by the Janus plugins (responses and events)
    pass


class PluginDataBase(JSONObject):
    pass


class SIPPluginData(PluginDataBase):
    __plugin__ = 'sip'

    # noinspection PyMethodParameters,PyUnresolvedReferences
    @classproperty
    def __id__(cls):
        if cls is SIPErrorEvent:
            return cls.__plugin__, cls.sip.value, cls.error.name
        else:
            return cls.__plugin__, cls.sip.value, cls.result.object_type.event.value


class VideoroomPluginData(PluginDataBase):
    __plugin__ = 'videoroom'
    __event__ = None  # for events, the name of the property that describes the event

    # noinspection PyMethodParameters,PyUnresolvedReferences
    @classproperty
    def __id__(cls):
        if cls.__event__ is not None:
            return cls.__plugin__, cls.videoroom.value, cls.__event__
        else:
            return cls.__plugin__, cls.videoroom.value


class CoreResponse(JanusCoreMessageBase):
    transaction = StringProperty()


class CoreEvent(JanusCoreMessageBase):
    session_id = IntegerProperty()
    sender = IntegerProperty()


# Miscellaneous models

class NumericId(JSONObject):
    id = IntegerProperty()


class UserId(JSONObject):
    id = IntegerProperty()
    display = StringProperty(optional=True)


class ErrorInfo(JSONObject):
    code = IntegerProperty()
    reason = StringProperty()


class PluginDataContainer(JSONObject):
    plugin = StringProperty()
    data = AbstractObjectProperty()  # type: Union[SIPPluginData, VideoroomPluginData]


class SIPDataContainer(JSONObject):
    sip = FixedValueProperty('event')
    result = AbstractObjectProperty()


class SDPOffer(JSONObject):
    type = FixedValueProperty('offer')
    sdp = StringProperty()


class SDPAnswer(JSONObject):
    type = FixedValueProperty('answer')
    sdp = StringProperty()


class JSEP(JSONObject):
    type = LimitedChoiceProperty(['offer', 'answer'])
    sdp = StringProperty()


class VideoroomPublisher(JSONObject):
    id = IntegerProperty()
    display = StringProperty(optional=True)
    audio_codec = StringProperty(optional=True)
    video_codec = StringProperty(optional=True)
    talking = BooleanProperty(optional=True)


class VideoroomPublishers(JSONArray):
    item_type = VideoroomPublisher

class ContactParams(JSONObject):
    pn_app = StringProperty(optional=True)
    pn_tok = StringProperty(optional=True)
    pn_type = StringProperty(optional=True)
    pn_silent = StringProperty(optional=True)
    pn_device = StringProperty(optional=True)

    @property
    def __data__(self):
        data = super(ContactParams, self).__data__
        for key in list(data):
            data[key.replace('_', '-')] = data.pop(key)
        return data

# Janus requests

class InfoRequest(JanusRequestBase):
    janus = FixedValueProperty('info')


class SessionCreateRequest(JanusRequestBase):
    janus = FixedValueProperty('create')


class SessionDestroyRequest(JanusRequestBase):
    janus = FixedValueProperty('destroy')
    session_id = IntegerProperty()


class SessionKeepaliveRequest(JanusRequestBase):
    janus = FixedValueProperty('keepalive')
    session_id = IntegerProperty()


class PluginAttachRequest(JanusRequestBase):
    janus = FixedValueProperty('attach')
    session_id = IntegerProperty()
    plugin = StringProperty()


class PluginDetachRequest(JanusRequestBase):
    janus = FixedValueProperty('detach')
    session_id = IntegerProperty()
    handle_id = IntegerProperty()


class MessageRequest(JanusRequestBase):
    janus = FixedValueProperty('message')
    session_id = IntegerProperty()
    handle_id = IntegerProperty()
    body = AbstractObjectProperty()
    jsep = AbstractObjectProperty(optional=True)


class TrickleRequest(JanusRequestBase):
    janus = FixedValueProperty('trickle')
    session_id = IntegerProperty()
    handle_id = IntegerProperty()
    candidates = ArrayProperty(ICECandidates)  # type: ICECandidates

    @property
    def __data__(self):
        data = super(TrickleRequest, self).__data__
        candidates = data.pop('candidates', [])
        if len(candidates) == 0:
            data['candidate'] = {'completed': True}
            # data['candidate'] = None
        elif len(candidates) == 1:
            data['candidate'] = candidates[0]
        else:
            data['candidates'] = candidates
        return data


# SIP plugin messages (to be used as body for MessageRequest for the SIP plugin messages)

class SIPRegister(JSONObject):
    request = FixedValueProperty('register')
    username = StringProperty()  # this is the account.uri which is already a valid URI
    ha1_secret = StringProperty()
    display_name = StringProperty(optional=True)
    user_agent = StringProperty(optional=True)
    proxy = StringProperty(optional=True)
    send_register = BooleanProperty(optional=True)
    contact_params = ObjectProperty(ContactParams)

class SIPUnregister(JSONObject):
    request = FixedValueProperty('unregister')


class SIPCall(JSONObject):
    request = FixedValueProperty('call')
    uri = StringProperty(validator=URIValidator())  # this comes from the client request.uri which was validated as an AOR and we need a URI
    srtp = LimitedChoiceProperty(['sdes_optional', 'sdes_mandatory'], optional=True)


class SIPAccept(JSONObject):
    request = FixedValueProperty('accept')


class SIPDecline(JSONObject):
    request = FixedValueProperty('decline')
    code = IntegerProperty()


class SIPHangup(JSONObject):
    request = FixedValueProperty('hangup')


class SIPMessage(JSONObject):
    request = FixedValueProperty('message')
    content_type = StringProperty()
    content = StringProperty()


# Videoroom plugin messages (to be used as body for MessageRequest for videoroom plugin messages)

class VideoroomCreate(JSONObject):
    request = FixedValueProperty('create')
    room = IntegerProperty()
    pin = StringProperty(optional=True)
    description = StringProperty(optional=True)
    publishers = IntegerProperty(optional=True, default=10)
    videocodec = StringProperty(optional=True)  # don't need a validator, as the value comes from the configuration and it's validated there
    bitrate = IntegerProperty(optional=True)    # don't need a validator, as the value comes from the configuration and it's validated there
    record = BooleanProperty(optional=True, default=False)
    rec_dir = StringProperty(optional=True)


class VideoroomDestroy(JSONObject):
    request = FixedValueProperty('destroy')
    room = IntegerProperty()


class VideoroomJoin(JSONObject):
    request = FixedValueProperty('joinandconfigure')
    ptype = FixedValueProperty('publisher')
    room = IntegerProperty()
    display = StringProperty(optional=True)
    audio = BooleanProperty(optional=True, default=True)
    video = BooleanProperty(optional=True, default=True)
    bitrate = IntegerProperty(optional=True)


class VideoroomLeave(JSONObject):
    request = FixedValueProperty('leave')


class VideoroomUpdatePublisher(JSONObject):
    request = FixedValueProperty('configure')
    audio = BooleanProperty(optional=True)
    video = BooleanProperty(optional=True)
    bitrate = IntegerProperty(optional=True)


class VideoroomFeedAttach(JSONObject):
    request = FixedValueProperty('join')
    ptype = FixedValueProperty('subscriber')
    room = IntegerProperty()
    feed = IntegerProperty()
    offer_audio = BooleanProperty(optional=True, default=True)
    offer_video = BooleanProperty(optional=True, default=True)


class VideoroomFeedDetach(JSONObject):
    request = FixedValueProperty('leave')


class VideoroomFeedStart(JSONObject):
    request = FixedValueProperty('start')


class VideoroomFeedPause(JSONObject):
    request = FixedValueProperty('pause')


class VideoroomFeedUpdate(JSONObject):
    request = FixedValueProperty('configure')
    audio = BooleanProperty(optional=True)
    video = BooleanProperty(optional=True)
    substream = IntegerProperty(optional=True)       # for VP8 simulcast
    temporal = IntegerProperty(optional=True)        # for VP8 simulcast
    spatial_layer = IntegerProperty(optional=True)   # for VP9 SVC
    temporal_layer = IntegerProperty(optional=True)  # for VP9 SVC


# Janus core messages

class AckResponse(CoreResponse):
    janus = FixedValueProperty('ack')
    session_id = IntegerProperty()


class ErrorResponse(CoreResponse):
    janus = FixedValueProperty('error')
    session_id = IntegerProperty(optional=True)  # not used
    error = ObjectProperty(ErrorInfo)            # type: ErrorInfo


class SuccessResponse(CoreResponse):
    janus = FixedValueProperty('success')
    session_id = IntegerProperty(optional=True)      # not used
    data = ObjectProperty(NumericId, optional=True)  # type: NumericId


class ServerInfoResponse(CoreResponse):
    janus = FixedValueProperty('server_info')
    name = StringProperty()
    version = IntegerProperty()
    version_string = StringProperty()
    data_channels = BooleanProperty()


class DetachedEvent(CoreEvent):
    janus = FixedValueProperty('detached')


class HangupEvent(CoreEvent):
    janus = FixedValueProperty('hangup')
    reason = StringProperty(optional=True)


class MediaEvent(CoreEvent):
    janus = FixedValueProperty('media')
    type = LimitedChoiceProperty(['audio', 'video'])
    receiving = BooleanProperty()
    seconds = IntegerProperty(optional=True)


class SlowlinkEvent(CoreEvent):
    janus = FixedValueProperty('slowlink')
    media = LimitedChoiceProperty(['audio', 'video'])
    uplink = BooleanProperty()
    lost = IntegerProperty()


class WebrtcUpEvent(CoreEvent):
    janus = FixedValueProperty('webrtcup')


# Janus plugin messages

class PluginResponse(JanusPluginMessageBase):
    janus = FixedValueProperty('success')
    transaction = StringProperty()
    session_id = IntegerProperty()
    sender = IntegerProperty()
    plugindata = ObjectProperty(PluginDataContainer)  # type: PluginDataContainer


class PluginEvent(JanusPluginMessageBase):
    janus = FixedValueProperty('event')
    transaction = StringProperty(optional=True)
    session_id = IntegerProperty()
    sender = IntegerProperty()
    plugindata = ObjectProperty(PluginDataContainer)  # type: PluginDataContainer
    jsep = ObjectProperty(JSEP, optional=True)        # type: JSEP


# SIP plugin data messages

# Results

class SIPResultRegistering(JSONObject):
    event = FixedValueProperty('registering')


class SIPResultRegistered(JSONObject):
    event = FixedValueProperty('registered')
    username = StringProperty()  # not used
    register_sent = BooleanProperty()


class SIPResultRegistrationFailed(JSONObject):
    event = FixedValueProperty('registration_failed')
    code = IntegerProperty()
    reason = StringProperty()


class SIPResultUnregistering(JSONObject):
    event = FixedValueProperty('unregistering')


class SIPResultUnregistered(JSONObject):
    event = FixedValueProperty('unregistered')
    username = StringProperty()  # not used


class SIPResultCalling(JSONObject):
    event = FixedValueProperty('calling')


class SIPResultRinging(JSONObject):
    event = FixedValueProperty('ringing')


class SIPResultProceeding(JSONObject):
    event = FixedValueProperty('proceeding')
    code = IntegerProperty()


class SIPResultProgress(JSONObject):
    event = FixedValueProperty('progress')
    username = StringProperty()  # not used


class SIPResultDeclining(JSONObject):
    event = FixedValueProperty('declining')
    code = IntegerProperty()


class SIPResultAccepting(JSONObject):
    event = FixedValueProperty('accepting')


class SIPResultAccepted(JSONObject):
    event = FixedValueProperty('accepted')
    username = StringProperty(optional=True)  # not used (only present for outgoing)


class SIPResultHolding(JSONObject):
    event = FixedValueProperty('holding')


class SIPResultResuming(JSONObject):
    event = FixedValueProperty('resuming')


class SIPResultHangingUp(JSONObject):
    event = FixedValueProperty('hangingup')


class SIPResultHangup(JSONObject):
    event = FixedValueProperty('hangup')
    code = IntegerProperty()
    reason = StringProperty()


class SIPResultIncomingCall(JSONObject):
    event = FixedValueProperty('incomingcall')
    username = StringProperty()
    displayname = StringProperty(optional=True)
    srtp = LimitedChoiceProperty(['sdes_optional', 'sdes_mandatory'], optional=True)


class SIPResultMissedCall(JSONObject):
    event = FixedValueProperty('missed_call')
    caller = StringProperty()
    displayname = StringProperty(optional=True)


class SIPResultInfo(JSONObject):
    event = FixedValueProperty('info')
    sender = StringProperty()
    displayname = StringProperty(optional=True)
    type = StringProperty()
    content = StringProperty()


class SIPResultInfoSent(JSONObject):
    event = FixedValueProperty('infosent')


class SIPResultMessage(JSONObject):
    event = FixedValueProperty('message')
    sender = StringProperty()
    displayname = StringProperty(optional=True)
    content = StringProperty()
    content_type = StringProperty(optional=True, default='text/plain')


class SIPResultMessageSent(JSONObject):
    event = FixedValueProperty('messagesent')


class SIPResultMessageDelivery(JSONObject):
    event = FixedValueProperty('messagedelivery')
    code = IntegerProperty()
    reason = StringProperty()


class SIPResultDTMFSent(JSONObject):
    event = FixedValueProperty('dtmfsent')


# Data messages

class SIPErrorEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    error_code = IntegerProperty()
    error = StringProperty()


class SIPRegisteringEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultRegistering)         # type: SIPResultRegistering


class SIPRegisteredEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultRegistered)          # type: SIPResultRegistered


class SIPRegistrationFailedEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultRegistrationFailed)  # type: SIPResultRegistrationFailed


class SIPUnregisteringEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultUnregistering)       # type: SIPResultUnregistering


class SIPUnregisteredEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultUnregistered)        # type: SIPResultRegistered


class SIPCallingEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultCalling)             # type: SIPResultCalling


class SIPRingingEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultRinging)             # type: SIPResultRinging


class SIPProceedingEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultProceeding)          # type: SIPResultProceeding


class SIPProgressEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultProgress)            # type: SIPResultProgress
    call_id = StringProperty(optional=True)


class SIPDecliningEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultDeclining)           # type: SIPResultDeclining


class SIPAcceptingEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultAccepting)           # type: SIPResultAccepting


class SIPAcceptedEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultAccepted)            # type: SIPResultAccepted
    call_id = StringProperty()


class SIPHoldingEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultHolding)             # type: SIPResultHolding


class SIPResumingEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultResuming)            # type: SIPResultResuming


class SIPHangingUpEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultHangingUp)           # type: SIPResultHangingUp


class SIPHangupEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultHangup)              # type: SIPResultHangup


class SIPIncomingCallEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultIncomingCall)        # type: SIPResultIncomingCall
    call_id = StringProperty()


class SIPMissedCallEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultMissedCall)          # type: SIPResultMissedCall


class SIPInfoEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultInfo)                # type: SIPResultInfo


class SIPInfoSentEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultInfoSent)            # type: SIPResultInfoSent


class SIPMessageEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    call_id = StringProperty(optional=True)
    result = ObjectProperty(SIPResultMessage)             # type: SIPResultMessage


class SIPMessageSentEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultMessageSent)         # type: SIPResultMessageSent


class SIPMessageDeliveryEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    call_id = StringProperty(optional=True)
    result = ObjectProperty(SIPResultMessageDelivery)         # type: SIPResultMessageSent


class SIPDTMFSentEvent(SIPPluginData):
    sip = FixedValueProperty('event')
    result = ObjectProperty(SIPResultDTMFSent)            # type: SIPResultDTMFSent


# Videoroom plugin data messages

class VideoroomCreated(VideoroomPluginData):
    videoroom = FixedValueProperty('created')
    room = IntegerProperty()
    # permanent = BooleanProperty()  # this is not available in older janus versions. not used.


class VideoroomEdited(VideoroomPluginData):
    videoroom = FixedValueProperty('edited')
    room = IntegerProperty()
    # permanent = BooleanProperty()  # this is not available in older janus versions. not used.


class VideoroomDestroyed(VideoroomPluginData):  # this comes both in the response to 'destroy' and in an event to participants still in the room when destroyed (if any)
    videoroom = FixedValueProperty('destroyed')
    room = IntegerProperty()
    # permanent = BooleanProperty(optional=True)  # this is not available in older janus versions (only present in the response, but not in the event). not used.


class VideoroomJoined(VideoroomPluginData):
    videoroom = FixedValueProperty('joined')
    room = IntegerProperty()
    description = StringProperty()
    id = IntegerProperty()
    publishers = ArrayProperty(VideoroomPublishers)  # type: VideoroomPublishers
    # private_id = IntegerProperty()  # this is not available in older janus versions. not used.


class VideoroomAttached(VideoroomPluginData):
    videoroom = FixedValueProperty('attached')
    room = IntegerProperty()
    id = IntegerProperty()
    display = StringProperty(optional=True)


class VideoroomSlowLink(VideoroomPluginData):
    videoroom = FixedValueProperty('slow_link')
    # current_bitrate = IntegerProperty()  # this is actually defined as 'current-bitrate' in JSON, so we cannot map it to an attribute name. also not used.


class VideoroomErrorEvent(VideoroomPluginData):
    __event__ = 'error'

    videoroom = FixedValueProperty('event')
    error_code = IntegerProperty()
    error = StringProperty()


class VideoroomStartedEvent(VideoroomPluginData):
    __event__ = 'started'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    started = FixedValueProperty('ok')


class VideoroomPublishersEvent(VideoroomPluginData):
    __event__ = 'publishers'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    publishers = ArrayProperty(VideoroomPublishers)  # type: VideoroomPublishers


class VideoroomConfiguredEvent(VideoroomPluginData):
    __event__ = 'configured'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    configured = FixedValueProperty('ok')


class VideoroomLeftEvent(VideoroomPluginData):
    __event__ = 'left'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    left = FixedValueProperty('ok')


class VideoroomLeavingEvent(VideoroomPluginData):
    __event__ = 'leaving'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    leaving = AbstractProperty()  # this is either a participant id or the string "ok"
    reason = StringProperty(optional=True)


class VideoroomKickedEvent(VideoroomPluginData):
    __event__ = 'kicked'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    kicked = IntegerProperty()


class VideoroomUnpublishedEvent(VideoroomPluginData):
    __event__ = 'unpublished'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    unpublished = AbstractProperty()  # this is either a participant id or the string "ok"


class VideoroomPausedEvent(VideoroomPluginData):
    __event__ = 'paused'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    paused = FixedValueProperty('ok')


class VideoroomSwitchedEvent(VideoroomPluginData):
    __event__ = 'switched'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    id = IntegerProperty()
    switched = FixedValueProperty('ok')


class VideoroomJoiningEvent(VideoroomPluginData):  # only sent if room has notify_joining == True (default is False). Can be used to monitor non-publishers.
    __event__ = 'joining'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    joining = ObjectProperty(UserId)  # type: UserId


class VideoroomDisplayEvent(VideoroomPluginData):  # participant display name change
    __event__ = 'display'

    videoroom = FixedValueProperty('event')
    id = IntegerProperty()
    display = StringProperty()


class VideoroomSubstreamEvent(VideoroomPluginData):  # simulcast substream change
    __event__ = 'substream'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    substream = IntegerProperty()


class VideoroomTemporalEvent(VideoroomPluginData):  # simulcast temporal layer change
    __event__ = 'temporal'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    temporal = IntegerProperty()


class VideoroomSpatialLayerEvent(VideoroomPluginData):  # SVC spatial layer change
    __event__ = 'spatial_layer'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    spatial_layer = IntegerProperty()


class VideoroomTemporalLayerEvent(VideoroomPluginData):  # SVC temporal layer change
    __event__ = 'temporal_layer'

    videoroom = FixedValueProperty('event')
    room = IntegerProperty()
    temporal_layer = IntegerProperty()


# Janus message to model mapping

class ProtocolError(Exception):
    pass


class PluginDataHandler(object):
    pass


class SIPDataHandler(PluginDataHandler):
    __plugin__ = 'sip'

    __classmap__ = {cls.__id__: cls for cls in subclasses(SIPPluginData) if cls.__plugin__ in cls.__properties__}

    @classmethod
    def decode(cls, data):
        try:
            sip = data[cls.__plugin__]
            if 'error' in data:
                data_key = cls.__plugin__, sip, 'error'
            elif 'result' in data:
                data_key = cls.__plugin__, sip, data['result']['event']
            else:
                data_key = cls.__plugin__, sip
        except KeyError:
            raise ProtocolError('could not get {!s} plugin data type from {!r}'.format(cls.__plugin__, data))
        try:
            data_class = cls.__classmap__[data_key]
        except KeyError:
            raise ProtocolError('unknown {!s} plugin data: {!r}'.format(cls.__plugin__, data))
        return data_class(**data)


class VideoroomDataHandler(PluginDataHandler):
    __plugin__ = 'videoroom'

    __classmap__ = {cls.__id__: cls for cls in subclasses(VideoroomPluginData) if cls.__plugin__ in cls.__properties__}
    __eventset__ = frozenset(cls.__event__ for cls in subclasses(VideoroomPluginData) if cls.__event__)

    @classmethod
    def decode(cls, data):
        try:
            data_type = data[cls.__plugin__]
        except KeyError:
            raise ProtocolError('could not get {!s} plugin data type from {!r}'.format(cls.__plugin__, data))
        if data_type == 'event':
            common_keys = list(cls.__eventset__.intersection(data))
            if len(common_keys) != 1:
                raise ProtocolError('unknown {!s} plugin event: {!r}'.format(cls.__plugin__, data))
            event_type = common_keys[0]
            data_key = cls.__plugin__, data_type, event_type
        else:
            data_key = cls.__plugin__, data_type
        try:
            data_class = cls.__classmap__[data_key]
        except KeyError:
            raise ProtocolError('unknown {!s} plugin data: {!r}'.format(cls.__plugin__, data))
        return data_class(**data)


class JanusMessage(object):
    __core_classmap__ = {cls.janus.value: cls for cls in subclasses(JanusCoreMessageBase) if 'janus' in cls.__properties__}
    __plugin_classmap__ = {cls.janus.value: cls for cls in subclasses(JanusPluginMessageBase) if 'janus' in cls.__properties__}
    __plugin_handlers__ = {handler.__plugin__: handler for handler in subclasses(PluginDataHandler)}

    @classmethod
    def from_payload(cls, payload):
        try:
            message_type = payload['janus']
        except KeyError:
            raise ProtocolError('could not get Janus message type')
        if 'plugindata' in payload:
            try:
                message_class = cls.__plugin_classmap__[message_type]
            except KeyError:
                raise ProtocolError('unknown Janus message: {!s}'.format(message_type))
            data_container = payload['plugindata']
            try:
                plugin_name = data_container['plugin'].rpartition('.')[2]
                plugin_data = data_container['data']
            except KeyError:
                raise ProtocolError('invalid plugin data: {!r}'.format(data_container))
            try:
                data_handler = cls.__plugin_handlers__[plugin_name]
            except KeyError:
                raise ProtocolError('could not find a data handler for {!r}'.format(plugin_data))
            data_container['data'] = data_handler.decode(plugin_data)
        else:
            try:
                message_class = cls.__core_classmap__[message_type]
            except KeyError:
                raise ProtocolError('unknown Janus message: {!s}'.format(message_type))
        return message_class(**payload)
