
from sylk.applications import SylkApplication
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.util import IdentityFormatter
from sylk.applications.webrtcgateway.web import WebHandler


class WebRTCGatewayApplication(SylkApplication):
    def __init__(self):
        self.web_handler = WebHandler()

    def start(self):
        self.web_handler.start()

    def stop(self):
        self.web_handler.stop()

    def incoming_session(self, session):
        log.msg(u'New incoming session %s from %s rejected' % (session.call_id, IdentityFormatter.format(session.remote_identity)))
        session.reject(403)

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, request, data):
        request.reject(405)

