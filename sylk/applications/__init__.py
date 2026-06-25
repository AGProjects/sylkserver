
import abc
import importlib
import logging
import os
import socket
import struct
import sys

from application import log
from application.configuration.datatypes import NetworkRange
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.decorator import execute_once
from application.python.types import Singleton
from collections import defaultdict
from itertools import chain
from sipsimple.threading import run_in_twisted_thread
from zope.interface import implementer

from sylk.configuration import ServerConfig, SIPConfig, ThorNodeConfig


__all__ = 'ISylkApplication', 'ApplicationRegistry', 'SylkApplication', 'IncomingRequestHandler', 'ApplicationLogger'


SYLK_APP_HEADER = 'X-Sylk-App'


def find_builtin_applications():
    applications_directory = os.path.dirname(__file__)
    for path, dirs, files in os.walk(applications_directory):
        parent_directory, name = os.path.split(path)
        if parent_directory == applications_directory and '__init__.py' in files and name not in ServerConfig.disabled_applications:
            yield name
        if path != applications_directory:
            del dirs[:]  # do not descend more than 1 level


def find_extra_applications():
    if ServerConfig.extra_applications_dir:
        applications_directory = os.path.realpath(ServerConfig.extra_applications_dir.normalized)
        for path, dirs, files in os.walk(applications_directory):
            parent_directory, name = os.path.split(path)
            if parent_directory == applications_directory and '__init__.py' in files and name not in ServerConfig.disabled_applications:
                yield name
            if path != applications_directory:
                del dirs[:]  # do not descend more than 1 level


def find_applications():
    return chain(find_builtin_applications(), find_extra_applications())


class ApplicationRegistry(object, metaclass=Singleton):
    def __init__(self):
        self.application_map = {}

    def __getitem__(self, name):
        return self.application_map[name]

    def __contains__(self, name):
        return name in self.application_map

    def __iter__(self):
        return iter(list(self.application_map.values()))

    def __len__(self):
        return len(self.application_map)

    #@execute_once
    def load_applications(self):
        for name in find_builtin_applications():
            try:
                __import__('sylk.applications.{name}'.format(name=name))
            except ImportError as e:
                log.error('Failed to load builtin application {name!r}: {exception!s}'.format(name=name, exception=e))
        for name in find_extra_applications():
            if name in sys.modules:
                # being able to log this is contingent on this function only executing once
                log.warning('Not loading extra application {name!r} as it would overshadow a system package/module'.format(name=name))
                continue
            try:
                importlib.load_module(name, *importlib.machinery.PathFinder().find_spec(name, [ServerConfig.extra_applications_dir.normalized]))
            except ImportError as e:
                log.error('Failed to load extra application {name!r}: {exception!s}'.format(name=name, exception=e))

    def add(self, app_class):
        try:
            app = app_class()
        except Exception as e:
            log.exception('Failed to initialize {app.__appname__!r} application: {exception!s}'.format(app=app_class, exception=e))
        else:
            self.application_map[app.__appname__] = app

    def get(self, name, default=None):
        return self.application_map.get(name, default)


class ApplicationName(object):
    def __get__(self, instance, instance_type):
        name = instance_type.__name__
        return name[:-11].lower() if name.endswith('Application') else name.lower()


class SylkApplicationMeta(abc.ABCMeta, Singleton):
    """Metaclass for defining SylkServer applications: a Singleton that also adds them to the application registry"""

    def __init__(cls, name, bases, dic):
        super(SylkApplicationMeta, cls).__init__(name, bases, dic)
        if name != 'SylkApplication':
            ApplicationRegistry().add(cls)


class SylkApplication(object, metaclass=SylkApplicationMeta):
    """Base class for all SylkServer applications"""
    __appname__ = ApplicationName()

    @abc.abstractmethod
    def start(self):
        pass

    @abc.abstractmethod
    def stop(self):
        pass

    @abc.abstractmethod
    def incoming_session(self, session):
        pass

    @abc.abstractmethod
    def incoming_subscription(self, subscribe_request, data):
        pass

    @abc.abstractmethod
    def incoming_referral(self, refer_request, data):
        pass

    @abc.abstractmethod
    def incoming_message(self, message_request, data):
        pass

    def incoming_publish(self, publish_request, data):
        # Default: applications do not accept SIP PUBLISH. Override to handle it.
        # Not abstract so existing applications need not implement it.
        publish_request.answer(489)  # Bad Event


class ApplicationNotLoadedError(Exception):
    pass


@implementer(IObserver)
class IncomingRequestHandler(object, metaclass=Singleton):
    """Handle incoming requests and match them to applications"""

    def __init__(self):
        self.application_registry = ApplicationRegistry()
        self.application_registry.load_applications()
        log.info('Loaded applications: {}'.format(', '.join(sorted(app.__appname__ for app in self.application_registry))))
        if ServerConfig.default_application not in self.application_registry:
            log.warning('Default application "%s" does not exist, falling back to "conference"' % ServerConfig.default_application)
            ServerConfig.default_application = 'conference'
        else:
            log.info('Default application: %s' % ServerConfig.default_application)
        self.application_map = dict((item.split(':')) for item in ServerConfig.application_map)
        if self.application_map:
            txt = 'Application map:\n'
            inverted_app_map = defaultdict(list)
            for url, app in self.application_map.items():
                inverted_app_map[app].append(url)
            for app, urls in inverted_app_map.items():
                txt += '  {}: {}\n'.format(app, ', '.join(urls))
            log.info(txt[:-1])
        self.authorization_handler = AuthorizationHandler()
        self.call_limit_handler = CallLimitHandler()

    def start(self):
        for app in self.application_registry:
            try:
                app.start()
            except Exception as e:
                log.exception('Failed to start {app.__appname__!r} application: {exception!s}'.format(app=app, exception=e))
        self.authorization_handler.start()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionNewIncoming')
        notification_center.add_observer(self, name='SIPIncomingSubscriptionGotSubscribe')
        notification_center.add_observer(self, name='SIPIncomingReferralGotRefer')
        notification_center.add_observer(self, name='SIPIncomingRequestGotRequest')

    def stop(self):
        self.authorization_handler.stop()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SIPSessionNewIncoming')
        notification_center.remove_observer(self, name='SIPIncomingSubscriptionGotSubscribe')
        notification_center.remove_observer(self, name='SIPIncomingReferralGotRefer')
        notification_center.remove_observer(self, name='SIPIncomingRequestGotRequest')
        for app in self.application_registry:
            try:
                app.stop()
            except Exception as e:
                log.exception('Failed to stop {app.__appname__!r} application: {exception!s}'.format(app=app, exception=e))

    def get_application(self, ruri, headers):
        if SYLK_APP_HEADER in headers:
            application_name = headers[SYLK_APP_HEADER].body.strip()
            # Sessions from the WebRTC gateway's video-room chat bridge and the
            # sylk-janus-audio-bridge select their target app explicitly via this
            # header. Log it at the selection point — before the app's
            # incoming_session runs — so routing decisions are visible regardless
            # of which application ends up handling the request.
            log.debug('Application %r selected by %s header for %s' % (application_name, SYLK_APP_HEADER, ruri))
        else:
            application_name = ServerConfig.default_application
            if self.application_map:
                prefixes = ("%s@%s" % (ruri.user, ruri.host), ruri.host, ruri.user)
                for prefix in prefixes:
                    if prefix in self.application_map:
                        application_name = self.application_map[prefix]
                        break
        try:
            return self.application_registry[application_name]
        except KeyError:
            log.error('Application %s is not loaded' % application_name)
            raise ApplicationNotLoadedError

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @staticmethod
    def _header_uri(headers, name):
        """Best-effort extraction of a SIP URI from a header for logging.

        Returns the URI as a string, or '?' when the header is missing
        or doesn't expose a .uri attribute. Never raises — log helpers
        must not become a source of secondary failures.
        """
        try:
            return str(headers.get(name).uri)
        except (AttributeError, KeyError, TypeError):
            return '?'

    @staticmethod
    def _peer_ip(peer_address):
        """Stringified peer IP suitable for logs.

        sipsimple's peer_address.ip is bytes; embedding it in a format
        string yields "b'1.2.3.4'", which is awkward to read and breaks
        copy/paste back into config files. Decode defensively.
        """
        ip = getattr(peer_address, 'ip', peer_address)
        if isinstance(ip, bytes):
            try:
                return ip.decode()
            except Exception:
                return repr(ip)
        return ip

    @staticmethod
    def _request_summary(method, request_uri, peer_address, headers):
        """One-line summary string used in every rejection log entry.

        Includes the SIP method, Request-URI, peer IP and From-URI so
        that a single grep on the conference / webrtcgateway / sylk log
        ties the rejection back to the matching SIP-trace packet
        without needing to cross-reference Call-IDs.
        """
        return '%s %s from peer %s, From %s' % (
            method, request_uri,
            IncomingRequestHandler._peer_ip(peer_address),
            IncomingRequestHandler._header_uri(headers, 'From'))

    def _NH_SIPSessionNewIncoming(self, notification):
        session = notification.sender
        try:
            self.authorization_handler.authorize_source(session.peer_address.ip)
        except UnauthorizedRequest as e:
            log.info('rejected 403 INVITE %s from peer %s, From %s — %s' % (
                session.request_uri, self._peer_ip(session.peer_address),
                session.remote_identity.uri, e))
            session.reject(403)
            return
        try:
            self.call_limit_handler.check(session.peer_address.ip)
        except CallLimitExceeded as e:
            log.info('rejected 603 INVITE %s from peer %s, From %s — %s' % (
                session.request_uri, self._peer_ip(session.peer_address),
                session.remote_identity.uri, e))
            session.reject(603, 'Maximum calls exceeded')
            return
        try:
            app = self.get_application(session.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            log.info('rejected 404 INVITE %s from peer %s, From %s — no application loaded for this request' % (
                session.request_uri, self._peer_ip(session.peer_address),
                session.remote_identity.uri))
            session.reject(404)
        else:
            self.call_limit_handler.track(session)
            app.incoming_session(session)

    def _NH_SIPIncomingSubscriptionGotSubscribe(self, notification):
        subscribe_request = notification.sender
        try:
            self.authorization_handler.authorize_source(subscribe_request.peer_address.ip)
        except UnauthorizedRequest as e:
            log.info('rejected 403 %s — %s' % (self._request_summary(
                'SUBSCRIBE', notification.data.request_uri,
                subscribe_request.peer_address, notification.data.headers), e))
            subscribe_request.reject(403)
            return
        try:
            app = self.get_application(notification.data.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            log.info('rejected 404 %s — no application loaded for this request' % self._request_summary(
                'SUBSCRIBE', notification.data.request_uri,
                subscribe_request.peer_address, notification.data.headers))
            subscribe_request.reject(404)
        else:
            app.incoming_subscription(subscribe_request, notification.data)

    def _NH_SIPIncomingReferralGotRefer(self, notification):
        refer_request = notification.sender
        try:
            self.authorization_handler.authorize_source(refer_request.peer_address.ip)
        except UnauthorizedRequest as e:
            log.info('rejected 403 %s — %s' % (self._request_summary(
                'REFER', notification.data.request_uri,
                refer_request.peer_address, notification.data.headers), e))
            refer_request.reject(403)
            return
        try:
            app = self.get_application(notification.data.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            log.info('rejected 404 %s — no application loaded for this request' % self._request_summary(
                'REFER', notification.data.request_uri,
                refer_request.peer_address, notification.data.headers))
            refer_request.reject(404)
        else:
            app.incoming_referral(refer_request, notification.data)

    def _NH_SIPIncomingRequestGotRequest(self, notification):
        request = notification.sender
        method = notification.data.method
        if method not in ('MESSAGE', 'PUBLISH'):
            log.info('rejected 405 %s — only MESSAGE and PUBLISH are accepted as out-of-dialog requests' % self._request_summary(
                method, notification.data.request_uri,
                request.peer_address, notification.data.headers))
            request.answer(405)
            return
        try:
            self.authorization_handler.authorize_source(request.peer_address.ip)
        except UnauthorizedRequest as e:
            log.info('rejected 403 %s — %s' % (self._request_summary(
                method, notification.data.request_uri,
                request.peer_address, notification.data.headers), e))
            request.answer(403)
            return
        try:
            app = self.get_application(notification.data.request_uri, notification.data.headers)
        except ApplicationNotLoadedError:
            log.info('rejected 404 %s — no application loaded for this request' % self._request_summary(
                method, notification.data.request_uri,
                request.peer_address, notification.data.headers))
            request.answer(404)
        else:
            if method == 'PUBLISH':
                app.incoming_publish(request, notification.data)
            else:
                app.incoming_message(request, notification.data)


class UnauthorizedRequest(Exception):
    pass


@implementer(IObserver)
class AuthorizationHandler(object):

    def __init__(self):
        self.state = None
        self.trusted_peers = SIPConfig.trusted_peers
        self.thor_nodes = []

    @property
    def include_thor(self):
        # True when [SIP] trusted_peers contains the 'thor_network' keyword.
        return getattr(self.trusted_peers, 'include_thor', False)

    @property
    def trusted_parties(self):
        # The live Thor topology is folded into the trusted set only when
        # the operator explicitly asks for it via the 'thor_network' keyword
        # in [SIP] trusted_peers (and Thor is actually enabled). Statically
        # configured peers are always honoured, so 'thor_network' can be
        # combined with explicit ranges, e.g. a local
        # sylk-janus-audio-bridge or any other on-LAN peer:
        #
        #     trusted_peers = thor_network, 10.0.0.0/8
        #
        # Without the keyword, Thor nodes are not trusted even when Thor is
        # enabled — only the explicit ranges apply.
        if ThorNodeConfig.enabled and self.include_thor:
            return list(self.thor_nodes) + list(self.trusted_peers)
        return list(self.trusted_peers)

    def start(self):
        NotificationCenter().add_observer(self, name='ThorNetworkGotUpdate')
        self.state = 'started'

    def stop(self):
        self.state = 'stopped'
        NotificationCenter().remove_observer(self, name='ThorNetworkGotUpdate')

    def authorize_source(self, ip_address):
        if self.state != 'started':
            raise UnauthorizedRequest('authorization handler not started')
        ip_str = ip_address.decode() if isinstance(ip_address, bytes) else ip_address
        addr_long = struct.unpack('!L', socket.inet_aton(ip_str))[0]
        for range in self.trusted_parties:
            if addr_long & range[1] == range[0]:
                return True
        if ThorNodeConfig.enabled and self.include_thor:
            raise UnauthorizedRequest(
                'source IP %s not in any of %d thor_nodes or %d trusted_peers' %
                (ip_str, len(self.thor_nodes), len(self.trusted_peers)))
        raise UnauthorizedRequest(
            'source IP %s not in any of %d trusted_peers' %
            (ip_str, len(self.trusted_peers)))

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ThorNetworkGotUpdate(self, notification):
        self.thor_nodes = [NetworkRange(node.decode()) for node in chain.from_iterable(n.nodes for n in list(notification.data.networks.values()))]


class CallLimitExceeded(Exception):
    pass


@implementer(IObserver)
class CallLimitHandler(object):
    """DoS / call-flood mitigation.

    Keeps a live count of active SIP calls (INVITE sessions) both globally
    and per source IP, and refuses new INVITEs that would breach the limits
    configured in [SIP] of config.ini:

        maximum_call_count            - cap on total concurrent calls
        maximum_call_count_per_ip     - cap on concurrent calls per source IP
        maximum_call_count_exclude_ips- networks exempt from both caps

    A call is counted from the moment its INVITE is accepted into an
    application (via track()) until the session ends or fails, at which
    point the per-session observer below decrements the counters again.
    Excluded source IPs are never counted and never rejected. A limit of 0
    means "unlimited" for that dimension.
    """

    def __init__(self):
        self.active_calls = set()            # id(session) of every tracked call
        self.calls_per_ip = defaultdict(int)  # source IP -> active call count

    @staticmethod
    def _ip_str(ip_address):
        return ip_address.decode() if isinstance(ip_address, bytes) else ip_address

    def _is_excluded(self, ip_address):
        ip_str = self._ip_str(ip_address)
        try:
            addr_long = struct.unpack('!L', socket.inet_aton(ip_str))[0]
        except (socket.error, OSError, TypeError):
            return False
        # NetworkRangeList('none') evaluates to None (no networks), so guard
        # against a non-iterable value before iterating.
        for net in SIPConfig.maximum_call_count_exclude_ips or ():
            if addr_long & net[1] == net[0]:
                return True
        return False

    def check(self, ip_address):
        """Raise CallLimitExceeded if a new call from ip_address would breach
        a configured limit. Excluded source IPs are always allowed."""
        if self._is_excluded(ip_address):
            return
        max_total = SIPConfig.maximum_call_count
        if max_total and len(self.active_calls) >= max_total:
            raise CallLimitExceeded(
                'global active call count limit reached (%d)' % max_total)
        max_per_ip = SIPConfig.maximum_call_count_per_ip
        if max_per_ip:
            ip_str = self._ip_str(ip_address)
            if self.calls_per_ip.get(ip_str, 0) >= max_per_ip:
                raise CallLimitExceeded(
                    'per-IP active call count limit reached (%d for %s)' % (max_per_ip, ip_str))

    def track(self, session):
        """Start counting an accepted call and arrange for it to be
        decremented when the session terminates. Excluded source IPs and
        sessions already tracked are ignored."""
        ip_address = session.peer_address.ip
        if self._is_excluded(ip_address):
            return
        key = id(session)
        if key in self.active_calls:
            return
        ip_str = self._ip_str(ip_address)
        self.active_calls.add(key)
        self.calls_per_ip[ip_str] += 1
        session._sylk_call_limit_ip = ip_str
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session, name='SIPSessionDidEnd')
        notification_center.add_observer(self, sender=session, name='SIPSessionDidFail')
        log.info('Usage: %s calls, %s from %s%s' % (
            self._format_count(len(self.active_calls), SIPConfig.maximum_call_count),
            self._format_count(self.calls_per_ip[ip_str], SIPConfig.maximum_call_count_per_ip),
            ip_str, self._status_suffix()))

    @staticmethod
    def _format_count(current, limit):
        """Render a counter as 'current/limit', or 'current (no limit)' when
        the limit is 0 (disabled)."""
        if limit:
            return '%d/%d' % (current, limit)
        return '%d (no limit)' % current

    @staticmethod
    def _status_suffix():
        """Suffix for the 'Active calls' line: active conference count, and
        per-mixer slot usage when the mixer pool is active."""
        parts = []
        try:
            if 'conference' in ApplicationRegistry():
                from sylk.applications.conference import ConferenceApplication
                parts.append('conferences: %d' % len(ConferenceApplication()._rooms))
        except Exception:
            pass
        try:
            from sylk.audio import mixer_pool
            if mixer_pool.size > 1:
                parts.append('mixer slots: %s' % mixer_pool.load_summary())
        except Exception:
            pass
        return (', ' + ', '.join(parts)) if parts else ''

    def _untrack(self, session):
        key = id(session)
        if key not in self.active_calls:
            return
        self.active_calls.discard(key)
        ip_str = getattr(session, '_sylk_call_limit_ip', None)
        ip_current = 0
        if ip_str is not None:
            self.calls_per_ip[ip_str] -= 1
            ip_current = self.calls_per_ip[ip_str]
            if self.calls_per_ip[ip_str] <= 0:
                self.calls_per_ip.pop(ip_str, None)
                ip_current = 0
        notification_center = NotificationCenter()
        for name in ('SIPSessionDidEnd', 'SIPSessionDidFail'):
            try:
                notification_center.remove_observer(self, sender=session, name=name)
            except KeyError:
                pass
        # Log the updated totals once a call ends, mirroring track().
        log.info('Active calls: %s total, %s from %s%s' % (
            self._format_count(len(self.active_calls), SIPConfig.maximum_call_count),
            self._format_count(ip_current, SIPConfig.maximum_call_count_per_ip),
            ip_str if ip_str is not None else '?', self._status_suffix()))

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidEnd(self, notification):
        self._untrack(notification.sender)

    def _NH_SIPSessionDidFail(self, notification):
        self._untrack(notification.sender)


class ApplicationLogger(object):
    def __new__(cls, package):
        return logging.getLogger(package.split('.')[-1])
