
import json

from twisted.internet import defer, reactor
from twisted.web.client import Agent
from twisted.web.iweb import IBodyProducer
from twisted.web.http_headers import Headers
from zope.interface import implementer

from .configuration import GeneralConfig
from .logger import log
from .models import firebase


__all__ = 'incoming_session', 'missed_session', 'conference_invite'


agent = Agent(reactor)
headers = Headers({'User-Agent': ['SylkServer'],
                   'Content-Type': ['application/json'],
                   'Authorization': ['key=%s' % GeneralConfig.firebase_server_key]})
FIREBASE_API_URL = 'https://fcm.googleapis.com/fcm/send'


@implementer(IBodyProducer)
class StringProducer(object):
    def __init__(self, data):
        self.body = data
        self.length = len(data)

    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
        pass


def incoming_session(originator, destination, tokens):
    for token in tokens:
        request = firebase.FirebaseRequest(token, event=firebase.IncomingCallEvent(originator=originator, destination=destination), time_to_live=60)
        _send_push_notification(json.dumps(request.__data__))


def missed_session(originator, destination, tokens):
    for token in tokens:
        request = firebase.FirebaseRequest(token, event=firebase.MissedCallEvent(originator=originator, destination=destination))
        _send_push_notification(json.dumps(request.__data__))


def conference_invite(originator, destination, room, tokens):
    for token in tokens:
        request = firebase.FirebaseRequest(token, event=firebase.ConferenceInviteEvent(originator=originator, destination=destination, room=room), time_to_live=3600)
        _send_push_notification(json.dumps(request.__data__))


@defer.inlineCallbacks
def _send_push_notification(payload):
    if GeneralConfig.firebase_server_key:
        try:
            r = yield agent.request('POST', FIREBASE_API_URL, headers, StringProducer(payload))
        except Exception as e:
            log.info('Error sending Firebase message: %s', e)
        else:
            if r.code != 200:
                log.warn('Error sending Firebase message: %s' % r.phrase)
    else:
        log.warn('Cannot send push notification: no Firebase server key configured')
