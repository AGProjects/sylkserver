
"""
Handling of RTP media streams according to RFC3550, RFC3605, RFC3581,
RFC2833 and RFC3711, RFC3489 and draft-ietf-mmusic-ice-19.
"""

from threading import RLock

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implements

from sipsimple.audio import AudioBridge, AudioDevice, IAudioPort
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import AudioTransport, PJSIPError, RTPTransport, SIPCoreError
from sipsimple.streams.rtp import RTPStreamEncryption

from sylk.applications.xmppgateway.xmpp.jingle.streams import IMediaStream, InvalidStreamError, MediaStreamRegistrar, UnknownStreamError


__all__ = 'AudioStream',


class AudioStream(object):
    __metaclass__ = MediaStreamRegistrar

    implements(IMediaStream, IAudioPort, IObserver)

    type = 'audio'
    priority = 1

    hold_supported = True

    def __init__(self):
        from sipsimple.application import SIPApplication
        self.mixer = SIPApplication.voice_audio_mixer
        self.bridge = AudioBridge(self.mixer)
        self.device = AudioDevice(self.mixer)

        self.notification_center = NotificationCenter()

        self.on_hold_by_local = False
        self.on_hold_by_remote = False
        self.direction = None
        self.state = 'NULL'

        self._transport = None
        self._hold_request = None
        self._ice_state = 'NULL'
        self._lock = RLock()
        self._rtp_transport = None

        self.session = None
        self.encryption = RTPStreamEncryption(self)

        self._srtp_encryption = None
        self._try_ice = False

        self._initialized = False
        self._done = False
        self._failure_reason = None

        self.bridge.add(self.device)

    # Audio properties
    #

    @property
    def codec(self):
        return self._transport.codec if self._transport else None

    @property
    def consumer_slot(self):
        return self._transport.slot if self._transport else None

    @property
    def producer_slot(self):
        return self._transport.slot if self._transport and not self.muted else None

    @property
    def sample_rate(self):
        return self._transport.sample_rate if self._transport else None

    @property
    def statistics(self):
        return self._transport.statistics if self._transport else None

    @property
    def muted(self):
        return self.__dict__.get('muted', False)

    @muted.setter
    def muted(self, value):
        if not isinstance(value, bool):
            raise ValueError('illegal value for muted property: %r' % (value,))
        if value == self.muted:
            return
        old_producer_slot = self.producer_slot
        self.__dict__['muted'] = value
        notification_center = NotificationCenter()
        data = NotificationData(consumer_slot_changed=False, producer_slot_changed=True, old_producer_slot=old_producer_slot, new_producer_slot=self.producer_slot)
        notification_center.post_notification('AudioPortDidChangeSlots', sender=self, data=data)

    # RTP properties
    #

    @property
    def local_rtp_address(self):
        return self._rtp_transport.local_rtp_address if self._rtp_transport else None

    @property
    def local_rtp_port(self):
        return self._rtp_transport.local_rtp_port if self._rtp_transport else None

    @property
    def remote_rtp_address(self):
        if self._ice_state == 'IN_USE':
            return self._rtp_transport.remote_rtp_address_received if self._rtp_transport else None
        else:
            return self._rtp_transport.remote_rtp_address_sdp if self._rtp_transport else None

    @property
    def remote_rtp_port(self):
        if self._ice_state == 'IN_USE':
            return self._rtp_transport.remote_rtp_port_received if self._rtp_transport else None
        else:
            return self._rtp_transport.remote_rtp_port_sdp if self._rtp_transport else None

    @property
    def local_rtp_candidate_type(self):
        return self._rtp_transport.local_rtp_candidate_type if self._rtp_transport else None

    @property
    def remote_rtp_candidate_type(self):
        return self._rtp_transport.remote_rtp_candidate_type if self._rtp_transport else None

    @property
    def ice_active(self):
        return self._ice_state == 'IN_USE'

    # Generic properties
    #

    @property
    def on_hold(self):
        return self.on_hold_by_local or self.on_hold_by_remote

    # Public methods
    #

    @classmethod
    def new_from_sdp(cls, session, remote_sdp, stream_index):
        # TODO: actually validate the SDP
        settings = SIPSimpleSettings()
        remote_stream = remote_sdp.media[stream_index]
        if remote_stream.media != 'audio':
            raise UnknownStreamError
        if remote_stream.transport not in ('RTP/AVP', 'RTP/SAVP'):
            raise InvalidStreamError('expected RTP/AVP or RTP/SAVP transport in audio stream, got %s' % remote_stream.transport)
        local_encryption_policy = 'sdes_optional'
        if local_encryption_policy == 'sdes_mandatory' and not 'crypto' in remote_stream.attributes:
            raise InvalidStreamError("SRTP/SDES is locally mandatory but it's not remotely enabled")
        if remote_stream.transport == 'RTP/SAVP' and 'crypto' in remote_stream.attributes and local_encryption_policy not in ('opportunistic', 'sdes_optional', 'sdes_mandatory'):
            raise InvalidStreamError("SRTP/SDES is remotely mandatory but it's not locally enabled")
        supported_codecs = session.account.rtp.audio_codec_list or settings.rtp.audio_codec_list
        if not any(codec for codec in remote_stream.codec_list if codec in supported_codecs):
            raise InvalidStreamError('no compatible codecs found')
        stream = cls()
        stream._incoming_remote_sdp = remote_sdp
        stream._incoming_stream_index = stream_index
        return stream

    def initialize(self, session, direction):
        with self._lock:
            if self.state != 'NULL':
                raise RuntimeError('AudioStream.initialize() may only be called in the NULL state')
            self.state = 'INITIALIZING'
            self.session = session
            local_encryption_policy = 'sdes_optional'
            if hasattr(self, '_incoming_remote_sdp') and hasattr(self, '_incoming_stream_index'):
                # ICE attributes could come at the session level or at the media level
                remote_stream = self._incoming_remote_sdp.media[self._incoming_stream_index]
                self._try_ice = (remote_stream.has_ice_attributes or self._incoming_remote_sdp.has_ice_attributes) and remote_stream.has_ice_candidates
                if 'zrtp-hash' in remote_stream.attributes:
                    incoming_stream_encryption = 'zrtp'
                elif 'crypto' in remote_stream.attributes:
                    incoming_stream_encryption = 'sdes_mandatory' if remote_stream.transport == 'RTP/SAVP' else 'sdes_optional'
                else:
                    incoming_stream_encryption = None
                if incoming_stream_encryption is not None and local_encryption_policy == 'opportunistic':
                    self._srtp_encryption = incoming_stream_encryption
                else:
                    self._srtp_encryption = 'zrtp' if local_encryption_policy == 'opportunistic' else local_encryption_policy
            else:
                self._try_ice = True
                self._srtp_encryption = 'zrtp' if local_encryption_policy == 'opportunistic' else local_encryption_policy

            self._init_rtp_transport()

    def get_local_media(self, remote_sdp=None, index=0):
        with self._lock:
            if self.state not in ['INITIALIZED', 'WAIT_ICE', 'ESTABLISHED']:
                raise RuntimeError('AudioStream.get_local_media() may only be called in the INITIALIZED, WAIT_ICE  or ESTABLISHED states')
            if remote_sdp is None:
                # offer
                old_direction = self._transport.direction
                if old_direction is None:
                    new_direction = 'sendrecv'
                elif 'send' in old_direction:
                    new_direction = ('sendonly' if (self._hold_request == 'hold' or (self._hold_request is None and self.on_hold_by_local)) else 'sendrecv')
                else:
                    new_direction = ('inactive' if (self._hold_request == 'hold' or (self._hold_request is None and self.on_hold_by_local)) else 'recvonly')
            else:
                new_direction = None
            return self._transport.get_local_media(remote_sdp, index, new_direction)

    def start(self, local_sdp, remote_sdp, stream_index):
        with self._lock:
            if self.state != 'INITIALIZED':
                raise RuntimeError('AudioStream.start() may only be called in the INITIALIZED state')
            settings = SIPSimpleSettings()
            self._transport.start(local_sdp, remote_sdp, stream_index, timeout=settings.rtp.timeout)
            self._check_hold(self._transport.direction, True)
            if self._try_ice:
                self.state = 'WAIT_ICE'
            else:
                self.state = 'ESTABLISHED'
                self.notification_center.post_notification('MediaStreamDidStart', sender=self)

    def validate_update(self, remote_sdp, stream_index):
        with self._lock:
            # TODO: implement
            return True

    def update(self, local_sdp, remote_sdp, stream_index):
        with self._lock:
            connection = remote_sdp.media[stream_index].connection or remote_sdp.connection
            if not self._rtp_transport.ice_active and (connection.address != self._rtp_transport.remote_rtp_address_sdp or self._rtp_transport.remote_rtp_port_sdp != remote_sdp.media[stream_index].port):
                settings = SIPSimpleSettings()
                old_consumer_slot = self.consumer_slot
                old_producer_slot = self.producer_slot
                self.notification_center.remove_observer(self, sender=self._transport)
                self._transport.stop()
                try:
                    self._transport = AudioTransport(self.mixer, self._rtp_transport, remote_sdp, stream_index, codecs=list(self.session.account.rtp.audio_codec_list or settings.rtp.audio_codec_list))
                except SIPCoreError as e:
                    self.state = 'ENDED'
                    self._failure_reason = e.args[0]
                    self.notification_center.post_notification('MediaStreamDidFail', sender=self, data=NotificationData(reason=self._failure_reason))
                    return
                self.notification_center.add_observer(self, sender=self._transport)
                self._transport.start(local_sdp, remote_sdp, stream_index, timeout=settings.rtp.timeout)
                self.notification_center.post_notification('AudioPortDidChangeSlots', sender=self, data=NotificationData(consumer_slot_changed=True, producer_slot_changed=True,
                                                                                                                         old_consumer_slot=old_consumer_slot, new_consumer_slot=self.consumer_slot,
                                                                                                                         old_producer_slot=old_producer_slot, new_producer_slot=self.producer_slot))
                if connection.address == '0.0.0.0' and remote_sdp.media[stream_index].direction == 'sendrecv':
                    self._transport.update_direction('recvonly')
                self._check_hold(self._transport.direction, False)
                self.notification_center.post_notification('RTPStreamDidChangeRTPParameters', sender=self)
            else:
                new_direction = local_sdp.media[stream_index].direction
                self._transport.update_direction(new_direction)
                self._check_hold(new_direction, False)
            self._hold_request = None

    def hold(self):
        with self._lock:
            if self.on_hold_by_local or self._hold_request == 'hold':
                return
            if self.state == 'ESTABLISHED' and self.direction != 'inactive':
                self.bridge.remove(self)
            self._hold_request = 'hold'

    def unhold(self):
        with self._lock:
            if (not self.on_hold_by_local and self._hold_request != 'hold') or self._hold_request == 'unhold':
                return
            if self.state == 'ESTABLISHED' and self._hold_request == 'hold':
                self.bridge.add(self)
            self._hold_request = None if self._hold_request == 'hold' else 'unhold'

    def deactivate(self):
        with self._lock:
            self.bridge.stop()

    def end(self):
        with self._lock:
            if not self._initialized or self._done:
                return
            self._done = True
            self.notification_center.post_notification('MediaStreamWillEnd', sender=self)
            if self._transport is not None:
                self._transport.stop()
                self.notification_center.remove_observer(self, sender=self._transport)
                self._transport = None
                self.notification_center.remove_observer(self, sender=self._rtp_transport)
                self._rtp_transport = None
            self.state = 'ENDED'
            self.notification_center.post_notification('MediaStreamDidEnd', sender=self, data=NotificationData(error=self._failure_reason))
            self.session = None

    def reset(self, stream_index):
        with self._lock:
            if self.direction == 'inactive' and not self.on_hold_by_local:
                new_direction = 'sendrecv'
                self._transport.update_direction(new_direction)
                self._check_hold(new_direction, False)
                # TODO: do a full reset, re-creating the AudioTransport, so that a new offer
                # would contain all codecs and ICE would be renegotiated -Saul

    def send_dtmf(self, digit):
        with self._lock:
            if self.state != 'ESTABLISHED':
                raise RuntimeError('AudioStream.send_dtmf() cannot be used in %s state' % self.state)
            try:
                self._transport.send_dtmf(digit)
            except PJSIPError as e:
                if not e.args[0].endswith('(PJ_ETOOMANY)'):
                    raise

    # Notification handling
    #

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_RTPTransportDidFail(self, notification):
        with self._lock:
            self.notification_center.remove_observer(self, sender=notification.sender)
            if self.state == 'ENDED':
                return
            self._try_next_rtp_transport(notification.data.reason)

    def _NH_RTPTransportDidInitialize(self, notification):
        settings = SIPSimpleSettings()
        rtp_transport = notification.sender
        with self._lock:
            if self.state == 'ENDED':
                return
            del self._rtp_args
            del self._stun_servers
            try:
                if hasattr(self, '_incoming_remote_sdp') and hasattr(self, '_incoming_stream_index'):
                    try:
                        audio_transport = AudioTransport(self.mixer, rtp_transport, self._incoming_remote_sdp, self._incoming_stream_index,
                                                         codecs=list(self.session.account.rtp.audio_codec_list or settings.rtp.audio_codec_list))
                    finally:
                        del self._incoming_remote_sdp
                        del self._incoming_stream_index
                else:
                    audio_transport = AudioTransport(self.mixer, rtp_transport, codecs=list(self.session.account.rtp.audio_codec_list or settings.rtp.audio_codec_list))
            except SIPCoreError as e:
                self.state = "ENDED"
                self.notification_center.post_notification('MediaStreamDidNotInitialize', sender=self, data=NotificationData(reason=e.args[0]))
                return
            self._rtp_transport = rtp_transport
            self._transport = audio_transport
            self.notification_center.add_observer(self, sender=audio_transport)
            self._initialized = True
            self.state = 'INITIALIZED'
            self.notification_center.post_notification('MediaStreamDidInitialize', sender=self)

    def _NH_RTPAudioStreamGotDTMF(self, notification):
        self.notification_center.post_notification('AudioStreamGotDTMF', sender=self, data=NotificationData(digit=notification.data.digit))

    def _NH_RTPAudioTransportDidTimeout(self, notification):
        self.notification_center.post_notification('RTPStreamDidTimeout', sender=self)

    def _NH_RTPTransportICENegotiationStateDidChange(self, notification):
        with self._lock:
            if self._ice_state != 'NULL' or self.state not in ('INITIALIZING', 'INITIALIZED', 'WAIT_ICE'):
                return
        self.notification_center.post_notification('RTPStreamICENegotiationStateDidChange', sender=self, data=notification.data)

    def _NH_RTPTransportICENegotiationDidSucceed(self, notification):
        with self._lock:
            if self.state != 'WAIT_ICE':
                return
            self._ice_state = 'IN_USE'
            self.state = 'ESTABLISHED'
        self.notification_center.post_notification('RTPStreamICENegotiationDidSucceed', sender=self, data=notification.data)
        self.notification_center.post_notification('MediaStreamDidStart', sender=self)

    def _NH_RTPTransportICENegotiationDidFail(self, notification):
        with self._lock:
            if self.state != 'WAIT_ICE':
                return
            self._ice_state = 'FAILED'
            self.state = 'ESTABLISHED'
        self.notification_center.post_notification('RTPStreamICENegotiationDidFail', sender=self, data=notification.data)
        self.notification_center.post_notification('MediaStreamDidStart', sender=self)

    # Private methods
    #

    def _init_rtp_transport(self, stun_servers=None):
        self._rtp_args = dict()
        self._rtp_args['encryption'] = self._srtp_encryption
        self._rtp_args['use_ice'] = self._try_ice
        self._stun_servers = [(None, None)]
        if stun_servers:
            self._stun_servers.extend(reversed(stun_servers))
        self._try_next_rtp_transport()

    def _try_next_rtp_transport(self, failure_reason=None):
        if self._stun_servers:
            stun_address, stun_port = self._stun_servers.pop()
            try:
                rtp_transport = RTPTransport(ice_stun_address=stun_address, ice_stun_port=stun_port, **self._rtp_args)
            except SIPCoreError as e:
                self._try_next_rtp_transport(e.args[0])
            else:
                self.notification_center.add_observer(self, sender=rtp_transport)
                try:
                    rtp_transport.set_INIT()
                except SIPCoreError as e:
                    self.notification_center.remove_observer(self, sender=rtp_transport)
                    self._try_next_rtp_transport(e.args[0])
        else:
            self.state = 'ENDED'
            self.notification_center.post_notification('MediaStreamDidNotInitialize', sender=self, data=NotificationData(reason=failure_reason))

    def _check_hold(self, direction, is_initial):
        was_on_hold_by_local = self.on_hold_by_local
        was_on_hold_by_remote = self.on_hold_by_remote
        was_inactive = self.direction == 'inactive'
        self.direction = direction
        inactive = self.direction == 'inactive'
        self.on_hold_by_local = was_on_hold_by_local if inactive else direction == 'sendonly'
        self.on_hold_by_remote = 'send' not in direction
        if (is_initial or was_on_hold_by_local or was_inactive) and not inactive and not self.on_hold_by_local and self._hold_request != 'hold':
            self.bridge.add(self)
        if not was_on_hold_by_local and self.on_hold_by_local:
            self.notification_center.post_notification('RTPStreamDidChangeHoldState', sender=self, data=NotificationData(originator='local', on_hold=True))
        if was_on_hold_by_local and not self.on_hold_by_local:
            self.notification_center.post_notification('RTPStreamDidChangeHoldState', sender=self, data=NotificationData(originator='local', on_hold=False))
        if not was_on_hold_by_remote and self.on_hold_by_remote:
            self.notification_center.post_notification('RTPStreamDidChangeHoldState', sender=self, data=NotificationData(originator='remote', on_hold=True))
        if was_on_hold_by_remote and not self.on_hold_by_remote:
            self.notification_center.post_notification('RTPStreamDidChangeHoldState', sender=self, data=NotificationData(originator='remote', on_hold=False))
