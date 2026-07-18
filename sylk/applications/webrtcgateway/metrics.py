
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from sipsimple.threading import run_in_thread
from twisted.internet import defer, reactor, task
from zope.interface import implementer

from .configuration import CassandraConfig, FileStorageConfig
from .logger import log

__all__ = 'Metrics',

FLUSH_INTERVAL = 30
METRIC_NAMES = ('connections', 'registrations', 'accounts', 'sessions', 'sessions_audio', 'sessions_video', 'conferences', 'messages')
COUNTED_MESSAGE_TYPES = ('text/plain', 'text/html')

CASSANDRA_MODULES_AVAILABLE = False
try:
    from cassandra.cqlengine import columns, connection  # noqa: F401
    from cassandra.cqlengine.models import Model         # noqa: F401
except ImportError:
    pass
else:
    CASSANDRA_MODULES_AVAILABLE = True


def _today():
    return datetime.utcnow().strftime('%Y%m%d')


@implementer(IObserver)
class Metrics(object, metaclass=Singleton):
    """Cumulative daily counters (plain event counts, no concurrency).
    Increments are buffered in memory and flushed every FLUSH_INTERVAL
    seconds to Cassandra counter tables or to metrics.json."""

    def __init__(self):
        self._pending = defaultdict(int)   # (metric, day) -> delta
        self._pending_seen = set()         # (metric, day, item)
        self._flusher = task.LoopingCall(self.flush)
        self._use_cassandra = CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points
        self._tables_ready = not self._use_cassandra
        self._file_path = os.path.join(FileStorageConfig.storage_dir.normalized, 'metrics.json')

    def start(self):
        if not self._flusher.running:
            self._flusher.start(FLUSH_INTERVAL, now=False)
        NotificationCenter().add_observer(self, name='VideoroomCreated')
        if self._use_cassandra:
            from .storage import CassandraConnection
            CassandraConnection()   # queue connection setup on the cassandra thread first
            self._create_tables()   # then table sync runs after it, same serialized thread

    def _sync_tables(self):
        os.environ.setdefault('CQLENG_ALLOW_SCHEMA_MANAGEMENT', '1')
        from cassandra.cqlengine.management import sync_table
        from .models.storage.cassandra import MetricsDaily, MetricsDailySeen
        sync_table(MetricsDaily)
        sync_table(MetricsDailySeen)
        self._tables_ready = True
        log.info('metrics tables ready')

    @run_in_thread('cassandra')
    def _create_tables(self):
        try:
            self._sync_tables()
        except Exception as e:
            log.warning('could not create metrics tables (will retry on next flush): %s' % e)

    def stop(self):
        if self._flusher.running:
            self._flusher.stop()
        NotificationCenter().remove_observer(self, name='VideoroomCreated')
        self.flush()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_VideoroomCreated(self, notification):
        self.increment('conferences')

    def increment(self, metric, value=1):
        self._pending[(metric, _today())] += value

    def mark(self, metric, item):
        self._pending_seen.add((metric, _today(), item))

    def count_message(self, content_type):
        if content_type and (content_type in COUNTED_MESSAGE_TYPES or content_type.startswith('application/sylk-file-transfer')):
            self.increment('messages')

    def flush(self):
        pending, self._pending = dict(self._pending), defaultdict(int)
        seen, self._pending_seen = set(self._pending_seen), set()
        if not pending and not seen:
            return
        if self._use_cassandra:
            self._flush_cassandra(pending, seen)
        else:
            self._flush_file(pending, seen)

    def _requeue(self, pending, seen):
        # failed flush: put the deltas back so no counts are lost
        for key, delta in pending.items():
            self._pending[key] += delta
        self._pending_seen.update(seen)

    @run_in_thread('cassandra')
    def _flush_cassandra(self, pending, seen):
        try:
            if not self._tables_ready:
                self._sync_tables()
            from .models.storage.cassandra import MetricsDaily, MetricsDailySeen
            for (metric, day), delta in pending.items():
                MetricsDaily(metric=metric, day=day).update(value=delta)
            for metric, day, item in seen:
                MetricsDailySeen.create(metric=metric, day=day, item=item)
        except Exception as e:
            log.warning('metrics flush failed (will retry): %s' % e)
            reactor.callFromThread(self._requeue, pending, seen)

    @run_in_thread('file-io')
    def _flush_file(self, pending, seen):
        try:
            data = self._load_file()
            for (metric, day), delta in pending.items():
                data.setdefault(metric, {})
                data[metric][day] = data[metric].get(day, 0) + delta
            for metric, day, item in seen:
                data.setdefault(metric + '_seen', {}).setdefault(day, {})[item] = 1
            counter_cutoff = (datetime.utcnow() - timedelta(days=400)).strftime('%Y%m%d')
            seen_cutoff = (datetime.utcnow() - timedelta(days=90)).strftime('%Y%m%d')
            for metric, days in data.items():
                cutoff = seen_cutoff if metric.endswith('_seen') else counter_cutoff
                for day in [d for d in days if d < cutoff]:
                    del days[day]
            os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
            with open(self._file_path, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log.warning('metrics flush failed (will retry): %s' % e)
            reactor.callFromThread(self._requeue, pending, seen)

    def _load_file(self):
        try:
            with open(self._file_path) as f:
                return json.load(f)
        except (OSError, IOError, ValueError):
            return {}

    def get_daily(self, days=30):
        deferred = defer.Deferred()
        start_day = (datetime.utcnow() - timedelta(days=days - 1)).strftime('%Y%m%d')
        day_list = [(datetime.utcnow() - timedelta(days=i)).strftime('%Y%m%d') for i in range(days)]
        counter_metrics = tuple(m for m in METRIC_NAMES if m != 'accounts')

        if self._use_cassandra:
            @run_in_thread('cassandra')
            def query():
                result = {}
                try:
                    from .models.storage.cassandra import MetricsDaily, MetricsDailySeen
                    for metric in counter_metrics:
                        result[metric] = {row.day: int(row.value) for row in
                                          MetricsDaily.objects(MetricsDaily.metric == metric, MetricsDaily.day >= start_day)}
                    accounts = {}
                    for day in day_list:
                        count = MetricsDailySeen.objects(MetricsDailySeen.metric == 'accounts', MetricsDailySeen.day == day).count()
                        if count:
                            accounts[day] = count
                    result['accounts'] = accounts
                except Exception as e:
                    result['error'] = str(e)
                reactor.callFromThread(deferred.callback, result)
            query()
        else:
            @run_in_thread('file-io')
            def query():
                data = self._load_file()
                result = {metric: {day: value for day, value in (data.get(metric) or {}).items() if day >= start_day}
                          for metric in counter_metrics}
                result['accounts'] = {day: len(items) for day, items in (data.get('accounts_seen') or {}).items()
                                      if day >= start_day}
                reactor.callFromThread(deferred.callback, result)
            query()

        def merge_pending(result):
            # fold in not-yet-flushed counters so today is current
            for (metric, day), delta in self._pending.items():
                if day >= start_day and 'error' not in result:
                    result.setdefault(metric, {})
                    result[metric][day] = result[metric].get(day, 0) + delta
            return result
        deferred.addCallback(merge_pending)
        return deferred
