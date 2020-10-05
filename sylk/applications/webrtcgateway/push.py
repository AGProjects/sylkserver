
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
        _send_push_notification(request, destination, device_token)

def conference_invite(originator, destination, room, call_id, audio, video):
    tokens = TokenStorage()
    if video:
        media_type = 'video'
    else:
        media_type = 'audio'

    request = sylkpush.ConferenceInviteEvent(token='dummy', app_id='dummy', platform='dummy', device_id='dummy',
                                             originator=originator.uri, from_display_name=originator.display_name, to=room, call_id=str(call_id),
                                             media_type=media_type)
    user_tokens = tokens[destination]
    if isinstance(user_tokens, set):
        return
    else:
        if isinstance(user_tokens, defer.Deferred):
            user_tokens.addCallback(lambda result: _construct_and_send(result, request, destination))
        else:
            _construct_and_send(user_tokens, request, destination)


@defer.inlineCallbacks
def _send_push_notification(payload, destination, token):
    if GeneralConfig.sylk_push_url:
        try:
            r = yield agent.request('POST', GeneralConfig.sylk_push_url, headers, StringProducer(json.dumps(payload.__data__)))
        except Exception as e:
            log.info('Error sending push notification to %s: %s', GeneralConfig.sylk_push_url, e)
        else:
            try:
                raw_body = yield readBody(r)
                body = json.loads(raw_body)
            except Exception as e:
                log.warning('Error reading response body: %s', e)
                body = {}

            try:  
                platform = body['data']['platform']  
            except KeyError:
                platform = 'Unknown platform' 
 
            if r.code != 200:
                try:
                    reason = body['data']['reason']
                except KeyError:
                    reason = None
                
                try:   
                    details = body['data']['body']['_content']['error']['message']
                except  KeyError:
                    details = None
                    
                if reason and details:
                    error_description = "%s %s" % (reason, details)
                elif reason:
                    error_description = reason
                else:
                    error_description = body
            
                if r.code == 410:
                    if body and 'application/json' in r.headers.getRawHeaders('content-type'):
                        try:
                            token = body['data']['token']
                        except KeyError:
                            pass
                        else:
                            log.info('Purging expired push token %s/%s' % (destination, token))
                            tokens = TokenStorage()
                            tokens.remove(destination, token)
                else:
                    log.warning('Error sending %s push notification for videoroom to %s/%s: %s (%s) %s %s' % (platform.title(), payload.to, destination, token[:15], r.phrase, r.code, error_description))
            else:
                log.info('Sent %s push notify for videoroom %s to %s/%s' % (platform.title(), payload.to, destination, token[:15]))
                
    else:
        log.warning('Cannot send push notification: no Sylk push server configured')
