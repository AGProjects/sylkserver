# Copyright (C) 2011 AG-Projects.
#
# This module is proprietary to AG Projects. Use of this module by third
# parties is not supported.

__all__ = ['ConferenceNode']

from application import log
from application.notification import NotificationCenter, NotificationData
from application.python.types import Singleton
from eventlib.twistedutil import block_on, callInGreenThread
from gnutls.interfaces.twisted import X509Credentials
from gnutls.constants import COMP_DEFLATE, COMP_LZO, COMP_NULL
from twisted.internet import defer

from thor.eventservice import EventServiceClient, ThorEvent
from thor.entities import ThorEntitiesRoleMap, GenericThorEntity as ThorEntity
from thor.scheduler import KeepRunning

import sylk
from sylk.configuration import SIPConfig, ThorNodeConfig


class ConferenceNode(EventServiceClient):
    __metaclass__ = Singleton
    topics = ["Thor.Members"]

    def __init__(self):
        # Needs to be called from a green thread
        self.node = ThorEntity(SIPConfig.local_ip, ['conference_server', 'xmpp_gateway'], version=sylk.__version__)
        self.networks = {}
        self.presence_message = ThorEvent('Thor.Presence', self.node.id)
        self.shutdown_message = ThorEvent('Thor.Leave', self.node.id)
        credentials = X509Credentials(ThorNodeConfig.certificate, ThorNodeConfig.private_key, [ThorNodeConfig.ca])
        credentials.verify_peer = True
        credentials.session_params.compressions = (COMP_LZO, COMP_DEFLATE, COMP_NULL)
        EventServiceClient.__init__(self, ThorNodeConfig.domain, credentials)

    def connectionLost(self, connector, reason):
        """Called when an event server connection goes away"""
        self.connections.discard(connector.transport)

    def connectionFailed(self, connector, reason):
        """Called when an event server connection has an unrecoverable error"""
        connector.failed = True
        available_connectors = set(c for c in self.connectors if not c.failed)
        if not available_connectors:
            log.fatal("All Thor Event Servers have unrecoverable errors.")
            NotificationCenter().post_notification('ThorNetworkGotFatalError', sender=self)

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
        for role in ('sip_proxy', 'conference_server', 'xmpp_gateway'):
            try:
                network = networks[role]
            except KeyError:
                from thor import network as thor_network
                network = thor_network.new(ThorNodeConfig.multiply)
                networks[role] = network
            new_nodes = set([node.ip for node in role_map.get(role, [])])
            old_nodes = set(network.nodes)
            added_nodes = new_nodes - old_nodes
            removed_nodes = old_nodes - new_nodes
            if removed_nodes:
                for node in removed_nodes:
                    network.remove_node(node)
                plural = len(removed_nodes) != 1 and 's' or ''
                log.msg("removed %s node%s: %s" % (role, plural, ', '.join(removed_nodes)))
                updated = True
            if added_nodes:
                for node in added_nodes:
                    network.add_node(node)
                plural = len(added_nodes) != 1 and 's' or ''
                log.msg("added %s node%s: %s" % (role, plural, ', '.join(added_nodes)))
                updated = True
            #print "Thor %s nodes: %s" % (role, str(network.nodes))
        if updated:
            NotificationCenter().post_notification('ThorNetworkGotUpdate', sender=self, data=NotificationData(networks=self.networks))

