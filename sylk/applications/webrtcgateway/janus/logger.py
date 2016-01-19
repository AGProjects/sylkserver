
"""
Logging support for Janus traffic.
"""

__all__ = ["Logger"]

import os
import sys

from application.system import makedirs
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread


class Logger(object):
    def __init__(self):
        self.stopped = False
        self._janustrace_filename = None
        self._janustrace_file = None
        self._janustrace_error = False
        self._janustrace_start_time = None
        self._janustrace_packet_count = 0

        self._log_directory_error = False

    def start(self):
        # try to create the log directory
        try:
            self._init_log_directory()
            self._init_log_file()
        except Exception:
            pass
        self.stopped = False

    def stop(self):
        self.stopped = True
        if self._janustrace_file is not None:
            self._janustrace_file.close()
            self._janustrace_file = None

    def msg(self, direction, timestamp, packet):
        if self._janustrace_start_time is None:
            self._janustrace_start_time = timestamp
        self._janustrace_packet_count += 1
        buf = ["%s: Packet %d, +%s" % (direction, self._janustrace_packet_count, (timestamp - self._janustrace_start_time))]
        buf.append(packet)
        buf.append('--')
        message = '\n'.join(buf)
        self._process_log((message, timestamp))

    @run_in_thread('log-io')
    def _process_log(self, record):
        if self.stopped:
            return
        message, timestamp = record
        try:
            self._init_log_file()
        except Exception:
            pass
        else:
            self._janustrace_file.write('%s [%s %d]: %s\n' % (timestamp, os.path.basename(sys.argv[0]).rstrip('.py'), os.getpid(), message))
            self._janustrace_file.flush()

    def _init_log_directory(self):
        settings = SIPSimpleSettings()
        log_directory = settings.logs.directory.normalized
        try:
            makedirs(log_directory)
        except Exception, e:
            if not self._log_directory_error:
                print "failed to create logs directory '%s': %s" % (log_directory, e)
                self._log_directory_error = True
            self._janustrace_error = True
            raise
        else:
            self._log_directory_error = False
            if self._janustrace_filename is None:
                self._janustrace_filename = os.path.join(log_directory, 'janus_trace.log')
                self._janustrace_error = False

    def _init_log_file(self):
        if self._janustrace_file is None:
            self._init_log_directory()
            filename = self._janustrace_filename
            try:
                self._janustrace_file = open(filename, 'a')
            except Exception, e:
                if not self._janustrace_error:
                    print "failed to create log file '%s': %s" % (filename, e)
                    self._janustrace_error = True
                raise
            else:
                self._janustrace_error = False

