
"""
Multi-core audio: a pool of AudioMixers so media work uses all CPU cores.

Each sipsimple AudioMixer (created device-less) starts its own pjmedia
master-port clock thread, and the mixing + codec work for that bridge runs in
that thread in C with the GIL released. So running N mixers spreads the media
load across N CPU cores inside this single SylkServer process - the Twisted
reactor (SIP signalling) stays on one core, which is cheap.

Usage:
    from sylk.audio import mixer_pool, assign_stream_mixer

    mixer_pool.build(size, sample_rate)          # once, at startup
    m = mixer_pool.pick()                         # per call (round-robin)
    m = mixer_pool.by_key(room_uri)               # per room (stable affinity)
    assign_stream_mixer(audio_stream, m)          # BEFORE session.accept()

A single point-to-point call or conference room must live entirely on one
mixer (an AudioBridge requires all its ports to share one mixer), so we
distribute *across* calls/rooms rather than splitting one.
"""

import hashlib
from threading import Lock, RLock

from application import log
from application.python.types import Singleton
from sipsimple.audio import AudioBridge, AudioConference, AudioDevice, RootAudioBridge
from sipsimple.core import AudioMixer

__all__ = ('mixer_pool', 'assign_stream_mixer', 'PooledAudioConference')


class MixerPool(object, metaclass=Singleton):
    def __init__(self):
        self.mixers = []          # the AudioMixer instances (one clock thread each)
        self._roots = []          # keep RootAudioBridge + AudioDevice refs alive
        self._rr = 0              # round-robin cursor
        self._lock = Lock()
        self._built = False

    def build(self, size, sample_rate):
        """Create the mixer pool. size 0 means one mixer per CPU core. mixers[0]
        is the process voice mixer (registered first via prime()); this only
        creates the *additional* mixers up to the requested size."""
        if self._built:
            return
        self._built = True
        if size <= 0:
            import os
            # Auto: one mixer per core, but always leave at least one core for
            # the Twisted reactor (SIP signalling) so heavy media load on the
            # mixer clock threads can't starve call setup.
            size = max(1, (os.cpu_count() or 1) - 1)
        # mixers[0] is already present (primed from the voice mixer); create the
        # remaining mixers until the pool reaches the requested size.
        while len(self.mixers) < size:
            mixer = AudioMixer(None, None, sample_rate, 0, 9999)
            device = AudioDevice(mixer)
            bridge = RootAudioBridge(mixer)
            bridge.add(device)
            self.mixers.append(mixer)
            self._roots.append((bridge, device))

    def prime(self, mixer):
        """Register the already-created process voice mixer as mixers[0] so it
        participates in the pool instead of being a separate, unused mixer."""
        if mixer is not None and mixer not in self.mixers:
            self.mixers.insert(0, mixer)

    @property
    def size(self):
        return len(self.mixers)

    def pick(self):
        """Round-robin selection for independent calls (playback / echo)."""
        if not self.mixers:
            return None
        with self._lock:
            mixer = self.mixers[self._rr % len(self.mixers)]
            self._rr += 1
            return mixer

    def by_key(self, key):
        """Stable hash selection so all participants of one conference room (and
        the room's AudioConference) land on the same mixer."""
        if not self.mixers:
            return None
        digest = hashlib.md5(str(key).encode('utf-8')).hexdigest()
        return self.mixers[int(digest, 16) % len(self.mixers)]

    def load_summary(self):
        """Per-mixer live load as 'used_slot_count' (the number of pjmedia ports
        on each mixer's conference bridge). Read straight from the core, so it
        can't drift. It's a proxy for calls (each established call adds a few
        slots), but it tracks how work is distributed across the cores."""
        counts = []
        for m in self.mixers:
            try:
                counts.append(m.used_slot_count)
            except Exception:
                counts.append(0)
        return '%s total %d' % (','.join(str(c) for c in counts), sum(counts))


mixer_pool = MixerPool()


def _resolve_application_name(request_uri, headers):
    """Resolve which application a call is for using SylkServer's OWN routing
    (the X-Sylk-App header, then the application map, then the default app) so
    the mixer factory always agrees with how the call is actually handled.

    get_application's map matching expects str user/host (SylkServer normally
    calls it with the decoded session.request_uri), but at stream birth we have
    the raw invitation Request-URI with bytes fields -- so pass a decoded shim.
    """
    from types import SimpleNamespace
    from sylk.applications import IncomingRequestHandler
    uri = SimpleNamespace(
        user=request_uri.user.decode() if isinstance(request_uri.user, bytes) else (request_uri.user or ''),
        host=request_uri.host.decode() if isinstance(request_uri.host, bytes) else (request_uri.host or ''),
    )
    app = IncomingRequestHandler().get_application(uri, headers or {})
    return getattr(app, '__appname__', None)


def conference_room_key(uri):
    """Canonical 'user@host' key for a conference room, used by BOTH the mixer
    factory (at stream birth, given the INVITE Request-URI) and the Room (given
    its self.uri string), so a room and its participants always hash to the same
    mixer. Accepts a SIP URI object or a string. Decoded, case preserved (to
    match Room.uri), and any URI parameters are stripped -- e.g.
    '222222222222@conference.sip2sip.info;a=b' -> '222222222222@conference.sip2sip.info'.
    """
    if isinstance(uri, (str, bytes)):
        s = uri.decode() if isinstance(uri, bytes) else uri
    else:
        user = uri.user.decode() if isinstance(uri.user, bytes) else (uri.user or '')
        host = uri.host.decode() if isinstance(uri.host, bytes) else (uri.host or '')
        s = '%s@%s' % (user, host)
    # Drop any URI parameters (';transport=...', ';a=b', ...), trim whitespace,
    # and lower-case so equivalent room URIs map to one mixer regardless of case.
    return s.split(';', 1)[0].strip().lower()


def stream_mixer_factory(request_uri, headers=None):
    """RTPStream.mixer_factory: pick the mixer a call's audio is born on.

    Playback and echo are independent calls -> round-robin across the pool (one
    core each). Conference -> hashed by the room URI (by_key), so every
    participant of a room is born on that room's mixer and mixes together on one
    core, while different rooms spread across cores. With an external IVR the
    Request-URI is already the real room, so this is correct from the start.
    (The built-in select_conference IVR carries the selector URI, not the room,
    so those legs land on the wrong mixer for now and are guarded in the welcome
    handler -- to be solved later.) Everything else stays on the voice mixer.
    No-op when the pool is disabled; never raises (falls back to voice mixer).
    """
    if mixer_pool.size <= 1 or request_uri is None:
        return None
    try:
        app = _resolve_application_name(request_uri, headers)
        if app in ('playback', 'echo'):
            return mixer_pool.pick()
        if app == 'conference':
            return mixer_pool.by_key(conference_room_key(request_uri))
    except Exception:
        return None
    return None


def assign_stream_mixer(stream, mixer):
    """Rebind an AudioStream (and its bridge + device) to a specific pool
    mixer. MUST be called before the stream is started (i.e. before
    session.accept()/accept_proposal()), because the codec engine
    (AudioTransport) is created from stream.mixer at start time.

    No-op if the pool is disabled (mixer is None) or the stream is already on
    the requested mixer. Best-effort and defensive: never let a redirect break
    call setup."""
    if mixer is None or stream is None:
        return
    try:
        if getattr(stream, 'mixer', None) is mixer:
            return
        # AudioStream.__init__ already built a bridge (with live multiplexer /
        # demultiplexer pjmedia ports) on the default voice mixer. Build the
        # replacement on the chosen mixer, swap it in, then EXPLICITLY stop the
        # old bridge in this (reactor) thread.
        #
        # Stopping it explicitly is essential: AudioBridge.stop() disconnects
        # the slot connections first and then removes the ports via the
        # deferred-safe path, all under the conference-bridge lock. If we leave
        # the old bridge to be reaped by Python GC instead, its ports get torn
        # down at an unpredictable moment, in the wrong order, racing the
        # mixer's clock thread -> SIGSEGV in get_frame()/write_port(). (That
        # also explains debris piling up on mixer #0, the default mixer every
        # stream starts on.)
        old_bridge = stream.bridge
        new_bridge = AudioBridge(mixer)
        new_device = AudioDevice(mixer)
        new_bridge.add(new_device)
        stream.mixer = mixer
        stream.bridge = new_bridge
        stream.device = new_device
        if old_bridge is not None:
            try:
                old_bridge.stop()
            except Exception:
                pass
    except Exception:
        # Leave the stream on the default mixer rather than fail the call.
        pass


class PooledAudioConference(AudioConference):
    """AudioConference bound to a chosen pool mixer instead of the global voice
    mixer. Mirrors sipsimple.audio.AudioConference.__init__ exactly, but takes
    the mixer as an argument (the only reason a sipsimple change would
    otherwise be needed)."""

    def __init__(self, mixer):
        self.bridge = RootAudioBridge(mixer)
        self.device = AudioDevice(mixer)
        self.on_hold = False
        self.streams = []
        self._lock = RLock()
        self.bridge.add(self.device)
