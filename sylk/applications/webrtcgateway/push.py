
import json

from twisted.internet import defer, reactor
from twisted.web.client import Agent, readBody
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

def _construct_and_send(result, request, destination):
    for device_token, push_parameters in result.iteritems():
        request.token = device_token
        request.app_id = push_parameters['app']
        request.platform = push_parameters['platform']
        request.device_id = push_parameters['device_id']
        _send_push_notification(json.dumps(request), destination)

def conference_invite(originator, destination, room, call_id):
    tokens = TokenStorage()
    request = sylkpush.ConferenceInviteEvent(token='dummy', app_id='dummy', platform='dummy', device_id='dummy',
                                             originator=originator.uri, from_display_name=originator.display_name, to=room, call_id=str(call_id))
    user_tokens = tokens[destination]
    if isinstance(user_tokens, set):
        return
    else:
        if isinstance(user_tokens, defer.Deferred):
            user_tokens.addCallback(lambda result: _construct_and_send(result, request, destination))
        else:
            _construct_and_send(user_tokens, request, destination)


@defer.inlineCallbacks
def _send_push_notification(payload, destination):
    if GeneralConfig.sylk_push_url:
        try:
            payload.token = payload.token.split('#')[0]
        except IndexError:
            pass
        try:
            r = yield agent.request('POST', GeneralConfig.sylk_push_url, headers, StringProducer(payload.__data__)
        except Exception as e:
            log.info('Error sending push notification to %s: %s', GeneralConfig.sylk_push_url, e)
        else:
            if r.code != 200:
                if r.code == 410:
                    log.info("Token expired, purging old token from storage")
                    tokens = TokenStorage()
                    tokens.remove(destination, payload.token)
                else:
                    log.warning('Error sending push notification: %s', r.phrase)
            else:
                log.debug('Sent push notification: %s', payload)
    else:
        log.warning('Cannot send push notification: no Sylk push server configured')
