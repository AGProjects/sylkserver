
import json

from twisted.internet import defer, reactor
from twisted.web.client import Agent
from twisted.web.iweb import IBodyProducer
from twisted.web.http_headers import Headers
from zope.interface import implementer

from .configuration import GeneralConfig
from .logger import log
from .models import sylkpush
from .storage import TokenStorage


__all__ = 'conference_invite'


agent = Agent(reactor)
headers = Headers({'User-Agent': ['SylkServer'],
                   'Content-Type': ['application/json']})


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


def conference_invite(originator, destination, room, call_id):
    tokens = TokenStorage()
    request = sylkpush.ConferenceInviteEvent(token='dummy', app_id='dummy', platform='dummy', device_id='dummy',
                                             originator=originator.uri, from_display_name=originator.display_name, to=room, call_id=str(call_id))
    if isinstance(tokens[destination], set):
        return
    else:
        for device_id, push_parameters in tokens[destination].iteritems():
            try:
                request.token = push_parameters['token'].split('#')[1]
            except IndexError:
                request.token = push_parameters['token']
            request.app_id = push_parameters['app']
            request.platform = push_parameters['platform']
            request.device_id = device_id
            _send_push_notification(json.dumps(request.__data__))


@defer.inlineCallbacks
def _send_push_notification(payload):
    if GeneralConfig.sylk_push_url:
        try:
            r = yield agent.request('POST', GeneralConfig.sylk_push_url, headers, StringProducer(payload))
        except Exception as e:
            log.info('Error sending push notification: %s', e)
        else:
            if r.code != 200:
                log.warning('Error sending push notification: %s', r.phrase)
            else:
                log.debug('Sent push notification: %s', payload)
    else:
        log.warning('Cannot send push notification: no Firebase server key configured')
