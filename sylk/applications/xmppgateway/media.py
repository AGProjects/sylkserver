# Copyright (C) 2013 AG Projects. See LICENSE for details
#

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from eventlib.twistedutil import block_on
from sipsimple.account import AccountManager
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import ContactHeader, FromHeader, ToHeader
from sipsimple.core import Engine, SIPURI, SIPCoreError
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.streams import MediaStreamRegistry as SIPMediaStreamRegistry
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implements

from sylk.applications.xmppgateway.datatypes import Identity, FrozenURI, generate_sylk_resource, encode_resource, decode_resource
from sylk.applications.xmppgateway.logger import log
from sylk.applications.xmppgateway.xmpp import XMPPManager
from sylk.applications.xmppgateway.xmpp.jingle.session import JingleSession
from sylk.applications.xmppgateway.xmpp.jingle.streams import MediaStreamRegistry as JingleMediaStreamRegistry
from sylk.applications.xmppgateway.xmpp.stanzas import jingle
from sylk.configuration import SIPConfig
from sylk.session import Session


__all__ = ['MediaSessionHandler']


class MediaSessionHandler(object):
    implements(IObserver)

    def __init__(self):
        self.started = False
        self.ended = False

        self._sip_identity = None
        self._xmpp_identity = None

        self._audio_bidge = AudioConference()
        self.sip_session = None
        self.jingle_session = None

    @classmethod
    def new_from_sip_session(cls, session):
        proposed_stream_types = set([stream.type for stream in session.proposed_streams])
        streams = []
        for stream_type in proposed_stream_types:
            try:
                klass = JingleMediaStreamRegistry().get(stream_type)
            except Exception:
                continue
            streams.append(klass())
        if not streams:
            session.reject(488)
            return None
        session.send_ring_indication()
        instance = cls()
        NotificationCenter().add_observer(instance, sender=session)
        # Get URI representing the SIP side
        contact_uri = session._invitation.remote_contact_header.uri
        if contact_uri.parameters.get('gr') is not None:
            sip_leg_uri = FrozenURI(contact_uri.user, contact_uri.host, contact_uri.parameters.get('gr'))
        else:
            tmp = session.remote_identity.uri
            sip_leg_uri = FrozenURI(tmp.user, tmp.host, generate_sylk_resource())
        instance._sip_identity = Identity(sip_leg_uri)
        # Get URI representing the XMPP side
        request_uri = session._invitation.request_uri
        remote_resource = request_uri.parameters.get('gr', None)
        if remote_resource is not None:
            try:
                remote_resource = decode_resource(remote_resource)
            except (TypeError, UnicodeError):
                remote_resource = None
        xmpp_leg_uri = FrozenURI(request_uri.user, request_uri.host, remote_resource)
        instance._xmpp_identity = Identity(xmpp_leg_uri)
        instance.sip_session = session
        instance._start_outgoing_jingle_session(streams)
        return instance

    @classmethod
    def new_from_jingle_session(cls, session):
        proposed_stream_types = set([stream.type for stream in session.proposed_streams])
        streams = []
        for stream_type in proposed_stream_types:
            try:
                klass = SIPMediaStreamRegistry().get(stream_type)
            except Exception:
                continue
            streams.append(klass())
        if not streams:
            session.reject('unsupported-applications')
            return None
        session.send_ring_indication()
        instance = cls()
        NotificationCenter().add_observer(instance, sender=session)
        instance._xmpp_identity = session.remote_identity
        instance._sip_identity = session.local_identity
        instance.jingle_session = session
        instance._start_outgoing_sip_session(streams)
        return instance

    @property
    def sip_identity(self):
        return self._sip_identity

    @property
    def xmpp_identity(self):
        return self._xmpp_identity

    def _set_started(self, value):
        old_value = self.__dict__.get('started', False)
        self.__dict__['started'] = value
        if not old_value and value:
            NotificationCenter().post_notification('MediaSessionHandlerDidStart', sender=self)
    def _get_started(self):
        return self.__dict__['started']
    started = property(_get_started, _set_started)
    del _get_started, _set_started

    @run_in_green_thread
    def _start_outgoing_sip_session(self, streams):
        notification_center = NotificationCenter()
        # self.xmpp_identity is our local identity on the SIP side
        from_uri = self.xmpp_identity.uri.as_sip_uri()
        from_uri.parameters.pop('gr', None)    # no GRUU in From header
        to_uri = self.sip_identity.uri.as_sip_uri()
        to_uri.parameters.pop('gr', None)      # no GRUU in To header
        # TODO: need to fix GRUU in the proxy
        #contact_uri = self.xmpp_identity.uri.as_sip_uri()
        #contact_uri.parameters['gr'] = encode_resource(contact_uri.parameters['gr'].decode('utf-8'))
        lookup = DNSLookup()
        settings = SIPSimpleSettings()
        account = AccountManager().sylkserver_account
        if account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host,
                         port=account.sip.outbound_proxy.port,
                         parameters={'transport': account.sip.outbound_proxy.transport})
        else:
            uri = to_uri
        try:
            routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
        except DNSLookupError:
            log.warning('DNS lookup error while looking for %s proxy' % uri)
            notification_center.post_notification('MedialSessionHandlerDidFail', sender=self, data=NotificationData(reason='DNS lookup error'))
            return
        route = routes.pop(0)
        from_header = FromHeader(from_uri)
        to_header = ToHeader(to_uri)
        transport = route.transport
        parameters = {} if transport=='udp' else {'transport': transport}
        contact_uri = SIPURI(user=account.contact.username, host=SIPConfig.local_ip.normalized, port=getattr(Engine(), '%s_port' % transport), parameters=parameters)
        contact_header = ContactHeader(contact_uri)
        self.sip_session = Session(account)
        notification_center.add_observer(self, sender=self.sip_session)
        self.sip_session.connect(from_header, to_header, contact_header=contact_header, routes=[route], streams=streams)

    @run_in_green_thread
    def _start_outgoing_jingle_session(self, streams):
        if self.xmpp_identity.uri.resource is not None:
            self.sip_session.reject()
            return
        xmpp_manager = XMPPManager()
        local_jid = self.sip_identity.uri.as_xmpp_jid()
        remote_jid = self.xmpp_identity.uri.as_xmpp_jid()

        # If this was an invitation to a conference, use the information in the Referred-By header
        if self.sip_identity.uri.host in xmpp_manager.muc_domains and self.sip_session.transfer_info and self.sip_session.transfer_info.referred_by:
            try:
                referred_by_uri = SIPURI.parse(self.sip_session.transfer_info.referred_by)
            except SIPCoreError:
                self.sip_session.reject(488)
                return
            else:
                inviter_uri = FrozenURI(referred_by_uri.user, referred_by_uri.host)
                local_jid = inviter_uri.as_xmpp_jid()

        # Use disco to gather potential JIDs to call
        d = xmpp_manager.disco_client_protocol.requestItems(remote_jid, sender=local_jid)
        try:
            items = block_on(d)
        except Exception:
            items = []
        if not items:
            self.sip_session.reject(480)
            return

        # Check which items support Jingle
        valid = []
        for item in items:
            d = xmpp_manager.disco_client_protocol.requestInfo(item.entity, nodeIdentifier=item.nodeIdentifier, sender=local_jid)
            try:
                info = block_on(d)
            except Exception:
                continue
            if jingle.NS_JINGLE in info.features and jingle.NS_JINGLE_APPS_RTP in info.features:
                valid.append(item.entity)
        if not valid:
            self.sip_session.reject(480)
            return

        # TODO: start multiple sessions?
        self._xmpp_identity = Identity(FrozenURI.parse(valid[0]))

        notification_center = NotificationCenter()
        if self.sip_identity.uri.host in xmpp_manager.muc_domains:
            self.jingle_session = JingleSession(xmpp_manager.jingle_coin_protocol)
        else:
            self.jingle_session = JingleSession(xmpp_manager.jingle_protocol)
        notification_center.add_observer(self, sender=self.jingle_session)
        self.jingle_session.connect(self.sip_identity, self.xmpp_identity, streams)

    def end(self):
        if self.ended:
            return
        notification_center = NotificationCenter()
        if self.sip_session is not None:
            notification_center.remove_observer(self, sender=self.sip_session)
            if self.sip_session.direction == 'incoming' and not self.started:
                self.sip_session.reject()
            else:
                self.sip_session.end()
            self.sip_session = None
        if self.jingle_session is not None:
            notification_center.remove_observer(self, sender=self.jingle_session)
            if self.jingle_session.direction == 'incoming' and not self.started:
                self.jingle_session.reject()
            else:
                self.jingle_session.end()
            self.jingle_session = None
        self.ended = True
        if self.started:
            notification_center.post_notification('MediaSessionHandlerDidEnd', sender=self)
        else:
            notification_center.post_notification('MediaSessionHandlerDidFail', sender=self)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidStart(self, notification):
        log.msg("SIP session %s started" % notification.sender._invitation.call_id)
        if self.sip_session.direction == 'outgoing':
            # Time to accept the Jingle session and bridge them together
            try:
                audio_stream = next(stream for stream in self.sip_session.streams if stream.type=='audio')
            except StopIteration:
                pass
            else:
                self._audio_bidge.add(audio_stream)
            self.jingle_session.accept(self.jingle_session.proposed_streams)
        else:
            # Both sessions have been accepted now
            self.started = True
            try:
                audio_stream = next(stream for stream in self.sip_session.streams if stream.type=='audio')
            except StopIteration:
                pass
            else:
                self._audio_bidge.add(audio_stream)

    def _NH_SIPSessionDidEnd(self, notification):
        log.msg("SIP session %s ended" % notification.sender._invitation.call_id)
        notification.center.remove_observer(self, sender=self.sip_session)
        self.sip_session = None
        self.end()

    def _NH_SIPSessionDidFail(self, notification):
        log.msg("SIP session %s failed (%s)" % (notification.sender._invitation.call_id, notification.data.reason))
        notification.center.remove_observer(self, sender=self.sip_session)
        self.sip_session = None
        self.end()

    def _NH_SIPSessionGotProposal(self, notification):
        self.sip_session.reject_proposal()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        self.sip_session.reject_transfer(403)

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        if notification.data.originator == 'remote':
            if notification.data.on_hold:
                self.jingle_session.hold()
            else:
                self.jingle_session.unhold()

    def _NH_JingleSessionDidStart(self, notification):
        log.msg("Jingle session %s started" % notification.sender.id)
        if self.jingle_session.direction == 'incoming':
            # Both sessions have been accepted now
            self.started = True
            try:
                audio_stream = next(stream for stream in self.jingle_session.streams if stream.type=='audio')
            except StopIteration:
                pass
            else:
                self._audio_bidge.add(audio_stream)
        else:
            # Time to accept the Jingle session and bridge them together
            try:
                audio_stream = next(stream for stream in self.jingle_session.streams if stream.type=='audio')
            except StopIteration:
                pass
            else:
                self._audio_bidge.add(audio_stream)
            self.sip_session.accept(self.sip_session.proposed_streams)

    def _NH_JingleSessionDidEnd(self, notification):
        log.msg("Jingle session %s ended" % notification.sender.id)
        notification.center.remove_observer(self, sender=self.jingle_session)
        self.jingle_session = None
        self.end()

    def _NH_JingleSessionDidFail(self, notification):
        log.msg("Jingle session %s failed (%s)" % (notification.sender.id, notification.data.reason))
        notification.center.remove_observer(self, sender=self.jingle_session)
        self.jingle_session = None
        self.end()

    def _NH_JingleSessionDidChangeHoldState(self, notification):
        if notification.data.originator == 'remote':
            if notification.data.on_hold:
                self.sip_session.hold()
            else:
                self.sip_session.unhold()

