# Copyright (C) 2013 AG Projects. See LICENSE for details
#

from application.notification import IObserver, NotificationCenter
from application.python import Null
from twisted.internet import reactor
from zope.interface import implements

from sylk.applications import SylkApplication, ApplicationLogger


log = ApplicationLogger.for_package(__package__)


def format_identity(identity):
    return u'%s <sip:%s@%s>' % (identity.display_name, identity.uri.user, identity.uri.host)


class EchoApplication(SylkApplication):
    implements(IObserver)

    def start(self):
        self.pending = set()
        self.sessions = set()

    def stop(self):
        self.pending.clear()
        self.sessions.clear()

    def incoming_session(self, session):
        session.call_id = session._invitation.call_id
        log.msg(u'New incoming session %s from %s' % (session.call_id, format_identity(session.remote_identity)))
        audio_streams = [stream for stream in session.proposed_streams if stream.type=='audio']
        chat_streams = [stream for stream in session.proposed_streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            log.msg(u'Session %s rejected: invalid media, only RTP audio and MSRP chat are supported' % session.call_id)
            session.reject(488)
            return
        NotificationCenter().add_observer(self, sender=session)
        if audio_streams:
            session.send_ring_indication()
        streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
        reactor.callLater(2 if audio_streams else 0, self._accept_session, session, streams)
        self.pending.add(session)

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_sip_message(self, request, data):
        request.reject(405)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _accept_session(self, session, streams):
        if session in self.pending:
            session.accept(streams)

    def _make_audio_stream_echo(self, stream):
        if stream.producer_slot is not None and stream.consumer_slot is not None:
            # TODO: handle slot changes
            stream.bridge.remove(stream.device)
            stream.mixer.connect_slots(stream.producer_slot, stream.consumer_slot)

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        self.pending.remove(session)
        self.sessions.add(session)
        try:
            audio_stream = next(stream for stream in session.streams if stream.type == 'audio')
        except StopIteration:
            audio_stream = None
        try:
            chat_stream = next(stream for stream in session.streams if stream.type == 'chat')
        except StopIteration:
            chat_stream = None
        log.msg('Session %s started' % session.call_id)
        if audio_stream is not None:
            self._make_audio_stream_echo(audio_stream)
            notification.center.add_observer(self, sender=audio_stream)
        if chat_stream is not None:
            notification.center.add_observer(self, sender=chat_stream)

    def _NH_SIPSessionDidEnd(self, notification):
        session = notification.sender
        log.msg('Session %s ended' % session.call_id)
        notification.center.remove_observer(self, sender=session)
        # We could get DidEnd even if we never got DidStart
        self.sessions.discard(session)
        self.pending.discard(session)

    def _NH_SIPSessionDidFail(self, notification):
        session = notification.sender
        log.msg('Session %s failed from %s' % session.call_id)
        self.pending.remove(session)
        notification.center.remove_observer(self, sender=session)

    def _NH_ChatStreamGotMessage(self, notification):
        stream = notification.sender
        message = notification.data.message
        content_type = message.content_type.lower()
        if content_type.startswith('text/'):
            stream.msrp_session.send_report(notification.data.chunk, 200, 'OK')
            stream.send_message(message.body, message.content_type)
        else:
            stream.msrp_session.send_report(notification.data.chunk, 413, 'Unwanted message')

    def _NH_SIPSessionGotProposal(self, notification):
        session = notification.sender
        audio_streams = [stream for stream in notification.data.streams if stream.type=='audio']
        chat_streams = [stream for stream in notification.data.streams if stream.type=='chat']
        if not audio_streams and not chat_streams:
            session.reject_proposal()
            return
        streams = [streams[0] for streams in (audio_streams, chat_streams) if streams]
        session.accept_proposal(streams)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        session = notification.sender
        streams = notification.data.streams
        if notification.data.action == 'add':
            try:
                chat_stream = next(stream for stream in streams if stream.type == 'chat')
            except StopIteration:
                pass
            else:
                notification.center.add_observer(self, sender=chat_stream)
                log.msg(u'Session %s has added chat' % session.call_id)
            try:
                audio_stream = next(stream for stream in streams if stream.type == 'audio')
            except StopIteration:
                pass
            else:
                notification.center.add_observer(self, sender=audio_stream)
                self._make_audio_stream_echo(audio_stream)
                log.msg(u'Session %s has added audio' % session.call_id)
        elif notification.data.action == 'remove':
            try:
                chat_stream = next(stream for stream in streams if stream.type == 'chat')
            except StopIteration:
                pass
            else:
                notification.center.remove_observer(self, sender=chat_stream)
                log.msg(u'Session %s has removed chat' % session.call_id)
            try:
                audio_stream = next(stream for stream in streams if stream.type == 'audio')
            except StopIteration:
                pass
            else:
                notification.center.remove_observer(self, sender=audio_stream)
                log.msg(u'Session %s has removed audio' % session.call_id)
            if not session.streams:
                log.msg(u'Session %s has removed all streams, session will be terminated' % session.call_id)
                session.end()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        notification.sender.reject_transfer(403)

