
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

    def build(self, size, sample_rate):
        """Create the mixer pool. size 0 means one mixer per CPU core. The
        first mixer is the process voice mixer (set up by server.py); pass it
        in via prime() so we don't create a duplicate for it."""
        if self.mixers:
            return
        if size <= 0:
            import os
            size = os.cpu_count() or 1
        # mixers[0] is primed from the existing voice mixer (see prime()); only
        # create the extra ones here.
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


mixer_pool = MixerPool()


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
        # At this point the stream is freshly created and not yet started, so
        # its bridge only holds its own device. Replace all three with new
        # objects bound to the chosen mixer.
        stream.mixer = mixer
        stream.bridge = AudioBridge(mixer)
        stream.device = AudioDevice(mixer)
        stream.bridge.add(stream.device)
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
