
import random
import string

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from cStringIO import StringIO
from datetime import datetime
from eventlib import api, coros, proc
from eventlib.twistedutil import block_on
from lxml import etree
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SDPSession, SDPMediaStream, SDPConnection, SDPNegotiator
from sipsimple.core import SIPCoreError
from sipsimple.threading import run_in_twisted_thread
from twisted.internet import reactor
from twisted.words.protocols.jabber.error import StanzaError
from twisted.words.protocols.jabber.xmlstream import TimeoutError as IqTimeoutError
from zope.interface import implements

from sylk.accounts import DefaultAccount
from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI
from sylk.applications.xmppgateway.xmpp.jingle.streams import MediaStreamRegistry, InvalidStreamError, UnknownStreamError
from sylk.applications.xmppgateway.xmpp.jingle.util import jingle_to_sdp, sdp_to_jingle
from sylk.applications.xmppgateway.xmpp.stanzas import jingle
from sylk.configuration import SIPConfig


def random_id():
    return ''.join(random.choice(string.ascii_letters+string.digits) for x in xrange(32))


class MediaStreamDidFailError(Exception):
    def __init__(self, stream, data):
        self.stream = stream
        self.data = data


class MediaStreamDidNotInitializeError(Exception):
    def __init__(self, stream, data):
        self.stream = stream
        self.data = data


class Operation(object):
    __params__ = ()

    def __init__(self, **params):
        for name, value in params.iteritems():
            setattr(self, name, value)
        for param in set(self.__params__).difference(params):
            raise ValueError("missing operation parameter: '%s'" % param)
        self.channel = coros.queue()


class AcceptOperation(Operation):
    __params__ = ('streams', 'is_focus')


class SendRingIndicationOperation(Operation):
    __params__ = ()


class RejectOperation(Operation):
    __params__ = ('reason',)


class EndOperation(Operation):
    __params__ = ()


class HoldOperation(Operation):
    __params__ = ()


class UnholdOperation(Operation):
    __params__ = ()


class ProcessRemoteOperation(Operation):
    __params__ = ('notification',)


class ConnectOperation(Operation):
    __params__ = ('sender', 'recipient', 'streams', 'is_focus')


class SendConferenceInfoOperation(Operation):
    __params__ = ('xml',)


class JingleSession(object):
    implements(IObserver)

    jingle_stanza_timeout = 3
    media_stream_timeout = 15

    def __init__(self, protocol):
        self.account = DefaultAccount()
        self._protocol = protocol

        self._id = None
        self._local_identity = None
        self._remote_identity = None
        self._local_jid = None
        self._remote_jid = None

        self._channel = coros.queue()
        self._current_operation = None
        self._proc = proc.spawn(self._run)
        self._timer = None

        self._sdp_negotiator = None
        self._pending_transport_info_stanzas = []

        self.direction = None
        self.state = None
        self.streams = None
        self.proposed_streams = None
        self.start_time = None
        self.end_time = None
        self.on_hold = False
        self.local_focus = False
        self.candidates = set()

    def init_incoming(self, stanza):
        self._id = stanza.jingle.sid
        self._local_identity = Identity(FrozenURI.parse(stanza.recipient))
        self._remote_identity = Identity(FrozenURI.parse(stanza.sender))
        self._local_jid = self._local_identity.uri.as_xmpp_jid()
        self._remote_jid = self._remote_identity.uri.as_xmpp_jid()

        remote_sdp = jingle_to_sdp(stanza.jingle)
        try:
            self._sdp_negotiator = SDPNegotiator.create_with_remote_offer(remote_sdp)
        except SIPCoreError as e:
            self._fail(originator='local', reason='general-error', description=str(e))
            return

        self.proposed_streams = []
        for index, media_stream in enumerate(remote_sdp.media):
            if media_stream.port != 0:
                for stream_type in MediaStreamRegistry:
                    try:
                        stream = stream_type.new_from_sdp(self, remote_sdp, index)
                    except InvalidStreamError:
                        break
                    except UnknownStreamError:
                        continue
                    else:
                        stream.index = index
                        self.proposed_streams.append(stream)
                        break

        if self.proposed_streams:
            self.direction = 'incoming'
            self.state = 'incoming'
            NotificationCenter().post_notification('JingleSessionNewIncoming', sender=self, data=NotificationData(streams=self.proposed_streams))
        else:
            self._fail(originator='local', reason='unsupported-applications')

    def connect(self, sender_identity, recipient_identity, streams, is_focus=False):
        self._schedule_operation(ConnectOperation(sender=sender_identity, recipient=recipient_identity, streams=streams, is_focus=is_focus))

    def send_ring_indication(self):
        self._schedule_operation(SendRingIndicationOperation())

    def accept(self, streams, is_focus=False):
        self._schedule_operation(AcceptOperation(streams=streams, is_focus=is_focus))

    def reject(self, reason='busy'):
        self._schedule_operation(RejectOperation(reason=reason))

    def hold(self):
        self._schedule_operation(HoldOperation())

    def unhold(self):
        self._schedule_operation(UnholdOperation())

    def end(self):
        self._schedule_operation(EndOperation())

    def add_stream(self):
        raise NotImplementedError

    def remove_stream(self):
        raise NotImplementedError

    @property
    def id(self):
        return self._id

    @property
    def local_identity(self):
        return self._local_identity

    @property
    def remote_identity(self):
        return self._remote_identity

    @run_in_twisted_thread
    def _send_conference_info(self, xml):
        # This function is not meant for users to call, entities with knowledge about JingleSession
        # internals will call it, such as the MediaSessionHandler
        self._schedule_operation(SendConferenceInfoOperation(xml=xml))

    def _send_stanza(self, stanza):
        if self.direction == 'incoming':
            stanza.jingle.initiator = unicode(self._remote_jid)
            stanza.jingle.responder = unicode(self._local_jid)
        else:
            stanza.jingle.initiator = unicode(self._local_jid)
            stanza.jingle.responder = unicode(self._remote_jid)
        stanza.timeout = self.jingle_stanza_timeout
        return self._protocol.request(stanza)

    def _fail(self, originator='local', reason='general-error', description=None):
        reason = jingle.Reason(jingle.ReasonType(reason), text=description)
        stanza = self._protocol.sessionTerminate(self._local_jid, self._remote_jid, self._id, reason)
        self._send_stanza(stanza)
        self.state = 'terminated'
        failure_str = '%s%s' % (reason, ' %s' % description if description else '')
        NotificationCenter().post_notification('JingleSessionDidFail', sender=self, data=NotificationData(originator='local', reason=failure_str))
        self._channel.send_exception(proc.ProcExit)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_RTPStreamDidEnableEncryption(self, notification):
        if notification.sender.type != 'audio':
            return
        audio_stream = notification.sender
        if audio_stream.encryption.type == 'ZRTP':
            # start ZRTP on the video stream, if applicable
            try:
                video_stream = next(stream for stream in self.streams or [] if stream.type=='video')
            except StopIteration:
                return
            if video_stream.encryption.type == 'ZRTP' and not video_stream.encryption.active:
                video_stream.encryption.zrtp._enable(audio_stream)

    def _NH_MediaStreamDidStart(self, notification):
        stream = notification.sender
        if stream.type == 'audio' and stream.encryption.type == 'ZRTP':
            stream.encryption.zrtp._enable()
        elif stream.type == 'video' and stream.encryption.type == 'ZRTP':
            # start ZRTP on the video stream, if applicable
            try:
                audio_stream = next(stream for stream in self.streams or [] if stream.type=='audio')
            except StopIteration:
                pass
            else:
                if audio_stream.encryption.type == 'ZRTP' and audio_stream.encryption.active:
                    stream.encryption.zrtp._enable(audio_stream)
        if self._current_operation is not None:
            self._current_operation.channel.send(notification)

    def _NH_MediaStreamDidInitialize(self, notification):
        if self._current_operation is not None:
            self._current_operation.channel.send(notification)

    def _NH_MediaStreamDidNotInitialize(self, notification):
        if self._current_operation is not None:
            self._current_operation.channel.send_exception(MediaStreamDidNotInitializeError(notification.sender, notification.data))

    def _NH_MediaStreamDidFail(self, notification):
        if self._current_operation is not None:
            self._current_operation.channel.send_exception(MediaStreamDidFailError(notification.sender, notification.data))
        else:
            self.end()

    def _NH_XMPPGotJingleSessionAccept(self, notification):
        self._schedule_operation(ProcessRemoteOperation(notification=notification))

    def _NH_XMPPGotJingleSessionTerminate(self, notification):
        self._schedule_operation(ProcessRemoteOperation(notification=notification))

    def _NH_XMPPGotJingleSessionInfo(self, notification):
        self._schedule_operation(ProcessRemoteOperation(notification=notification))

    def _NH_XMPPGotJingleDescriptionInfo(self, notification):
        self._schedule_operation(ProcessRemoteOperation(notification=notification))

    def _NH_XMPPGotJingleTransportInfo(self, notification):
        self._schedule_operation(ProcessRemoteOperation(notification=notification))

    # Operation handling

    @run_in_twisted_thread
    def _schedule_operation(self, operation):
        self._channel.send(operation)

    def _run(self):
        while True:
            self._current_operation = op = self._channel.wait()
            try:
                handler = getattr(self, '_OH_%s' % op.__class__.__name__)
                handler(op)
            except BaseException:
                self._proc = None
                raise
            finally:
                self._current_operation = None

    def _OH_AcceptOperation(self, operation):
        if self.state != 'incoming':
            return

        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        streams = operation.streams

        for stream in self.proposed_streams:
            if stream in streams:
                notification_center.add_observer(self, sender=stream)
                stream.initialize(self, direction='incoming')

        try:
            wait_count = len(self.proposed_streams)
            while wait_count > 0:
                notification = operation.channel.wait()
                if notification.name == 'MediaStreamDidInitialize':
                    wait_count -= 1

            remote_sdp = self._sdp_negotiator.current_remote
            local_ip = SIPConfig.local_ip.normalized
            local_sdp = SDPSession(local_ip, connection=SDPConnection(local_ip), name=settings.user_agent)
            stream_map = dict((stream.index, stream) for stream in self.proposed_streams)
            for index, media in enumerate(remote_sdp.media):
                stream = stream_map.get(index, None)
                if stream is not None:
                    media = stream.get_local_media(remote_sdp=remote_sdp, index=index)
                else:
                    media = SDPMediaStream.new(media)
                    media.port = 0
                    media.attributes = []
                local_sdp.media.append(media)
            try:
                self._sdp_negotiator.set_local_answer(local_sdp)
                self._sdp_negotiator.negotiate()
            except SIPCoreError as e:
                self._fail(originator='local', reason='incompatible-parameters', description=str(e))
                return

            self.local_focus = operation.is_focus

            notification_center.post_notification('JingleSessionWillStart', sender=self)

            # Get active SDPs (negotiator may make changes)
            local_sdp = self._sdp_negotiator.active_local
            remote_sdp = self._sdp_negotiator.active_remote

            # Build the payload and send it over
            payload = sdp_to_jingle(local_sdp)
            payload.sid = self._id
            if self.local_focus:
                payload.conference_info = jingle.ConferenceInfo(True)
            stanza = self._protocol.sessionAccept(self._local_jid, self._remote_jid, payload)
            d = self._send_stanza(stanza)
            block_on(d)

            wait_count = 0
            stream_map = dict((stream.index, stream) for stream in self.proposed_streams)
            for index, local_media in enumerate(local_sdp.media):
                remote_media = remote_sdp.media[index]
                stream = stream_map.get(index, None)
                if stream is not None:
                    if remote_media.port:
                        wait_count += 1
                        stream.start(local_sdp, remote_sdp, index)
                    else:
                        notification_center.remove_observer(self, sender=stream)
                        self.proposed_streams.remove(stream)
                        del stream_map[stream.index]
                        stream.deactivate()
                        stream.end()
            removed_streams = [stream for stream in self.proposed_streams if stream.index >= len(local_sdp.media)]
            for stream in removed_streams:
                notification_center.remove_observer(self, sender=stream)
                self.proposed_streams.remove(stream)
                del stream_map[stream.index]
                stream.deactivate()
                stream.end()
            with api.timeout(self.media_stream_timeout):
                while wait_count > 0:
                    notification = operation.channel.wait()
                    if notification.name == 'MediaStreamDidStart':
                        wait_count -= 1
        except (MediaStreamDidNotInitializeError, MediaStreamDidFailError, api.TimeoutError, IqTimeoutError, StanzaError) as e:
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            if isinstance(e, api.TimeoutError):
                error = 'media stream timed out while starting'
            elif isinstance(e, IqTimeoutError):
                error = 'timeout sending IQ stanza'
            elif isinstance(e, StanzaError):
                error = str(e.condition)
            else:
                error = 'media stream failed: %s' % e.data.reason
            self._fail(originator='local', reason='failed-application', description=error)
        else:
            self.state = 'connected'
            self.streams = self.proposed_streams
            self.proposed_streams = None
            self.start_time = datetime.now()
            notification_center.post_notification('JingleSessionDidStart', self, NotificationData(streams=self.streams))

    def _OH_ConnectOperation(self, operation):
        if self.state is not None:
            return

        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()

        self.direction = 'outgoing'
        self.state = 'connecting'
        self.proposed_streams = operation.streams
        self.local_focus = operation.is_focus
        self._id = random_id()
        self._local_identity = operation.sender
        self._remote_identity = operation.recipient
        self._local_jid = self._local_identity.uri.as_xmpp_jid()
        self._remote_jid = self._remote_identity.uri.as_xmpp_jid()

        notification_center.post_notification('JingleSessionNewOutgoing', self, NotificationData(streams=operation.streams))

        for stream in self.proposed_streams:
            notification_center.add_observer(self, sender=stream)
            stream.initialize(self, direction='outgoing')

        try:
            wait_count = len(self.proposed_streams)
            while wait_count > 0:
                notification = operation.channel.wait()
                if notification.name == 'MediaStreamDidInitialize':
                    wait_count -= 1
            # Build local SDP and negotiator
            local_ip = SIPConfig.local_ip.normalized
            local_sdp = SDPSession(local_ip, connection=SDPConnection(local_ip), name=settings.user_agent)
            for index, stream in enumerate(self.proposed_streams):
                stream.index = index
                media = stream.get_local_media(remote_sdp=None, index=index)
                local_sdp.media.append(media)
            self._sdp_negotiator = SDPNegotiator.create_with_local_offer(local_sdp)
            # Build the payload and send it over
            payload = sdp_to_jingle(local_sdp)
            payload.sid = self._id
            if self.local_focus:
                payload.conference_info = jingle.ConferenceInfo(True)
            stanza = self._protocol.sessionInitiate(self._local_jid, self._remote_jid, payload)
            d = self._send_stanza(stanza)
            block_on(d)
        except (MediaStreamDidNotInitializeError, MediaStreamDidFailError, IqTimeoutError, StanzaError, SIPCoreError) as e:
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            if isinstance(e, IqTimeoutError):
                error = 'timeout sending IQ stanza'
            elif isinstance(e, StanzaError):
                error = str(e.condition)
            elif isinstance(e, SIPCoreError):
                error = str(e)
            else:
                error = 'media stream failed: %s' % e.data.reason
            self.state = 'terminated'
            NotificationCenter().post_notification('JingleSessionDidFail', sender=self, data=NotificationData(originator='local', reason=error))
            self._channel.send_exception(proc.ProcExit)
        else:
            self._timer = reactor.callLater(settings.sip.invite_timeout, self.end)

    def _OH_RejectOperation(self, operation):
        if self.state != 'incoming':
            return
        reason = jingle.Reason(jingle.ReasonType(operation.reason))
        stanza = self._protocol.sessionTerminate(self._local_jid, self._remote_jid, self._id, reason)
        self._send_stanza(stanza)
        self.state = 'terminated'
        self._channel.send_exception(proc.ProcExit)

    def _OH_EndOperation(self, operation):
        if self.state not in ('connecting', 'connected'):
            return

        if self._timer is not None and self._timer.active():
            self._timer.cancel()
        self._timer = None

        prev_state = self.state
        self.state = 'terminating'
        notification_center = NotificationCenter()
        notification_center.post_notification('JingleSessionWillEnd', self)

        streams = (self.streams or []) + (self.proposed_streams or [])
        for stream in streams[:]:
            try:
                notification_center.remove_observer(self, sender=stream)
            except KeyError:
                streams.remove(stream)
            else:
                stream.deactivate()

        if prev_state == 'connected':
            reason = jingle.Reason(jingle.ReasonType('success'))
        else:
            reason = jingle.Reason(jingle.ReasonType('cancel'))
        stanza = self._protocol.sessionTerminate(self._local_jid, self._remote_jid, self._id, reason)
        self._send_stanza(stanza)

        self.state = 'terminated'
        if prev_state == 'connected':
            self.end_time = datetime.now()
            notification_center.post_notification('JingleSessionDidEnd', self, NotificationData(originator='local'))
        else:
            notification_center.post_notification('JingleSessionDidFail', self, NotificationData(originator='local', reason='cancel'))

        for stream in streams:
            stream.end()
        self._channel.send_exception(proc.ProcExit)

    def _OH_SendRingIndicationOperation(self, operation):
        if self.state != 'incoming':
            return
        stanza = self._protocol.sessionInfo(self._local_jid, self._remote_jid, self._id, jingle.Info('ringing'))
        self._send_stanza(stanza)

    def _OH_HoldOperation(self, operation):
        if self.state != 'connected':
            return
        if self.on_hold:
            return
        self.on_hold = True
        for stream in self.streams:
            stream.hold()
        stanza = self._protocol.sessionInfo(self._local_jid, self._remote_jid, self._id, jingle.Info('hold'))
        self._send_stanza(stanza)
        NotificationCenter().post_notification('JingleSessionDidChangeHoldState', self, NotificationData(originator='local', on_hold=True, partial=False))

    def _OH_UnholdOperation(self, operation):
        if self.state != 'connected':
            return
        if not self.on_hold:
            return
        self.on_hold = False
        for stream in self.streams:
            stream.unhold()
        stanza = self._protocol.sessionInfo(self._local_jid, self._remote_jid, self._id, jingle.Info('unhold'))
        self._send_stanza(stanza)
        NotificationCenter().post_notification('JingleSessionDidChangeHoldState', self, NotificationData(originator='local', on_hold=False, partial=False))

    def _OH_SendConferenceInfoOperation(self, operation):
        if self.state != 'connected':
            return
        if not self.local_focus:
            return
        tree = etree.parse(StringIO(operation.xml))
        tree.getroot().attrib['sid'] = self._id             # FIXME: non-standard, but Jitsi does it
        data = etree.tostring(tree, xml_declaration=False)  # Strip the XML heading
        stanza = jingle.ConferenceInfoIq(sender=self._local_jid, recipient=self._remote_jid, payload=data)
        stanza.timeout = self.jingle_stanza_timeout
        self._protocol.request(stanza)

    def _OH_ProcessRemoteOperation(self, operation):
        notification = operation.notification
        stanza = notification.data.stanza
        if notification.name == 'XMPPGotJingleSessionTerminate':
            if self.state not in ('incoming', 'connecting', 'connected_pending_accept', 'connected'):
                return
            if self._timer is not None and self._timer.active():
                self._timer.cancel()
            self._timer = None
            # Session ended remotely
            prev_state = self.state
            self.state = 'terminated'
            if prev_state == 'incoming':
                reason = stanza.jingle.reason.value if stanza.jingle.reason else 'cancel'
                notification.center.post_notification('JingleSessionDidFail', self, NotificationData(originator='remote', reason=reason))
            else:
                notification.center.post_notification('JingleSessionWillEnd', self, NotificationData(originator='remote'))
                streams = self.proposed_streams if prev_state == 'connecting' else self.streams
                for stream in streams:
                    notification.center.remove_observer(self, sender=stream)
                    stream.deactivate()
                    stream.end()
                self.end_time = datetime.now()
                notification.center.post_notification('JingleSessionDidEnd', self, NotificationData(originator='remote'))
            self._channel.send_exception(proc.ProcExit)
        elif notification.name == 'XMPPGotJingleSessionInfo':
            info = stanza.jingle.info
            if not info:
                return
            if info == 'ringing':
                if self.state not in ('connecting', 'connected_pending_accept'):
                    return
                notification.center.post_notification('JingleSessionGotRingIndication', self)
            elif info in ('hold', 'unhold'):
                if self.state != 'connected':
                    return
                notification.center.post_notification('JingleSessionDidChangeHoldState', self, NotificationData(originator='remote', on_hold=info=='hold', partial=False))
        elif notification.name == 'XMPPGotJingleDescriptionInfo':
            if self.state != 'connecting':
                return

            # Add candidates acquired on transport-info stanzas
            for s in self._pending_transport_info_stanzas:
                for c in s.jingle.content:
                    content = next(content for content in stanza.jingle.content if content.name == c.name)
                    content.transport.candidates.extend(c.transport.candidates)
                    if isinstance(content.transport, jingle.IceUdpTransport):
                        if not content.transport.ufrag and c.transport.ufrag:
                            content.transport.ufrag = c.transport.ufrag
                        if not content.transport.password and c.transport.password:
                            content.transport.password = c.transport.password

            remote_sdp = jingle_to_sdp(stanza.jingle)
            try:
                self._sdp_negotiator.set_remote_answer(remote_sdp)
                self._sdp_negotiator.negotiate()
            except SIPCoreError:
                # The description-info stanza may have been just a parameter change, not a full 'SDP'
                return

            if self._timer is not None and self._timer.active():
                self._timer.cancel()
            self._timer = None

            del self._pending_transport_info_stanzas[:]

            # Get active SDPs (negotiator may make changes)
            local_sdp = self._sdp_negotiator.active_local
            remote_sdp = self._sdp_negotiator.active_remote

            notification.center.post_notification('JingleSessionWillStart', sender=self)
            stream_map = dict((stream.index, stream) for stream in self.proposed_streams)
            for index, local_media in enumerate(local_sdp.media):
                remote_media = remote_sdp.media[index]
                stream = stream_map[index]
                if remote_media.port:
                    stream.start(local_sdp, remote_sdp, index)
                else:
                    notification.center.remove_observer(self, sender=stream)
                    self.proposed_streams.remove(stream)
                    del stream_map[stream.index]
                    stream.deactivate()
                    stream.end()
            removed_streams = [stream for stream in self.proposed_streams if stream.index >= len(local_sdp.media)]
            for stream in removed_streams:
                notification.center.remove_observer(self, sender=stream)
                self.proposed_streams.remove(stream)
                del stream_map[stream.index]
                stream.deactivate()
                stream.end()

            try:
                with api.timeout(self.media_stream_timeout):
                    wait_count = len(self.proposed_streams)
                    while wait_count > 0:
                        notification = operation.channel.wait()
                        if notification.name == 'MediaStreamDidStart':
                            wait_count -= 1
            except (MediaStreamDidFailError, api.TimeoutError) as e:
                for stream in self.proposed_streams:
                    notification.center.remove_observer(self, sender=stream)
                    stream.deactivate()
                    stream.end()
                if isinstance(e, api.TimeoutError):
                    error = 'media stream timed out while starting'
                else:
                    error = 'media stream failed: %s' % e.data.reason
                self._fail(originator='local', reason='failed-application', description=error)
            else:
                self.state = 'connected_pending_accept'
                self.streams = self.proposed_streams
                self.proposed_streams = None
                self.start_time = datetime.now()
                # Hold the streams to prevent real RTP from flowing
                for stream in self.streams:
                    stream.hold()
        elif notification.name == 'XMPPGotJingleSessionAccept':
            if self.state not in ('connecting', 'connected_pending_accept'):
                return
            if self._timer is not None and self._timer.active():
                self._timer.cancel()
            self._timer = None

            if self.state == 'connected_pending_accept':
                # We already negotiated ICE and media is 'flowing' (not really because streams are on hold)
                # unhold the streams and pretend the session just started
                for stream in self.streams:
                    stream.unhold()
                self.state = 'connected'
                notification.center.post_notification('JingleSessionDidStart', self, NotificationData(streams=self.streams))
                return

            # Add candidates acquired on transport-info stanzas
            for s in self._pending_transport_info_stanzas:
                for c in s.jingle.content:
                    content = next(content for content in stanza.jingle.content if content.name == c.name)
                    content.transport.candidates.extend(c.transport.candidates)
                    if isinstance(content.transport, jingle.IceUdpTransport):
                        if not content.transport.ufrag and c.transport.ufrag:
                            content.transport.ufrag = c.transport.ufrag
                        if not content.transport.password and c.transport.password:
                            content.transport.password = c.transport.password
            del self._pending_transport_info_stanzas[:]

            remote_sdp = jingle_to_sdp(stanza.jingle)
            try:
                self._sdp_negotiator.set_remote_answer(remote_sdp)
                self._sdp_negotiator.negotiate()
            except SIPCoreError as e:
                for stream in self.proposed_streams:
                    notification.center.remove_observer(self, sender=stream)
                    stream.deactivate()
                    stream.end()
                self._fail(originator='remote', reason='incompatible-parameters', description=str(e))
                return

            # Get active SDPs (negotiator may make changes)
            local_sdp = self._sdp_negotiator.active_local
            remote_sdp = self._sdp_negotiator.active_remote

            notification.center.post_notification('JingleSessionWillStart', sender=self)
            stream_map = dict((stream.index, stream) for stream in self.proposed_streams)
            for index, local_media in enumerate(local_sdp.media):
                remote_media = remote_sdp.media[index]
                stream = stream_map[index]
                if remote_media.port:
                    stream.start(local_sdp, remote_sdp, index)
                else:
                    notification.center.remove_observer(self, sender=stream)
                    self.proposed_streams.remove(stream)
                    del stream_map[stream.index]
                    stream.deactivate()
                    stream.end()
            removed_streams = [stream for stream in self.proposed_streams if stream.index >= len(local_sdp.media)]
            for stream in removed_streams:
                notification.center.remove_observer(self, sender=stream)
                self.proposed_streams.remove(stream)
                del stream_map[stream.index]
                stream.deactivate()
                stream.end()

            try:
                with api.timeout(self.media_stream_timeout):
                    wait_count = len(self.proposed_streams)
                    while wait_count > 0:
                        notification = operation.channel.wait()
                        if notification.name == 'MediaStreamDidStart':
                            wait_count -= 1
            except (MediaStreamDidFailError, api.TimeoutError) as e:
                for stream in self.proposed_streams:
                    notification.center.remove_observer(self, sender=stream)
                    stream.deactivate()
                    stream.end()
                if isinstance(e, api.TimeoutError):
                    error = 'media stream timed out while starting'
                else:
                    error = 'media stream failed: %s' % e.data.reason
                self._fail(originator='local', reason='failed-application', description=error)
            else:
                self.state = 'connected'
                self.streams = self.proposed_streams
                self.proposed_streams = None
                self.start_time = datetime.now()
                notification.center.post_notification('JingleSessionDidStart', self, NotificationData(streams=self.streams))
        elif notification.name == 'XMPPGotJingleTransportInfo':
            if self.state != 'connecting':
                # ICE trickling not supported yet, so only accept candidates before accept
                return

            for c in stanza.jingle.content:
                content = next(content for content in stanza.jingle.content if content.name == c.name)
                content.transport.candidates.extend(c.transport.candidates)
                if isinstance(content.transport, jingle.IceUdpTransport) or isinstance(content.transport, jingle.RawUdpTransport):
                    for cand in content.transport.candidates:
                        if cand.port == 0:
                            continue
                            
                        idx = "%s:%s:%s" % (cand.protocol, cand.ip, cand.port)
                        if idx in self.candidates:
                            continue
                        
                        self.candidates.add(idx)
                        self._pending_transport_info_stanzas.append(stanza)


class JingleSessionManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        self.sessions = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='JingleSessionNewIncoming')
        notification_center.add_observer(self, name='JingleSessionNewOutgoing')
        notification_center.add_observer(self, name='JingleSessionDidFail')
        notification_center.add_observer(self, name='JingleSessionDidEnd')

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='JingleSessionNewIncoming')
        notification_center.remove_observer(self, name='JingleSessionNewOutgoing')
        notification_center.remove_observer(self, name='JingleSessionDidFail')
        notification_center.remove_observer(self, name='JingleSessionDidEnd')

    def handle_notification(self, notification):
        if notification.name in ('JingleSessionNewIncoming', 'JingleSessionNewOutgoing'):
            session = notification.sender
            self.sessions[session.id] = session
        elif notification.name in ('JingleSessionDidFail', 'JingleSessionDidEnd'):
            session = notification.sender
            del self.sessions[session.id]

