
"""Radio PTT relay on the conference focus (docs/radio-ptt.md).

A participant whose chat stream advertises CONTENT_TYPE in its
a=accept-wrapped-types drives a two-way radio. Other participants send
press/release commands to the room; the focus forwards them to that
radio endpoint and publishes the radio's on-air state, as reported by the
endpoint, in conference-info (<agp-conf:radio_ptt> on the endpoint).

There is no floor control: anyone may press or release any radio at any
time. The focus only relays, tracks state and runs a watchdog.

Runs in the twisted reactor thread, like Room.handle_notification.
"""

import json
import time

from twisted.internet import reactor

from sylk.applications.conference.logger import log
from sylk.payloads.conference_info_extensions import RadioPTT


__all__ = ['CONTENT_TYPE', 'RadioPTTController']


CONTENT_TYPE = 'application/vnd.agp.radio-ptt+json'
FEATURE_TAG = '+g.agp.radio-ptt'   # RFC 3840 Contact feature tag

WATCHDOG_TIMEOUT = 5    # seconds from forwarding a press to the endpoint reporting transmitting/error
ERROR_HOLD_TIME = 3     # seconds an error state stays published before falling back to idle

STATES = ('idle', 'keying', 'transmitting', 'error')


def _str(value):
    return value.decode() if isinstance(value, bytes) else (value or '')


def _aor(uri):
    return '%s@%s' % (_str(uri.user), _str(uri.host))


class RadioState(object):
    __slots__ = ('state', 'command_id', 'by', 'since', 'timeout', 'reason', 'watchdog', 'reset_timer')

    def __init__(self):
        self.state = 'idle'
        self.command_id = None
        self.by = None
        self.since = time.monotonic()
        self.timeout = None
        self.reason = None
        self.watchdog = None
        self.reset_timer = None

    def cancel_timers(self):
        for name in ('watchdog', 'reset_timer'):
            timer = getattr(self, name)
            if timer is not None and timer.active():
                timer.cancel()
            setattr(self, name, None)


class RadioPTTController(object):
    def __init__(self, room):
        self.room = room
        self.radios = {}  # session -> RadioState

    # -- capability --------------------------------------------------------

    @staticmethod
    def chat_stream(session):
        return next((stream for stream in session.streams or [] if stream.type == 'chat'), None)

    @staticmethod
    def has_feature_tag(session):
        try:
            parameters = session._invitation.remote_contact_header.parameters
        except AttributeError:
            return False
        return any((name.decode() if isinstance(name, bytes) else name).lower() == FEATURE_TAG for name in parameters)

    @classmethod
    def advertises(cls, session):
        """Either the Contact feature tag or the content type listed in the
        chat stream's a=accept-wrapped-types. A chat stream is required in
        both cases, it carries the commands."""
        stream = cls.chat_stream(session)
        if stream is None:
            return None
        if cls.has_feature_tag(session):
            return 'contact feature tag'
        wrapped = [t.lower() for t in getattr(stream, 'remote_accept_wrapped_types', None) or []]
        if CONTENT_TYPE in wrapped:
            return 'accept-wrapped-types'
        return None

    def _label(self, session):
        pid = getattr(session, '_sylk_participant_id', None)
        return '%s%s' % (_aor(session.remote_identity.uri), ' (%s)' % pid if pid else '')

    def session_added(self, session):
        if session in self.radios:
            return
        signal = self.advertises(session)
        if not signal:
            return
        self.radios[session] = RadioState()
        log.info('Room %s - radio PTT %s: radio endpoint joined (%s)' % (self.room.name, self._label(session), signal))

    def session_removed(self, session):
        radio = self.radios.pop(session, None)
        if radio is None:
            return
        radio.cancel_timers()
        log.info('Room %s - radio PTT %s: %s -> gone (left)%s' % (
            self.room.name, self._label(session), radio.state,
            ' id=%s' % radio.command_id if radio.command_id else ''))

    def stop(self):
        for radio in self.radios.values():
            radio.cancel_timers()
        self.radios.clear()

    # -- conference-info ---------------------------------------------------

    def decorate_endpoint(self, session, endpoint):
        radio = self.radios.get(session)
        if radio is None:
            return
        element = RadioPTT(radio.state)
        if radio.state in ('keying', 'transmitting'):
            element.by = radio.by
            element.elapsed = str(int(time.monotonic() - radio.since))
        if radio.state == 'transmitting' and radio.timeout is not None:
            element.timeout = str(radio.timeout)
        if radio.state in ('error', 'idle') and radio.reason:
            element.reason = radio.reason
        endpoint.radio_ptt = element

    # -- messages ----------------------------------------------------------

    def handle_message(self, session, message):
        """Returns the (code, reason) for the MSRP report."""
        content = message.content
        if isinstance(content, bytes):
            content = content.decode('utf-8', 'replace')
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            log.warning('Room %s - radio PTT: malformed message from %s: %r' % (self.room.name, _aor(session.remote_identity.uri), content))
            return 400, 'Bad Request'
        if 'action' in payload:
            return self._handle_request(session, payload)
        if 'state' in payload:
            return self._handle_report(session, payload)
        return 400, 'Bad Request'

    def _find_target(self, endpoint):
        if not endpoint:
            return None
        endpoint = str(endpoint)
        for session in self.radios:
            if endpoint == getattr(session, '_sylk_participant_id', None):
                return session
            try:
                if endpoint == str(session._invitation.remote_contact_header.uri):
                    return session
            except Exception:
                pass
        # A bare AoR is accepted when it identifies exactly one radio.
        bare = endpoint.split(':', 1)[1] if endpoint.startswith(('sip:', 'sips:')) else endpoint
        matches = [s for s in self.radios if _aor(s.remote_identity.uri) == bare]
        return matches[0] if len(matches) == 1 else None

    def _handle_request(self, session, payload):
        action = payload.get('action')
        command_id = payload.get('id')
        requester = _aor(session.remote_identity.uri)
        if action not in ('press', 'release'):
            return 400, 'Bad Request'
        target = self._find_target(payload.get('endpoint'))
        if target is None:
            log.info('Room %s - radio PTT %s from %s id=%s: no radio endpoint %s' % (
                self.room.name, action, requester, command_id, payload.get('endpoint')))
            return 404, 'Not Found'
        stream = self.chat_stream(target)
        if stream is None:
            return 404, 'Not Found'
        forward = {'action': action, 'id': command_id, 'by': requester}
        try:
            stream.send_message(json.dumps(forward), CONTENT_TYPE, sender=self.room.identity, recipients=[self.room.identity])
        except Exception as e:
            log.warning('Room %s - radio PTT %s: could not forward %s: %s' % (self.room.name, self._label(target), action, e))
            return 500, 'Internal Error'
        log.info('Room %s - radio PTT %s: %s id=%s by=%s forwarded' % (self.room.name, self._label(target), action, command_id, requester))
        radio = self.radios[target]
        if action == 'press':
            if radio.state in ('idle', 'error'):
                self._transition(target, radio, 'keying', command_id=command_id, by=requester)
                radio.watchdog = reactor.callLater(WATCHDOG_TIMEOUT, self._watchdog_expired, target, command_id)
            else:
                # Already keying/on air: the endpoint extends; latest presser shown.
                radio.command_id, radio.by = command_id, requester
                self.room.dispatch_conference_info()
        return 200, 'OK'

    def _handle_report(self, session, payload):
        radio = self.radios.get(session)
        if radio is None:
            # Capability seen only now (e.g. the stream was added later).
            radio = self.radios[session] = RadioState()
            log.info('Room %s - radio PTT %s: radio endpoint announced itself' % (self.room.name, self._label(session)))
        state = payload.get('state')
        if state not in STATES:
            return 400, 'Bad Request'
        command_id = payload.get('id')
        reason = payload.get('reason')
        timeout = payload.get('timeout')
        try:
            timeout = int(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout = None

        if state == 'transmitting':
            if radio.watchdog is not None and radio.watchdog.active():
                radio.watchdog.cancel()
            radio.watchdog = None
            if command_id is None:
                by = 'local'
            elif command_id == radio.command_id and radio.by:
                by = radio.by
            else:
                by = radio.by or 'unknown'
            if radio.state == 'transmitting':
                # Extension of an ongoing transmission: restart the countdown.
                radio.command_id, radio.by, radio.timeout = command_id, by, timeout
                radio.since = time.monotonic()
                log.info('Room %s - radio PTT %s: transmitting extended id=%s by=%s' % (self.room.name, self._label(session), command_id, by))
                self.room.dispatch_conference_info()
            else:
                self._transition(session, radio, 'transmitting', command_id=command_id, by=by, timeout=timeout)
        elif state == 'keying':
            if radio.state != 'keying':
                self._transition(session, radio, 'keying', command_id=command_id, by=radio.by)
        elif state == 'idle':
            if radio.state != 'idle':
                self._transition(session, radio, 'idle', command_id=command_id, reason=reason)
        elif state == 'error':
            self._error(session, radio, reason or 'error', command_id)
        return 200, 'OK'

    # -- state -------------------------------------------------------------

    def _transition(self, session, radio, new_state, command_id=None, by=None, timeout=None, reason=None):
        old_state = radio.state
        duration = time.monotonic() - radio.since
        if new_state == 'idle':
            radio.cancel_timers()
        radio.state = new_state
        radio.since = time.monotonic()
        radio.command_id = command_id
        radio.by = by if new_state in ('keying', 'transmitting') else None
        radio.timeout = timeout if new_state == 'transmitting' else None
        radio.reason = reason
        details = []
        if command_id:
            details.append('id=%s' % command_id)
        if radio.by:
            details.append('by=%s' % radio.by)
        if reason:
            details.append('reason=%s' % reason)
        log.info('Room %s - radio PTT %s: %s -> %s %s(%.2fs)' % (
            self.room.name, self._label(session), old_state, new_state,
            ' '.join(details) + ' ' if details else '', duration))
        self.room.dispatch_conference_info()

    def _error(self, session, radio, reason, command_id):
        radio.cancel_timers()
        self._transition(session, radio, 'error', command_id=command_id, reason=reason)
        radio.reset_timer = reactor.callLater(ERROR_HOLD_TIME, self._error_expired, session)

    def _error_expired(self, session):
        radio = self.radios.get(session)
        if radio is None or radio.state != 'error':
            return
        radio.reset_timer = None
        self._transition(session, radio, 'idle', command_id=radio.command_id, reason=radio.reason)

    def _watchdog_expired(self, session, command_id):
        radio = self.radios.get(session)
        if radio is None:
            return
        radio.watchdog = None
        if radio.state == 'keying' and radio.command_id == command_id:
            log.warning('Room %s - radio PTT %s: no confirmation within %ds for id=%s' % (
                self.room.name, self._label(session), WATCHDOG_TIMEOUT, command_id))
            self._error(session, radio, 'no_response', command_id)
