
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
        try:
            request.token = device_token.split('#')[1]
        except IndexError:
            request.token = device_token
        request.app_id = push_parameters['app']
        request.platform = push_parameters['platform']
        request.device_id = push_parameters['device_id']
        _send_push_notification(json.dumps(request.__data__), destination)

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
            r = yield agent.request('POST', GeneralConfig.sylk_push_url, headers, StringProducer(payload))
        except Exception as e:
            log.info('Error sending push notification to %s: %s', GeneralConfig.sylk_push_url, e)
        else:
            if r.code != 200:
                log.warning('Error sending push notification: %s', r.phrase)
            else:
                try:
                    body = yield readBody(r)
                except Exception as e:
                      log.warning("Error reading body: %s", e)
                else:
                    if "application/json" in r.headers.getRawHeaders('content-type'):
                        _maybe_purge_token(body, destination)
                log.debug('Sent push notification: %s', payload)
    else:
        log.warning('Cannot send push notification: no Sylk push server configured')

def _maybe_purge_token(body, destination):
    body = sylkpush.PushReply(**json.loads(body))
    reply_body = body.data.body
    try:
        failure = reply_body._content.failure
        if failure == 1:
            reason = reply_body._content.results[0]['error']
            if reason == 'NotRegistered':
                log.info("Token expired, purging old token from storage")
                tokens = TokenStorage()
                tokens.remove(destination, body.data.token)
    except AttributeError, KeyError:
        pass
