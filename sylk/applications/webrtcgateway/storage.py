
import pickle as pickle
import os

from application.python.types import Singleton
from collections import defaultdict
from sipsimple.threading import run_in_thread
from twisted.internet import defer

from sylk.configuration import ServerConfig

from .configuration import CassandraConfig
from .errors import StorageError
from .logger import log

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
        from cassandra import InvalidRequest
        from cassandra.cqlengine import CQLEngineException
        from cassandra.cqlengine.query import LWTException
        from cassandra.cluster import NoHostAvailable
        from cassandra.policies import DCAwareRoundRobinPolicy
        from .models.storage.cassandra import PushTokens
        if CassandraConfig.push_tokens_table:
            PushTokens.__table_name__ = CassandraConfig.push_tokens_table


class FileTokenStorage(object):
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
        try:
            (token, background_token) = contact_params['pn_tok'].split('-')
        except ValueError:
            token = contact_params['pn_tok']
            background_token = None
        data = {
            'device_id': contact_params['pn_device'],
            'platform': contact_params['pn_type'],
            'silent': contact_params['pn_silent'],
            'app_id': contact_params['pn_app'],
            'user_agent': user_agent,
            'background_token': background_token
        }
        key = f"{data['app_id']}-{data['device_id']}"
        if account in self._tokens:
            if isinstance(self._tokens[account], set):
                self._tokens[account] = {}
            # Remove old storage layout based on device id
            if contact_params['pn_device'] in self._tokens[account]:
                del self._tokens[account][contact_params['pn_device']]

            # Remove old storage layout based on token
            if token in self._tokens[account]:
                del self._tokens[account][token]

            # Remove old unsplit token if exists, can be removed if all tokens are stored split
            if background_token is not None:
                try:
                    del self._tokens[account][contact_params['pn_tok']]
                except IndexError:
                    pass
            self._tokens[account][key] = data
        else:
            self._tokens[account] = {key: data}
        self._save()

    def remove(self, account, app_id, device_id):
        key = f'{app_id}-{device_id}'
        try:
            del self._tokens[account][key]
        except KeyError:
            pass
        self._save()


class CassandraConnection(object, metaclass=Singleton):
    @run_in_thread('cassandra')
    def __init__(self):
        try:
            self.session = connection.setup(CassandraConfig.cluster_contact_points, CassandraConfig.keyspace, load_balancing_policy=DCAwareRoundRobinPolicy(), protocol_version=4)
        except NoHostAvailable:
            self.log.error("Not able to connect to any of the Cassandra contact points")
            raise StorageError


class CassandraTokenStorage(object):
    @run_in_thread('cassandra')
    def load(self):
        CassandraConnection()

    def __getitem__(self, key):
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_tokens(key):
            username, domain = key.split('@', 1)
            tokens = {}
            for device in PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain):
                tokens[f'{device.app_id}-{device.device_id}'] = {'device_id': device.device_id, 'token': device.token,
                                                                 'platform': device.platform, 'silent': device.silent,
                                                                 'app_id': device.app_id, 'background_token': device.background_token}
            deferred.callback(tokens)
            return tokens
        query_tokens(key)
        return deferred

    @run_in_thread('cassandra')
    def add(self, account, contact_params, user_agent):
        username, domain = account.split('@', 1)
        try:
            (token, background_token) = contact_params['pn_tok'].split('-')
        except ValueError:
            token = contact_params['pn_tok']
            background_token = None

        # Remove old unsplit token if exists, can be removed if all tokens are stored split
        if background_token is not None:
            try:
                PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain, PushTokens.device_token == contact_params['pn_tok']).if_exists().delete()
            except LWTException:
                pass
        try:
            PushTokens.create(username=username, domain=domain, device_id=contact_params['pn_device'],
                              device_token=token, background_token=background_token, platform=contact_params['pn_type'],
                              silent=contact_params['pn_silent'], app_id=contact_params['pn_app'], user_agent=user_agent)
        except (CQLEngineException, InvalidRequest) as e:
            self.logger.error(f'Storing token failed: {e}')
            raise StorageError

    @run_in_thread('cassandra')
    def remove(self, account, app_id, device_id):
        username, domain = account.split('@', 1)
        try:
            PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain,
                               PushTokens.device_id == device_id, PushTokens.app_id == app_id).if_exists().delete()
        except LWTException:
            pass


class TokenStorage(object, metaclass=Singleton):
    def __new__(self):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            return CassandraTokenStorage()
        else:
            return FileTokenStorage()
