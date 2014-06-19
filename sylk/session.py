# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

import random

from datetime import datetime
from time import time

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null, limit
from application.python.types import Singleton
from eventlib import api, coros, proc
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import Engine, Invitation, Subscription, SIPCoreError, sip_status_messages
from sipsimple.core import ContactHeader, RouteHeader, SubjectHeader, FromHeader, ToHeader
from sipsimple.core import SIPURI, SDPConnection, SDPSession
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads import ParserError
from sipsimple.payloads.conference import ConferenceDocument
from sipsimple.session import Session as _Session
from sipsimple.session import SessionReplaceHandler, TransferHandler, DialogID, TransferInfo
from sipsimple.session import InvitationDisconnectedError, MediaStreamDidFailError, InterruptSubscription, TerminateSubscription, SubscriptionError, SIPSubscriptionDidFail
from sipsimple.session import transition_state
from sipsimple.streams import MediaStreamRegistry, InvalidStreamError, UnknownStreamError
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import Command, run_in_green_thread
from twisted.internet import reactor
from zope.interface import implements

from sylk.configuration import SIPConfig


class ConferenceHandler(object):
    implements(IObserver)

    def __init__(self, session):
        self.session = session
        self.active = False
        self.subscribed = False
        self._command_proc = None
        self._command_channel = coros.queue()
        self._data_channel = coros.queue()
        self._subscription = None
        self._subscription_proc = None
        self._subscription_timer = None
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, name='NetworkConditionsDidChange')
        self._command_proc = proc.spawn(self._run)

    def _run(self):
        while True:
            command = self._command_channel.wait()
            handler = getattr(self, '_CH_%s' % command.name)
            handler(command)

    def _activate(self):
        self.active = True
        command = Command('subscribe')
        self._command_channel.send(command)
        return command

    def _deactivate(self):
        self.active = False
        command = Command('unsubscribe')
        self._command_channel.send(command)
        return command

    def _resubscribe(self):
        command = Command('subscribe')
        self._command_channel.send(command)
        return command

    def _terminate(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.session)
        notification_center.remove_observer(self, name='NetworkConditionsDidChange')
        self._deactivate()
        command = Command('terminate')
        self._command_channel.send(command)
        command.wait()
        self.session = None

    def _CH_subscribe(self, command):
        if self._subscription_timer is not None and self._subscription_timer.active():
            self._subscription_timer.cancel()
        self._subscription_timer = None
        if self._subscription_proc is not None:
            subscription_proc = self._subscription_proc
            subscription_proc.kill(InterruptSubscription)
            subscription_proc.wait()
        self._subscription_proc = proc.spawn(self._subscription_handler, command)

    def _CH_unsubscribe(self, command):
        # Cancel any timer which would restart the subscription process
        if self._subscription_timer is not None and self._subscription_timer.active():
            self._subscription_timer.cancel()
        self._subscription_timer = None
        if self._subscription_proc is not None:
            subscription_proc = self._subscription_proc
            subscription_proc.kill(TerminateSubscription)
            subscription_proc.wait()
            self._subscription_proc = None
        command.signal()

    def _CH_terminate(self, command):
        command.signal()
        raise proc.ProcExit()

    def _subscription_handler(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        try:
            # Lookup routes
            account = self.session.account
            if account.sip.outbound_proxy is not None:
                uri = SIPURI(host=account.sip.outbound_proxy.host,
                             port=account.sip.outbound_proxy.port,
                             parameters={'transport': account.sip.outbound_proxy.transport})
            elif account.sip.always_use_my_proxy:
                uri = SIPURI(host=account.id.domain)
            else:
                uri = SIPURI.new(self.session.remote_identity.uri)
            lookup = DNSLookup()
            try:
                routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
            except DNSLookupError, e:
                timeout = random.uniform(15, 30)
                raise SubscriptionError(error='DNS lookup failed: %s' % e, timeout=timeout)

            target_uri = SIPURI.new(self.session.remote_identity.uri)
            refresh_interval =  getattr(command, 'refresh_interval', account.sip.subscribe_interval)

            timeout = time() + 30
            for route in routes:
                remaining_time = timeout - time()
                if remaining_time > 0:
                    transport = route.transport
                    parameters = {} if transport=='udp' else {'transport': transport}
                    contact_uri = SIPURI(user=account.contact.username, host=SIPConfig.local_ip.normalized, port=getattr(Engine(), '%s_port' % transport), parameters=parameters)
                    subscription = Subscription(target_uri, FromHeader(SIPURI.new(self.session.local_identity.uri)),
                                                ToHeader(target_uri),
                                                ContactHeader(contact_uri),
                                                'conference',
                                                RouteHeader(route.uri),
                                                credentials=account.credentials,
                                                refresh=refresh_interval)
                    notification_center.add_observer(self, sender=subscription)
                    try:
                        subscription.subscribe(timeout=limit(remaining_time, min=1, max=5))
                    except SIPCoreError:
                        notification_center.remove_observer(self, sender=subscription)
                        timeout = 5
                        raise SubscriptionError(error='Internal error', timeout=timeout)
                    self._subscription = subscription
                    try:
                        while True:
                            notification = self._data_channel.wait()
                            if notification.sender is subscription and notification.name == 'SIPSubscriptionDidStart':
                                break
                    except SIPSubscriptionDidFail, e:
                        notification_center.remove_observer(self, sender=subscription)
                        self._subscription = None
                        if e.data.code == 407:
                            # Authentication failed, so retry the subscription in some time
                            timeout = random.uniform(60, 120)
                            raise SubscriptionError(error='Authentication failed', timeout=timeout)
                        elif e.data.code == 423:
                            # Get the value of the Min-Expires header
                            timeout = random.uniform(60, 120)
                            if e.data.min_expires is not None and e.data.min_expires > refresh_interval:
                                raise SubscriptionError(error='Interval too short', timeout=timeout, min_expires=e.data.min_expires)
                            else:
                                raise SubscriptionError(error='Interval too short', timeout=timeout)
                        elif e.data.code in (405, 406, 489, 1400):
                            command.signal(e)
                            return
                        else:
                            # Otherwise just try the next route
                            continue
                    else:
                        self.subscribed = True
                        command.signal()
                        break
            else:
                # There are no more routes to try, reschedule the subscription
                timeout = random.uniform(60, 180)
                raise SubscriptionError(error='No more routes to try', timeout=timeout)
            # At this point it is subscribed. Handle notifications and ending/failures.
            try:
                while True:
                    notification = self._data_channel.wait()
                    if notification.sender is not self._subscription:
                        continue
                    if notification.name == 'SIPSubscriptionGotNotify':
                        if notification.data.event == 'conference' and notification.data.body:
                            try:
                                conference_info = ConferenceDocument.parse(notification.data.body)
                            except ParserError:
                                pass
                            else:
                                notification_center.post_notification('SIPSessionGotConferenceInfo', sender=self.session, data=NotificationData(conference_info=conference_info))
                    elif notification.name == 'SIPSubscriptionDidEnd':
                        break
            except SIPSubscriptionDidFail:
                self._command_channel.send(Command('subscribe'))
            notification_center.remove_observer(self, sender=self._subscription)
        except InterruptSubscription, e:
            if not self.subscribed:
                command.signal(e)
            if self._subscription is not None:
                notification_center.remove_observer(self, sender=self._subscription)
                try:
                    self._subscription.end(timeout=2)
                except SIPCoreError:
                    pass
        except TerminateSubscription, e:
            if not self.subscribed:
                command.signal(e)
            if self._subscription is not None:
                try:
                    self._subscription.end(timeout=2)
                except SIPCoreError:
                    pass
                else:
                    try:
                        while True:
                            notification = self._data_channel.wait()
                            if notification.sender is self._subscription and notification.name == 'SIPSubscriptionDidEnd':
                                break
                    except SIPSubscriptionDidFail:
                        pass
                finally:
                    notification_center.remove_observer(self, sender=self._subscription)
        except SubscriptionError, e:
            if 'min_expires' in e.attributes:
                command = Command('subscribe', command.event, refresh_interval=e.attributes['min_expires'])
            else:
                command = Command('subscribe', command.event)
            self._subscription_timer = reactor.callLater(e.timeout, self._command_channel.send, command)
        finally:
            self.subscribed = False
            self._subscription = None
            self._subscription_proc = None

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSubscriptionDidStart(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSubscriptionDidEnd(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSubscriptionDidFail(self, notification):
        self._data_channel.send_exception(SIPSubscriptionDidFail(notification.data))

    def _NH_SIPSubscriptionGotNotify(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSessionDidStart(self, notification):
        if self.session.remote_focus:
            self._activate()

    @run_in_green_thread
    def _NH_SIPSessionDidFail(self, notification):
        self._terminate()

    @run_in_green_thread
    def _NH_SIPSessionDidEnd(self, notification):
        self._terminate()

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        if self.session.remote_focus and not self.active:
            self._activate()
        elif not self.session.remote_focus and self.active:
            self._deactivate()

    def _NH_NetworkConditionsDidChange(self, notification):
        if self.active:
            self._resubscribe()


class Session(_Session):

    def init_incoming(self, invitation, data):
        remote_sdp = invitation.sdp.proposed_remote
        if not remote_sdp:
            invitation.send_response(488)
            return
        self.proposed_streams = []
        for index, media_stream in enumerate(remote_sdp.media):
            if media_stream.port != 0:
                for stream_type in MediaStreamRegistry():
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
        if not self.proposed_streams:
            invitation.send_response(488)
            return
        self.direction = 'incoming'
        self.state = 'incoming'
        self.transport = invitation.transport
        self._invitation = invitation
        self.conference = ConferenceHandler(self)
        self.transfer_handler = TransferHandler(self)
        if 'isfocus' in invitation.remote_contact_header.parameters:
            self.remote_focus = True
        try:
            self.__dict__['subject'] = data.headers['Subject'].subject
        except KeyError:
            pass
        if 'Referred-By' in data.headers or 'Replaces' in data.headers:
            self.transfer_info = TransferInfo()
            if 'Referred-By' in data.headers:
                self.transfer_info.referred_by = data.headers['Referred-By'].body
            if 'Replaces' in data.headers:
                replaces_header = data.headers.get('Replaces')
                replaced_dialog_id = DialogID(replaces_header.call_id, local_tag=replaces_header.to_tag, remote_tag=replaces_header.from_tag)
                session_manager = SessionManager()
                try:
                    self.replaced_session = next(session for session in session_manager.sessions if session._invitation is not None and session._invitation.dialog_id == replaced_dialog_id)
                except StopIteration:
                    invitation.send_response(481)
                    return
                else:
                    self.transfer_info.replaced_dialog_id = replaced_dialog_id
                    replace_handler = SessionReplaceHandler(self)
                    replace_handler.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=invitation)
        notification_center.post_notification('SIPSessionNewIncoming', self, NotificationData(streams=self.proposed_streams, headers=data.headers))

    @transition_state(None, 'connecting')
    @run_in_green_thread
    def connect(self, from_header, to_header, routes, streams, contact_header=None, is_focus=False, subject=None, extra_headers=[]):
        self.greenlet = api.getcurrent()
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        connected = False
        unhandled_notifications = []

        self.direction = 'outgoing'
        self.proposed_streams = streams
        self.route = routes[0]
        self.transport = self.route.transport
        self.local_focus = is_focus
        self._invitation = Invitation()
        self._local_identity = from_header
        self._remote_identity = to_header
        self.conference = ConferenceHandler(self)
        self.transfer_handler = Null
        self.__dict__['subject'] = subject
        notification_center.add_observer(self, sender=self._invitation)
        notification_center.post_notification('SIPSessionNewOutgoing', self, NotificationData(streams=streams[:]))
        for stream in self.proposed_streams:
            notification_center.add_observer(self, sender=stream)
            stream.initialize(self, direction='outgoing')

        try:
            wait_count = len(self.proposed_streams)
            while wait_count > 0:
                notification = self._channel.wait()
                if notification.name == 'MediaStreamDidInitialize':
                    wait_count -= 1
            if contact_header is None:
                try:
                    contact_uri = self.account.contact[self.route]
                except KeyError, e:
                    for stream in self.proposed_streams:
                        notification_center.remove_observer(self, sender=stream)
                        stream.deactivate()
                        stream.end()
                    self._fail(originator='local', code=480, reason=sip_status_messages[480], error=str(e))
                    return
                else:
                    contact_header = ContactHeader(contact_uri)
            local_ip = contact_header.uri.host
            connection = SDPConnection(local_ip)
            local_sdp = SDPSession(local_ip, name=settings.user_agent)
            for index, stream in enumerate(self.proposed_streams):
                stream.index = index
                media = stream.get_local_media(remote_sdp=None, index=index)
                if media.connection is None or (media.connection is not None and not media.has_ice_attributes and not media.has_ice_candidates):
                    media.connection = connection
                local_sdp.media.append(media)
            route_header = RouteHeader(self.route.uri)
            if is_focus:
                contact_header.parameters['isfocus'] = None
            if self.subject:
                extra_headers.append(SubjectHeader(self.subject))
            self._invitation.send_invite(to_header.uri, from_header, to_header, route_header, contact_header, local_sdp, extra_headers=extra_headers)
            try:
                with api.timeout(settings.sip.invite_timeout):
                    while True:
                        notification = self._channel.wait()
                        if notification.name == 'SIPInvitationGotSDPUpdate':
                            if notification.data.succeeded:
                                local_sdp = notification.data.local_sdp
                                remote_sdp = notification.data.remote_sdp
                                break
                            else:
                                for stream in self.proposed_streams:
                                    notification_center.remove_observer(self, sender=stream)
                                    stream.deactivate()
                                    stream.end()
                                self._fail(originator='remote', code=0, reason=None, error='SDP negotiation failed: %s' % notification.data.error)
                                return
                        elif notification.name == 'SIPInvitationChangedState':
                            if notification.data.state == 'early':
                                if notification.data.code == 180:
                                    notification_center.post_notification('SIPSessionGotRingIndication', self, )
                                notification_center.post_notification('SIPSessionGotProvisionalResponse', self, NotificationData(code=notification.data.code, reason=notification.data.reason))
                            elif notification.data.state == 'connected':
                                if not connected:
                                    connected = True
                                else:
                                    unhandled_notifications.append(notification)
                            elif notification.data.state == 'disconnected':
                                raise InvitationDisconnectedError(notification.sender, notification.data)
            except api.TimeoutError:
                self.end()
                return

            notification_center.post_notification('SIPSessionWillStart', self)
            stream_map = dict((stream.index, stream) for stream in self.proposed_streams)
            for index, local_media in enumerate(local_sdp.media):
                remote_media = remote_sdp.media[index]
                stream = stream_map[index]
                if remote_media.port:
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
            invitation_notifications = []
            with api.timeout(self.media_stream_timeout):
                wait_count = len(self.proposed_streams)
                while wait_count > 0:
                    notification = self._channel.wait()
                    if notification.name == 'MediaStreamDidStart':
                        wait_count -= 1
                    elif notification.name == 'SIPInvitationChangedState':
                        invitation_notifications.append(notification)
            [self._channel.send(notification) for notification in invitation_notifications]
            while not connected or self._channel:
                notification = self._channel.wait()
                if notification.name == 'SIPInvitationChangedState':
                    if notification.data.state == 'early':
                        if notification.data.code == 180:
                            notification_center.post_notification('SIPSessionGotRingIndication', self)
                        notification_center.post_notification('SIPSessionGotProvisionalResponse', self, NotificationData(code=notification.data.code, reason=notification.data.reason))
                    elif notification.data.state == 'connected':
                        if not connected:
                            connected = True
                        else:
                            unhandled_notifications.append(notification)
                    elif notification.data.state == 'disconnected':
                        raise InvitationDisconnectedError(notification.sender, notification.data)
        except (MediaStreamDidFailError, api.TimeoutError), e:
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            if isinstance(e, api.TimeoutError):
                error = 'media stream timed out while starting'
            else:
                error = 'media stream failed: %s' % e.data.reason
            self._fail(originator='local', code=0, reason=None, error=error)
        except InvitationDisconnectedError, e:
            notification_center.remove_observer(self, sender=self._invitation)
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            self.state = 'terminated'
            # As weird as it may sound, PJSIP accepts a BYE even without receiving a final response to the INVITE
            if e.data.prev_state in ('connecting', 'connected') or getattr(e.data, 'method', None) == 'BYE':
                notification_center.post_notification('SIPSessionWillEnd', self, NotificationData(originator=e.data.originator))
                self.end_time = datetime.now()
                notification_center.post_notification('SIPSessionDidEnd', self, NotificationData(originator=e.data.originator, end_reason=e.data.disconnect_reason))
            else:
                if e.data.originator == 'remote':
                    code = e.data.code
                    reason = e.data.reason
                elif e.data.disconnect_reason == 'timeout':
                    code = 408
                    reason = 'timeout'
                elif e.data.originator == 'local' and e.data.code == 408:
                    code = e.data.code
                    reason = e.data.reason
                else:
                    code = 0
                    reason = None
                if e.data.originator == 'remote' and code // 100 == 3:
                    redirect_identities = e.data.headers.get('Contact', [])
                else:
                    redirect_identities = None
                notification_center.post_notification('SIPSessionDidFail', self, NotificationData(originator=e.data.originator, code=code, reason=reason, failure_reason=e.data.disconnect_reason, redirect_identities=redirect_identities))
            self.greenlet = None
        except SIPCoreError, e:
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            self._fail(originator='local', code=0, reason=None, error='SIP core error: %s' % str(e))
        else:
            self.greenlet = None
            self.state = 'connected'
            self.streams = self.proposed_streams
            self.proposed_streams = None
            self.start_time = datetime.now()
            notification_center.post_notification('SIPSessionDidStart', self, NotificationData(streams=self.streams[:]))
            for notification in unhandled_notifications:
                self.handle_notification(notification)
            if self._hold_in_progress:
                self._send_hold()


class SessionManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        self.sessions = []
        self.state = None
        self._channel = coros.queue()

    def start(self):
        self.state = 'starting'
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPSessionManagerWillStart', sender=self)
        notification_center.add_observer(self, 'SIPInvitationChangedState')
        notification_center.add_observer(self, 'SIPSessionNewIncoming')
        notification_center.add_observer(self, 'SIPSessionNewOutgoing')
        notification_center.add_observer(self, 'SIPSessionDidFail')
        notification_center.add_observer(self, 'SIPSessionDidEnd')
        self.state = 'started'
        notification_center.post_notification('SIPSessionManagerDidStart', sender=self)

    def stop(self):
        self.state = 'stopping'
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPSessionManagerWillEnd', sender=self)
        for session in self.sessions:
            session.end()
        while self.sessions:
            self._channel.wait()
        notification_center.remove_observer(self, 'SIPInvitationChangedState')
        notification_center.remove_observer(self, 'SIPSessionNewIncoming')
        notification_center.remove_observer(self, 'SIPSessionNewOutgoing')
        notification_center.remove_observer(self, 'SIPSessionDidFail')
        notification_center.remove_observer(self, 'SIPSessionDidEnd')
        self.state = 'stopped'
        notification_center.post_notification('SIPSessionManagerDidEnd', sender=self)

    @run_in_twisted_thread
    def handle_notification(self, notification):
        if notification.name == 'SIPInvitationChangedState' and notification.data.state == 'incoming':
            account = AccountManager().sylkserver_account
            notification.sender.send_response(100)
            session = Session(account)
            session.init_incoming(notification.sender, notification.data)
        elif notification.name in ('SIPSessionNewIncoming', 'SIPSessionNewOutgoing'):
            self.sessions.append(notification.sender)
        elif notification.name in ('SIPSessionDidFail', 'SIPSessionDidEnd'):
            self.sessions.remove(notification.sender)
            if self.state == 'stopping':
                self._channel.send(notification)

