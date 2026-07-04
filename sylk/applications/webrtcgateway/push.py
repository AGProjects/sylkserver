
import json

from twisted.internet import defer, reactor
from twisted.web.client import Agent, readBody
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer
from zope.interface import implementer

from .configuration import GeneralConfig
from .logger import log
from .models import sylkpush
from .storage import TokenStorage

__all__ = 'conference_invite', 'message'


agent = Agent(reactor)
headers = Headers({'User-Agent': ['SylkServer'],
                   'Content-Type': ['application/json']})


@implementer(IBodyProducer)
class BytesProducer(object):
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
    for device, push_parameters in result.items():
        request.token = push_parameters['token']
        if isinstance(request, sylkpush.MessageEvent) and push_parameters['background_token']:
            request.token = push_parameters['background_token']
        request.app_id = push_parameters['app_id']
        request.platform = push_parameters['platform']
        request.device_id = push_parameters['device_id']
        _send_push_notification(request, destination, request.token)


def conference_invite(originator, destination, room, call_id, audio, video):
    tokens = TokenStorage()
    if video:
        media_type = 'video'
    else:
        media_type = 'audio'

    request = sylkpush.ConferenceInviteEvent(token='dummy', app_id='dummy', platform='dummy', device_id='dummy',
                                             originator=originator.uri, from_display_name=originator.display_name, to=room, call_id=str(call_id),
                                             media_type=media_type, account=destination)
    user_tokens = tokens[destination]
    if isinstance(user_tokens, set):
        return
    else:
        if isinstance(user_tokens, defer.Deferred):
            user_tokens.addCallback(lambda result: _construct_and_send(result, request, destination))
        else:
            _construct_and_send(user_tokens, request, destination)


def message(originator, destination, call_id, badge, message):
    tokens = TokenStorage()
    media_type = 'sms'

    request = sylkpush.MessageEvent(token='dummy', app_id='dummy', platform='dummy', device_id='dummy',
                                    originator=originator.uri, from_display_name=originator.display_name, to=destination, call_id=str(call_id),
                                    media_type=media_type, badge=badge, content_type=message.content_type, content=message.content)
    user_tokens = tokens[destination]
    if isinstance(user_tokens, set):
        return
    else:
        if isinstance(user_tokens, defer.Deferred):
            user_tokens.addCallback(lambda result: _construct_and_send(result, request, destination))
        else:
            _construct_and_send(user_tokens, request, destination)


# Signals that the push provider (APNs/FCM, surfaced through the Sylk push
# server) rejected the request because the payload exceeded the platform size
# cap (FCM data: 4096 bytes; APNs alert: 4096; VoIP: 5120). APNs answers a clean
# 413/PayloadTooLarge; FCM phrases it as a size error inside a 400. We match the
# explicit 413 plus any response whose text mentions a size overflow.
_TOO_LARGE_MARKERS = ('too large', 'payloadtoolarge', 'payload too large',
                      'message_too_big', 'messagetoobig', 'entity too large',
                      'request entity too large', 'maximum payload', 'payload size',
                      'exceeds the maximum', 'message is too big', 'body is too long')


# APNs caps alert payloads at 4096 bytes (VoIP: 5120; FCM data: 4096). The push
# server adds its own envelope (aps dict etc.) on top of what we send it, so a
# request already at/over the cap is guaranteed to be rejected. Strip the body
# preemptively instead of burning a round-trip on a doomed request.
_MAX_PAYLOAD_SIZE = 4096


def _is_payload_too_large(code, *texts):
    # APNs answers HTTP 413 PayloadTooLarge; accept the code as int or str, and
    # whether it arrives as the HTTP status or inside the relay's body.
    try:
        if int(code) == 413:
            return True
    except (TypeError, ValueError):
        pass
    blob = ' '.join(str(t) for t in texts if t).lower()
    return any(marker in blob for marker in _TOO_LARGE_MARKERS)


def _message_payload_without_content(payload):
    # Rebuild the MessageEvent WITHOUT the body. content is optional in the
    # model, so we omit it entirely (no 'content' key on the wire) rather than
    # sending an empty string. Explicit reconstruction avoids mutating the shared
    # request object that _construct_and_send reuses across devices. The
    # notification still carries sender/badge/type; the app fetches the real
    # content from the journal/WS sync.
    return sylkpush.MessageEvent(token=payload.token, app_id=payload.app_id,
                                 platform=payload.platform, device_id=payload.device_id,
                                 originator=payload.originator, from_display_name=payload.from_display_name,
                                 to=payload.to, call_id=payload.call_id, media_type=payload.media_type,
                                 badge=payload.badge, content_type=payload.content_type)


@defer.inlineCallbacks
def _send_push_notification(payload, destination, token, allow_strip_retry=True):
    if GeneralConfig.sylk_push_url:
        try:
            body_bytes = json.dumps(payload.__data__).encode()
            # Preflight: if the payload already exceeds the provider size cap,
            # don't even try sending it with the message body — strip it now.
            if allow_strip_retry and getattr(payload, 'content', '') and len(body_bytes) > _MAX_PAYLOAD_SIZE:
                log.info('Push payload for %s/%s is %d bytes (limit %d) — sending without message body' %
                         (payload.to, destination, len(body_bytes), _MAX_PAYLOAD_SIZE))
                payload = _message_payload_without_content(payload)
                body_bytes = json.dumps(payload.__data__).encode()
                allow_strip_retry = False  # already stripped; nothing left to retry with
            r = yield agent.request(b'POST',
                                    GeneralConfig.sylk_push_url.encode(),
                                    headers,
                                    BytesProducer(body_bytes)
                                    )
        except Exception as e:
            log.info('Error sending push notification to %s: %s', GeneralConfig.sylk_push_url, e)
        else:
            try:
                raw_body = yield readBody(r)
            except Exception as e:
                log.warning('Error reading response body: %s', e)
            else:
                try:
                    body = json.loads(raw_body)
                except Exception as e:
                    log.warning('Error parsing response body: %s', e)
                    body = {}

            # Pull the provider status/reason out of the relay envelope. The Sylk
            # push server forwards the APNs/FCM result inside body['data'], so a
            # 413 PayloadTooLarge can show up EITHER as the HTTP status (r.code)
            # OR as data['code']/data['status']/data['reason'] while the relay
            # itself answers 200. We read both so detection works regardless.
            data = body.get('data', {}) if isinstance(body, dict) else {}
            if not isinstance(data, dict):
                data = {}
            platform = data.get('platform', 'Unknown platform')
            reason = data.get('reason')
            provider_code = data.get('code', data.get('status'))
            try:
                details = data['body']['_content']['error']['message']
            except (KeyError, TypeError):
                details = None
            if provider_code is None:
                try:
                    provider_code = data['body']['code']
                except (KeyError, TypeError):
                    provider_code = None

            too_large = (_is_payload_too_large(r.code, reason, details,
                                               raw_body.decode('utf-8', 'replace') if raw_body else '')
                         or _is_payload_too_large(provider_code, reason, details))

            # Payload too large: retry ONCE without the message body. Only for
            # MessageEvents that actually carried content (conference invites
            # etc. have none). allow_strip_retry guards against loops.
            if too_large and allow_strip_retry and getattr(payload, 'content', ''):
                log.info('Push payload too large for %s/%s (http=%s provider=%s) — retrying without message body' %
                         (payload.to, destination, r.code, provider_code))
                stripped = _message_payload_without_content(payload)
                yield _send_push_notification(stripped, destination, token, allow_strip_retry=False)
                return

            if r.code != 200:
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
                            tokens.remove(destination, payload.app_id, payload.device_id)
                else:
                    log.warning('Error sending %s push notification to %s/%s: %s (%s) %s %s' % (platform.title(), payload.to, destination, token[:15], r.phrase.decode(), r.code, error_description))
            else:
                log.info('Sent %s push notify for %s to %s/%s' % (platform.title(), payload.to, destination, token[:15]))
    else:
        log.warning('Cannot send push notification: no Sylk push server configured')
