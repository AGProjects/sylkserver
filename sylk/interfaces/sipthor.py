
from application import log
from application.notification import NotificationCenter, NotificationData
from application.python.types import Singleton
from eventlib.twistedutil import block_on, callInGreenThread
from gnutls.interfaces.twisted import TLSContext, X509Credentials
from twisted.internet import defer

from thor.eventservice import EventServiceClient, ThorEvent
from thor.entities import ThorEntitiesRoleMap, GenericThorEntity as ThorEntity
from thor.scheduler import KeepRunning

import sylk
from sylk.configuration import SIPConfig, ThorNodeConfig


__all__ = 'ConferenceNode',


class ConferenceNode(EventServiceClient, metaclass=Singleton):
    topics = ["Thor.Members"]

    def __init__(self):
        pass

    def connectionLost(self, connector, reason):
        """Called when an event server connection goes away"""
        self.connections.discard(connector.transport)

    def connectionFailed(self, connector, reason):
        """Called when an event server connection has an unrecoverable error"""
        connector.failed = True
        available_connectors = set(c for c in self.connectors if not c.failed)
        if not available_connectors:
            NotificationCenter().post_notification('ThorNetworkGotFatalError', sender=self)

    def start(self, roles):
        # Needs to be called from a green thread
        log.info('Publishing SIPThor roles: %s' % ", ".join(roles))
        self.node = ThorEntity(SIPConfig.local_ip.normalized, roles, version=sylk.__version__)
        self.networks = {}
        self.presence_message = ThorEvent('Thor.Presence', self.node.id)
        self.shutdown_message = ThorEvent('Thor.Leave', self.node.id)
        credentials = X509Credentials(ThorNodeConfig.certificate, ThorNodeConfig.private_key, [ThorNodeConfig.ca])
        credentials.verify_peer = True
        tls_context = TLSContext(credentials)
        EventServiceClient.__init__(self, ThorNodeConfig.domain, tls_context)

    def stop(self):
        # Needs to be called from a green thread
        self._shutdown()

    def _monitor_event_servers(self):
        def wrapped_func():
            servers = self._get_event_servers()
            self._update_event_servers(servers)
        callInGreenThread(wrapped_func)
        return KeepRunning

    def _disconnect_all(self):
        for conn in self.connectors:
            conn.disconnect()

    def _shutdown(self):
        if self.disconnecting:
            return
        self.disconnecting = True
        self.dns_monitor.cancel()
        if self.advertiser:
            self.advertiser.cancel()
        if self.shutdown_message:
            self._publish(self.shutdown_message)
        requests = [conn.protocol.unsubscribe(*self.topics) for conn in self.connections]
        d = defer.DeferredList([request.deferred for request in requests])
        block_on(d)
        self._disconnect_all()

    def handle_event(self, event):
        #print "Received event: %s" % event
        networks = self.networks
        role_map = ThorEntitiesRoleMap(event.message) # mapping between role names and lists of nodes with that role
        updated = False
        for role in self.node.roles + ('sip_proxy',):
            try:
                network = networks[role]
            except KeyError:
                from thor import network as thor_network
                network = thor_network.new(ThorNodeConfig.multiply)
                networks[role] = network
            new_nodes = set(node.ip for node in role_map.get(role, []))
            old_nodes = set(node.decode() for node in network.nodes)
            added_nodes = new_nodes - old_nodes
            removed_nodes = old_nodes - new_nodes
            if removed_nodes:
                for node in removed_nodes:
                    network.remove_node(node.encode())
                plural = len(removed_nodes) != 1 and 's' or ''
                log.info("removed %s node%s: %s" % (role, plural, ', '.join(removed_nodes)))
                updated = True
            if added_nodes:
                for node in added_nodes:
                    network.add_node(node.encode())
                plural = len(added_nodes) != 1 and 's' or ''
                log.info("added %s node%s: %s" % (role, plural, ', '.join(added_nodes)))
                updated = True
        if updated:
            NotificationCenter().post_notification('ThorNetworkGotUpdate', sender=self, data=NotificationData(networks=self.networks))

