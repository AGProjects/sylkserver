
from sylk.applications import SylkApplication
from sylk.applications.webrtcgateway.logger import log
from sylk.applications.webrtcgateway.storage import TokenStorage
from sylk.applications.webrtcgateway.web import WebHandler, AdminWebHandler


class WebRTCGatewayApplication(SylkApplication):
    def __init__(self):
        self.web_handler = WebHandler()
        self.admin_web_handler = AdminWebHandler()

    def start(self):
        self.web_handler.start()
        self.admin_web_handler.start()
        # Load tokens from the storage
        token_storage = TokenStorage()
        token_storage.load()

    def stop(self):
        self.web_handler.stop()
        self.admin_web_handler.stop()

    def incoming_session(self, session):
        log.info('New incoming session {session.call_id} from sip:{uri.user}@{uri.host} rejected'.format(session=session, uri=session.remote_identity.uri))
        session.reject(403)

    def incoming_subscription(self, request, data):
        request.reject(405)

    def incoming_referral(self, request, data):
        request.reject(405)

    def incoming_message(self, request, data):
        request.reject(405)
