"""
memdiag.py -- runtime memory-leak diagnostics for SylkServer.

Drop-in, dependency-light (tracemalloc + gc are stdlib; objgraph is optional).
It logs, on a fixed cadence, the three numbers that together tell you whether a
leak is in Python or in the native (PJSIP/sipsimple) layer:

    RSS            total resident memory of the process   (what MRTG graphs)
    tracemalloc    bytes currently allocated by *Python*  (CPython heap only)
    gc objects     number of live Python objects

Reading the output
------------------
  * RSS climbs AND tracemalloc/gc climb together  -> Python-level leak.
    The "TOP GROWTH" tracemalloc diff points at the exact file:line that is
    accumulating, and the per-type counts show which objects pile up
    (e.g. Room / AudioStream / WavePlayer not being collected).

  * RSS climbs but tracemalloc and gc stay flat   -> NATIVE leak.
    Python isn't holding the memory; it's pjmedia / the custom
    AudioMixer.get_signal_level() helper / RTP buffers. That exonerates the
    application code and points at the rebuilt python3-sipsimple core.

Wiring it in (pick one)
-----------------------
1. Best accuracy -- start tracemalloc before the interpreter allocates much.
   Launch the server with the env var set:

       PYTHONTRACEMALLOC=25 SYLK_MEMDIAG=1 ./sylk-server --no-fork ...

   With SYLK_MEMDIAG=1 this module auto-arms on import (see bottom). Then add
   near the top of the `sylk-server` script (after imports):

       import memdiag  # noqa

2. Explicit call -- add one line where the reactor is about to run, e.g. in
   ConferenceApplication.start() or in `sylk-server`:

       import memdiag; memdiag.start()

Either way it self-installs onto the Twisted reactor and writes to
/var/log/sylkserver/memdiag.log (override with SYLK_MEMDIAG_LOG).

Tunables (env vars)
-------------------
  SYLK_MEMDIAG_INTERVAL   seconds between reports        (default 120)
  SYLK_MEMDIAG_TOP        tracemalloc lines to show      (default 15)
  SYLK_MEMDIAG_FRAMES     tracemalloc traceback depth    (default 25)
  SYLK_MEMDIAG_LOG        log file path
  SYLK_MEMDIAG_TYPES      comma list of class names to count
                          (default Room,Session,AudioStream,WavePlayer,
                           ChatStream,FileTransferHandler,_Subscription)

The heavy walk (tracemalloc snapshot + gc scan) runs in a worker thread via
reactor.callInThread so it never adds jitter to the audio reactor loop.
"""

import gc
import os
import sys
import threading
import time
import tracemalloc

try:
    import objgraph  # optional: pip3 install objgraph
except Exception:
    objgraph = None

_TRACK_DEFAULT = ('Room', 'Session', 'AudioStream', 'WavePlayer',
                  'ChatStream', 'FileTransferHandler', '_Subscription')

_state = {'started': False, 'prev_snapshot': None, 't0': None, 'rss0': None}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _rss_bytes():
    """Resident set size in bytes, no psutil dependency."""
    try:
        with open('/proc/self/statm') as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE')
    except Exception:
        try:
            import resource
            kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KiB, macOS reports bytes.
            return kb if sys.platform == 'darwin' else kb * 1024
        except Exception:
            return 0


def _human(n):
    n = float(n)
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if abs(n) < 1024.0:
            return '%6.1f%s' % (n, unit)
        n /= 1024.0
    return '%.1fP' % n


def _logger():
    """Use SylkServer's logging if importable, else a plain file."""
    path = os.environ.get('SYLK_MEMDIAG_LOG', '/var/log/sylkserver/memdiag.log')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        path = os.path.join(os.getcwd(), 'memdiag.log')

    def emit(msg):
        line = '%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
        try:
            with open(path, 'a') as f:
                f.write(line)
        except Exception:
            sys.stderr.write(line)
    return emit


def _type_counts(names):
    """Count live instances by class name straight from the gc graph.
    Avoids objgraph so it works even when objgraph isn't installed."""
    wanted = set(names)
    counts = {n: 0 for n in names}
    for obj in gc.get_objects():
        try:
            cn = type(obj).__name__
        except Exception:
            continue
        if cn in wanted:
            counts[cn] += 1
    return counts


# --------------------------------------------------------------------------- #
# the periodic report (runs in a worker thread)
# --------------------------------------------------------------------------- #
def _collect_and_log(emit, top_n, track_types):
    gc.collect()  # force a full collection so we measure true survivors

    rss = _rss_bytes()
    snapshot = tracemalloc.take_snapshot()
    traced_cur, traced_peak = tracemalloc.get_traced_memory()
    n_objects = len(gc.get_objects())
    n_garbage = len(gc.garbage)  # uncollectable cycles -> a real red flag

    if _state['t0'] is None:
        _state['t0'] = time.time()
        _state['rss0'] = rss
    elapsed = time.time() - _state['t0']
    rss_delta = rss - _state['rss0']

    counts = _type_counts(track_types)

    emit('==== memdiag report (uptime %ds) ===='
         % int(elapsed))
    emit('  RSS          = %s   (delta since start %s%s)'
         % (_human(rss), '+' if rss_delta >= 0 else '-', _human(abs(rss_delta))))
    emit('  tracemalloc  = %s   (Python heap; peak %s)'
         % (_human(traced_cur), _human(traced_peak)))
    emit('  gc objects   = %d        gc.garbage = %d' % (n_objects, n_garbage))
    emit('  >> if RSS climbs while tracemalloc/gc stay flat, the leak is NATIVE '
         '(pjsip/sipsimple), not Python.')
    emit('  live instances: '
         + '  '.join('%s=%d' % (k, counts[k]) for k in track_types))

    # tracemalloc growth diff -- the money shot for a Python-side leak.
    prev = _state['prev_snapshot']
    if prev is not None:
        diff = snapshot.compare_to(prev, 'lineno')
        emit('  -- TOP %d GROWTH since last report (by size_diff) --' % top_n)
        for stat in diff[:top_n]:
            emit('     %+9s  (%+d blocks)  %s'
                 % (_human(stat.size_diff), stat.count_diff, stat))
    else:
        top = snapshot.statistics('lineno')
        emit('  -- TOP %d ALLOCATIONS (baseline; no diff yet) --' % top_n)
        for stat in top[:top_n]:
            emit('     %9s  (%d blocks)  %s'
                 % (_human(stat.size), stat.count, stat))
    _state['prev_snapshot'] = snapshot

    # objgraph growth, if available -- catches types we didn't name explicitly.
    if objgraph is not None:
        try:
            growth = objgraph.growth(limit=10)
            if growth:
                emit('  -- objgraph type growth since last call --')
                for name, total, delta in growth:
                    emit('     %-32s %8d  (%+d)' % (name, total, delta))
        except Exception as e:
            emit('  objgraph growth failed: %r' % e)

    emit('')  # blank line between reports


# --------------------------------------------------------------------------- #
# public entrypoint
# --------------------------------------------------------------------------- #
def start():
    """Idempotent. Arms tracemalloc (if not already) and schedules the
    periodic report on the Twisted reactor."""
    if _state['started']:
        return
    _state['started'] = True

    frames = int(os.environ.get('SYLK_MEMDIAG_FRAMES', '25'))
    if not tracemalloc.is_tracing():
        tracemalloc.start(frames)

    interval = float(os.environ.get('SYLK_MEMDIAG_INTERVAL', '120'))
    top_n = int(os.environ.get('SYLK_MEMDIAG_TOP', '15'))
    track_types = tuple(
        t.strip() for t in
        os.environ.get('SYLK_MEMDIAG_TYPES', ','.join(_TRACK_DEFAULT)).split(',')
        if t.strip()
    )

    emit = _logger()
    emit('memdiag armed: interval=%ss top=%s frames=%s objgraph=%s tracking=%s'
         % (interval, top_n, frames, bool(objgraph), ','.join(track_types)))

    from twisted.internet import reactor
    from twisted.internet.task import LoopingCall

    def tick():
        # Do the heavy walk off the reactor so audio timing is untouched.
        reactor.callInThread(_collect_and_log, emit, top_n, track_types)

    lc = LoopingCall(tick)

    def arm():
        # now=False: skip the t=0 sample, let the process settle first.
        lc.start(interval, now=False)
        emit('memdiag scheduled on reactor (first report in %ss)' % interval)
        # Start the tcmalloc heap profiler now -- AFTER all native dlopen()s
        # are done -- to dodge the profiler-vs-dynamic-linker deadlock that
        # bites when HEAPPROFILE is set at launch.
        _tcmalloc_profiler_setup(emit)

    reactor.callWhenRunning(arm)
    _state['loopingcall'] = lc


def _tcmalloc_profiler_setup(emit):
    """If SYLK_TCMALLOC_PROFILE is set, start tcmalloc's heap profiler via its
    C API (HeapProfilerStart) now that the process is fully up, and dump on a
    timer. Requires libtcmalloc(_and_profiler).so to be LD_PRELOADed, but with
    HEAPPROFILE *unset* so it doesn't auto-start during dlopen and hang."""
    prefix = os.environ.get('SYLK_TCMALLOC_PROFILE', '').strip()
    if not prefix:
        return
    import ctypes
    try:
        lib = ctypes.CDLL(None)          # global syms incl. the preloaded tcmalloc
        start_fn = lib.HeapProfilerStart
        dump_fn = lib.HeapProfilerDump
    except AttributeError:
        emit('tcmalloc: HeapProfilerStart not found -- LD_PRELOAD '
             'libtcmalloc_and_profiler.so.4 (the profiler build), not _minimal')
        return
    start_fn.argtypes = [ctypes.c_char_p]
    dump_fn.argtypes = [ctypes.c_char_p]
    try:
        start_fn(prefix.encode())
    except Exception as e:
        emit('tcmalloc: HeapProfilerStart failed: %r' % e)
        return
    emit('tcmalloc: heap profiler started AFTER startup (prefix=%s) '
         '-- dlopen deadlock avoided' % prefix)

    interval = float(os.environ.get('SYLK_TCMALLOC_DUMP_INTERVAL', '300'))
    n = {'i': 0}
    from twisted.internet.task import LoopingCall

    def dump():
        n['i'] += 1
        try:
            dump_fn(('periodic-%d' % n['i']).encode())
            emit('tcmalloc: heap dump #%d written (%s.NNNN.heap)' % (n['i'], prefix))
        except Exception as e:
            emit('tcmalloc: dump failed: %r' % e)

    lc = LoopingCall(dump)
    lc.start(interval, now=False)
    _state['tcmalloc_lc'] = lc


def _deferred_autostart():
    """Wait until SylkServer has installed AND started its OWN reactor, then
    arm. We must NOT import twisted.internet.reactor ourselves at import time:
    doing so installs the default reactor before sipsimple/eventlib wire up
    theirs, which deadlocks startup. So we only *observe* sys.modules and act
    once the application's reactor is up and running.
    """
    deadline = time.time() + 600  # give startup up to 10 min, then give up
    while time.time() < deadline:
        mod = sys.modules.get('twisted.internet.reactor')
        if mod is not None and getattr(mod, 'running', False):
            try:
                mod.callFromThread(start)  # arm on the reactor thread
            except Exception:
                pass
            return
        time.sleep(1.0)


# Auto-arm when launched with SYLK_MEMDIAG=1, so a bare `import memdiag` is
# enough and you don't have to touch any call site. The watcher runs in a
# daemon thread and never touches the reactor until the app's own reactor is
# already running -- safe to `import memdiag` anywhere, even at the very top
# of sylk-server.
if os.environ.get('SYLK_MEMDIAG', '').strip() in ('1', 'true', 'yes', 'on'):
    threading.Thread(target=_deferred_autostart, name='memdiag-watcher',
                     daemon=True).start()
