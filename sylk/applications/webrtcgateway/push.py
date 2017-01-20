
import json

from sipsimple.util import ISOTimestamp
from twisted.internet import defer, reactor
from twisted.web.client import Agent
from twisted.web.iweb import IBodyProducer
from twisted.web.http_headers import Headers
from zope.interface import implementer

from sylk.applications.webrtcgateway.configuration import GeneralConfig
from sylk.applications.webrtcgateway.logger import log

__all__ = ['incoming_session', 'missed_session']


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
        data = {'to': token,
                'notification': {},
                'data': {'sylkrtc': {}},
                'content_available': True
        }
        data['notification']['body'] = 'Incoming session from %s' % originator
        data['priority'] = 'high'
        data['time_to_live'] = 60    # don't deliver if phone is out for over a minute
        data['data']['sylkrtc']['event'] = 'incoming_session'
        data['data']['sylkrtc']['originator'] = originator
        data['data']['sylkrtc']['destination'] = destination
        data['data']['sylkrtc']['timestamp'] = str(ISOTimestamp.now())
        _send_push_notification(json.dumps(data))


def missed_session(originator, destination, tokens):
    for token in tokens:
        data = {'to': token,
                'notification': {},
                'data': {'sylkrtc': {}},
                'content_available': True
        }
        data['notification']['body'] = 'Missed session from %s' % originator
        data['priority'] = 'high'
        # No TTL, default is 4 weeks
        data['data']['sylkrtc']['event'] = 'missed_session'
        data['data']['sylkrtc']['originator'] = originator
        data['data']['sylkrtc']['destination'] = destination
        data['data']['sylkrtc']['timestamp'] = str(ISOTimestamp.now())
        _send_push_notification(json.dumps(data))


@defer.inlineCallbacks
def _send_push_notification(payload):
    if GeneralConfig.firebase_server_key:
        try:
            r = yield agent.request('POST', FIREBASE_API_URL, headers, StringProducer(payload))
        except Exception, e:
            log.msg('Error sending Firebase message: %s', e)
        else:
            if r.code != 200:
                log.warn('Error sending Firebase message: %s' % r.phrase)
            else:
                log.info("Pushed FCM {0[data][sylkrtc][event]} event for {0[data][sylkrtc][destination]}".format(payload))
    else:
        log.warn('Cannot send push notification: no Firebase server key configured')
