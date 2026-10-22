
"""Periodic per-call RTP statistics logging for SylkServer applications"""

import os

from datetime import datetime
from time import monotonic

from eventlib import api, proc

__all__ = 'RTPStatisticsLogger',


class RTPStatisticsLogger(object):
    """Appends one line per audio/video stream every `interval` seconds:

    <time> <call-id> <caller> <type> RTT=.. ms, loss=..%, RX/TX=../.. kbps, <local> <-> <remote>
    """

    def __init__(self, session, caller, logfile, interval=5, log=None):
        self.session = session
        self.call_id = session.call_id
        self.caller = caller
        self.logfile = logfile
        self.interval = max(int(interval or 5), 1)
        self.log = log
        self._last_bytes = {}
        self._proc = None

    @classmethod
    def start_for(cls, session, caller, enabled, logfile, interval, log=None):
        """Return a running logger, or None if disabled / not possible"""
        if not enabled or not logfile:
            return None
        logfile = os.path.expanduser(logfile)
        try:
            os.makedirs(os.path.dirname(logfile), exist_ok=True)
        except OSError as e:
            if log is not None:
                log.warning('Cannot create RTP statistics directory for %s: %s' % (logfile, e))
            return None
        logger = cls(session, caller, logfile, interval, log)
        logger.start()
        return logger

    def start(self):
        if self._proc is None:
            self._proc = proc.spawn(self._run)

    def stop(self):
        if self._proc is not None:
            self._proc.kill()
            self._proc = None
        self.session = None

    @staticmethod
    def _bytes(direction):
        # sipsimple statistics may use 'bytes' or 'octets' depending on version
        return direction.get('bytes', direction.get('octets', 0)) or 0

    def _bandwidth_kbps(self, stream, stats):
        key = id(stream)
        rx_now = self._bytes(stats['rx'])
        tx_now = self._bytes(stats['tx'])
        rx_prev, tx_prev, t_prev = self._last_bytes.get(key, (rx_now, tx_now, None))
        t_now = monotonic()
        self._last_bytes[key] = (rx_now, tx_now, t_now)
        if t_prev is None:
            return 0.0, 0.0
        dt = max(t_now - t_prev, 0.001)
        return (rx_now - rx_prev) * 8 / 1000.0 / dt, (tx_now - tx_prev) * 8 / 1000.0 / dt

    def _lines(self, session):
        ts = datetime.now().replace(microsecond=0)
        lines = []
        seen = set()
        for stream in session.streams or []:
            if stream.type not in ('audio', 'video'):
                continue
            stats = stream.statistics
            if stats is None:
                continue
            seen.add(id(stream))
            rx_kbps, tx_kbps = self._bandwidth_kbps(stream, stats)
            rx_pkts = stats['rx']['packets']
            loss = 100.0 * stats['rx']['packets_lost'] / rx_pkts if rx_pkts else 0
            lines.append('%s %s %s %s RTT=%4d ms, loss=%5.1f%%, RX/TX=%4.0f/%4.0f kbps, %s:%s <-> %s:%s\n' % (
                ts, self.call_id, self.caller, stream.type, stats['rtt']['avg'] / 1000, loss, rx_kbps, tx_kbps,
                stream.local_rtp_address, stream.local_rtp_port, stream.remote_rtp_address, stream.remote_rtp_port))
        for key in list(self._last_bytes):
            if key not in seen:
                del self._last_bytes[key]
        return lines

    def _run(self):
        try:
            while self.session is not None:
                api.sleep(self.interval)
                session = self.session
                if session is None:
                    break
                lines = self._lines(session)
                if not lines:
                    continue
                try:
                    with open(self.logfile, 'a') as f:
                        f.writelines(lines)
                except OSError as e:
                    if self.log is not None:
                        self.log.warning('Cannot write RTP statistics to %s: %s' % (self.logfile, e))
                    break
        except proc.ProcExit:
            pass
        finally:
            self._proc = None
