# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

from datetime import datetime

from application.notification import NotificationCenter
from application.python import Null
from eventlet import api
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import Invitation, SIPCoreError, sip_status_messages
from sipsimple.core import ContactHeader, RouteHeader, SubjectHeader
from sipsimple.core import SDPConnection, SDPSession
from sipsimple.session import Session, InvitationDisconnectedError, MediaStreamDidFailError, transition_state
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData


class ServerSession(Session):

    @transition_state(None, 'connecting')
    @run_in_green_thread
    def connect(self, from_header, to_header, routes, streams, contact_header=None, is_focus=False, subject=None, extra_headers=[]):
        self.greenlet = api.getcurrent()
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        connected = False
        received_code = 0
        received_reason = None
        unhandled_notifications = []

        self.direction = 'outgoing'
        self.proposed_streams = streams
        self.route = routes[0]
        self.transport = self.route.transport
        self.local_focus = is_focus
        self._invitation = Invitation()
        self._local_identity = from_header
        self._remote_identity = to_header
        self.__dict__['subject'] = subject
        self.conference = Null
        notification_center.add_observer(self, sender=self._invitation)
        notification_center.post_notification('SIPSessionNewOutgoing', self, TimestampedNotificationData(streams=streams))
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
            local_sdp = SDPSession(local_ip, connection=SDPConnection(local_ip), name=settings.user_agent)
            stun_addresses = []
            for index, stream in enumerate(self.proposed_streams):
                stream.index = index
                media = stream.get_local_media(for_offer=True)
                local_sdp.media.append(media)
                stun_addresses.extend((value.split(' ', 5)[4] for value in media.attributes.getall('candidate') if value.startswith('S ')))
            if stun_addresses:
                local_sdp.connection.address = stun_addresses[0]
            route_header = RouteHeader(self.route.get_uri())
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
                                self._fail(originator='remote', code=received_code, reason=received_reason, error='SDP negotiation failed: %s' % notification.data.error)
                                return
                        elif notification.name == 'SIPInvitationChangedState':
                            if notification.data.state == 'early':
                                if notification.data.code == 180:
                                    notification_center.post_notification('SIPSessionGotRingIndication', self, TimestampedNotificationData())
                                notification_center.post_notification('SIPSessionGotProvisionalResponse', self, TimestampedNotificationData(code=notification.data.code, reason=notification.data.reason))
                            elif notification.data.state == 'connecting':
                                received_code = notification.data.code
                                received_reason = notification.data.reason
                            elif notification.data.state == 'connected':
                                if not connected:
                                    connected = True
                                    notification_center.post_notification('SIPSessionDidProcessTransaction', self,
                                                                          TimestampedNotificationData(originator='local', method='INVITE', code=received_code, reason=received_reason))
                                else:
                                    unhandled_notifications.append(notification)
                            elif notification.data.state == 'disconnected':
                                raise InvitationDisconnectedError(notification.sender, notification.data)
            except api.TimeoutError:
                self.greenlet = None
                self.end()
                return

            notification_center.post_notification('SIPSessionWillStart', self, TimestampedNotificationData())
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
                            notification_center.post_notification('SIPSessionGotRingIndication', self, TimestampedNotificationData())
                        notification_center.post_notification('SIPSessionGotProvisionalResponse', self, TimestampedNotificationData(code=notification.data.code, reason=notification.data.reason))
                    elif notification.data.state == 'connecting':
                        received_code = notification.data.code
                        received_reason = notification.data.reason
                    elif notification.data.state == 'connected':
                        if not connected:
                            connected = True
                            notification_center.post_notification('SIPSessionDidProcessTransaction', self,
                                                                  TimestampedNotificationData(originator='local', method='INVITE', code=received_code, reason=received_reason))
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
            self._fail(originator='local', code=received_code, reason=received_reason, error=error)
        except InvitationDisconnectedError, e:
            notification_center.remove_observer(self, sender=self._invitation)
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            self.state = 'terminated'
            # As it weird as it may sound, PJSIP accepts a BYE even without receiving a final response to the INVITE
            if e.data.prev_state in ('connecting', 'connected') or getattr(e.data, 'method', None) == 'BYE':
                notification_center.post_notification('SIPSessionWillEnd', self, TimestampedNotificationData(originator=e.data.originator))
                if e.data.originator == 'remote':
                    notification_center.post_notification('SIPSessionDidProcessTransaction', self, TimestampedNotificationData(originator='remote', method=e.data.method, code=200, reason=sip_status_messages[200]))
                self.end_time = datetime.now()
                notification_center.post_notification('SIPSessionDidEnd', self, TimestampedNotificationData(originator=e.data.originator, end_reason=e.data.disconnect_reason))
            else:
                if e.data.originator == 'remote':
                    notification_center.post_notification('SIPSessionDidProcessTransaction', self, TimestampedNotificationData(originator='local', method='INVITE', code=e.data.code, reason=e.data.reason))
                if e.data.originator == 'remote':
                    code = e.data.code
                    reason = e.data.reason
                elif e.data.disconnect_reason == 'timeout':
                    code = 408
                    reason = 'timeout'
                else:
                    code = 0
                    reason = None
                if e.data.originator == 'remote' and code // 100 == 3:
                    redirect_identities = e.data.headers.get('Contact', [])
                else:
                    redirect_identities = None
                notification_center.post_notification('SIPSessionDidFail', self, TimestampedNotificationData(originator=e.data.originator, code=code, reason=reason, failure_reason=e.data.disconnect_reason, redirect_identities=redirect_identities))
            self.greenlet = None
        except SIPCoreError, e:
            for stream in self.proposed_streams:
                notification_center.remove_observer(self, sender=stream)
                stream.deactivate()
                stream.end()
            self._fail(originator='local', code=received_code, reason=received_reason, error='SIP core error: %s' % str(e))
        else:
            self.greenlet = None
            self.state = 'connected'
            self.streams = self.proposed_streams
            self.proposed_streams = None
            self.start_time = datetime.now()
            notification_center.post_notification('SIPSessionDidStart', self, TimestampedNotificationData(streams=self.streams))
            for notification in unhandled_notifications:
                self.handle_notification(notification)
            if self._hold_in_progress:
                self._send_hold()

