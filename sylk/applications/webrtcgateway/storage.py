
import cPickle as pickle
import os

from application.python.types import Singleton
from collections import defaultdict
from sipsimple.threading import run_in_thread
from twisted.internet import defer

from sylk.configuration import ServerConfig

from .configuration import CassandraConfig

__all__ = 'TokenStorage',


# TODO: Maybe add some more metadata like the modification date so we know when a token was refreshed,
# and thus it's ok to scrap it after a reasonable amount of time.

CASSANDRA_MODULES_AVAILABLE = False
try:
    from cassandra.cqlengine import columns, connection
except ImportError:
    pass
else:
    try:
        from cassandra.cqlengine.models import Model
    except ImportError:
        pass
    else:
        CASSANDRA_MODULES_AVAILABLE = True
        from cassandra.cqlengine.query import LWTException
        class PushTokens(Model):
            username        = columns.Text(partition_key=True)
            domain          = columns.Text(partition_key=True)
            device_id       = columns.Text()
            app             = columns.Text()
            device_token    = columns.Text(primary_key=True)
            platform        = columns.Text()
            silent          = columns.Text()
            user_agent      = columns.Text(required=False)


class FileStorage(object):
    def __init__(self):
        self._tokens = defaultdict()

    @run_in_thread('file-io')
    def _save(self):
        with open(os.path.join(ServerConfig.spool_dir, 'webrtc_device_tokens'), 'wb+') as f:
            pickle.dump(self._tokens, f)

    @run_in_thread('file-io')
    def load(self):
        try:
            tokens = pickle.load(open(os.path.join(ServerConfig.spool_dir, 'webrtc_device_tokens'), 'rb'))
        except Exception:
            pass
        else:
            self._tokens.update(tokens)

    def __getitem__(self, key):
        try:
            return self._tokens[key]
        except KeyError:
            return {}

    def add(self, account, contact_params, user_agent):
        data = {
            'device_id': contact_params['pn_device'],
            'platform': contact_params['pn_type'],
            'silent': contact_params['pn_silent'],
            'app': contact_params['pn_app'],
            'user_agent': user_agent
        }
        if account in self._tokens:
            if isinstance(self._tokens[account], set):
                self._tokens[account] = {}
            # Remove old storage layout based on device id
            if contact_params['pn_device'] in self._tokens[account]:
                del self._tokens[account][contact_params['pn_device']]
            self._tokens[account][contact_params['pn_tok']] = data
        else:
            self._tokens[account] = {contact_params['pn_tok']: data}
        self._save()

    def remove(self, account, device_token):
        try:
            device_token = device_token.split('#')[0]
        except IndexError:
            pass
        try:
            del self._tokens[account][device_token]
        except KeyError:
            pass
        self._save()


class CassandraStorage(object):
    @run_in_thread('cassandra')
    def load(self):
        connection.setup(CassandraConfig.cluster_contact_points, CassandraConfig.keyspace, protocol_version=4)

    def __getitem__(self, key):
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_tokens(key):
            username, domain = key.split('@', 1)
            tokens = {}
            for device in PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain):
                tokens[device.device_token] = {'device_id': device.device_id, 'platform': device.platform,
                                            'silent': device.silent, 'app': device.app}
            deferred.callback(tokens)
            return tokens
        query_tokens(key)
        return deferred

    @run_in_thread('cassandra')
    def add(self, account, contact_params, user_agent):
        username, domain = account.split('@', 1)
        PushTokens.create(username=username, domain=domain, device_id=contact_params['pn_device'],
                          device_token=contact_params['pn_tok'], platform=contact_params['pn_type'],
                          silent=contact_params['pn_silent'], app=contact_params['pn_app'], user_agent=user_agent)

    @run_in_thread('cassandra')
    def remove(self, account, device_token):
        username, domain = account.split('@', 1)
        try:
            device_token = device_token.split('#')[0]
        except IndexError:
            pass
        try:
            PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain, PushTokens.device_token == device_token).if_exists().delete()
        except LWTException:
            pass


class TokenStorage(object):
    __metaclass__ = Singleton

    def __new__(self):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            return CassandraStorage()
        else:
            return FileStorage()
