# Copyright (C) 2015 AG Projects. See LICENSE for details
#

from sylk.applications import SylkApplication
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.util import IdentityFormatter
from sylk.applications.webrtcgateway.web import WebHandler


class WebRTCGatewayApplication(SylkApplication):
    def __init__(self):
        self.ws = WebHandler()

    def start(self):
        self.ws.start()

    def stop(self):
        self.ws.stop()

    def incoming_session(self, session):
        log.msg(u'New incoming session %s from %s rejected' % (session.call_id, IdentityFormatter.format(session.remote_identity)))
        session.reject(403)

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, request, data):
        request.reject(405)

