
import datetime
import json
import pickle as pickle
import os

from application.python.types import Singleton
from application.system import makedirs
from collections import defaultdict
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp
from twisted.internet import defer
from types import SimpleNamespace

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
        from .models.storage.cassandra import ChatAccount, ChatMessage, ChatMessageIdMapping, PublicKey
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
            'background_token': background_token,
            'device_token': token
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
                except (IndexError, KeyError):
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

    def removeAll(self, account):
        try:
            del self._tokens[account]
        except KeyError:
            pass
        self._save()


class CassandraConnection(object, metaclass=Singleton):
    @run_in_thread('cassandra')
    def __init__(self):
        try:
            self.session = connection.setup(CassandraConfig.cluster_contact_points, CassandraConfig.keyspace, load_balancing_policy=DCAwareRoundRobinPolicy(), protocol_version=4)
        except NoHostAvailable:
            log.error("Not able to connect to any of the Cassandra contact points")
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
                tokens[f'{device.app_id}-{device.device_id}'] = {'device_id': device.device_id, 'token': device.device_token,
                                                                 'platform': device.platform, 'silent': device.silent,
                                                                 'app_id': device.app_id, 'background_token': device.background_token}
            deferred.callback(tokens)
            return tokens
        query_tokens(key)
        return deferred

    @run_in_thread('cassandra')
    def add(self, account, contact_params, user_agent):
        username, domain = account.split('@', 1)

        token = contact_params['pn_tok']
        background_token = None
        if contact_params['pn_type'] == 'ios':
            try:
                (token, background_token) = contact_params['pn_tok'].split('-')
            except ValueError:
                pass

        # Remove old unsplit token if exists, can be removed if all tokens are stored split
        if background_token is not None:
            try:
                old_token = PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain)[0]
            except (IndexError, LWTException):
                pass
            else:
                if old_token.device_token == contact_params['pn_tok']:
                    old_token.delete()
        try:
            PushTokens.create(username=username, domain=domain, device_id=contact_params['pn_device'],
                              device_token=token, background_token=background_token, platform=contact_params['pn_type'],
                              silent=contact_params['pn_silent'], app_id=contact_params['pn_app'], user_agent=user_agent)
        except (CQLEngineException, InvalidRequest) as e:
            log.error(f'Storing token failed: {e}')
            raise StorageError

    @run_in_thread('cassandra')
    def remove(self, account, app_id, device_id):
        username, domain = account.split('@', 1)
        try:
            PushTokens.objects(PushTokens.username == username, PushTokens.domain == domain,
                               PushTokens.device_id == device_id, PushTokens.app_id == app_id).if_exists().delete()
        except LWTException:
            pass


class FileMessageStorage(object):
    def __init__(self):
        self._public_keys = defaultdict()
        self._accounts = defaultdict()
        self._storage_path = os.path.join(ServerConfig.spool_dir, 'conversations')

    def _json_dateconverter(self, o):
        if isinstance(o, datetime.datetime):
            return o.__str__()

    @run_in_thread('file-io')
    def _save(self):
        with open(os.path.join(self._storage_path, 'accounts.json'), 'w+') as f:
            json.dump(self._accounts, f, default=self._json_dateconverter)
        with open(os.path.join(self._storage_path, 'public_keys.json'), 'w+') as f:
            json.dump(self._public_keys, f, default=self._json_dateconverter)

    def _save_messages(self, account, messages):
        with open(os.path.join(self._storage_path, account[0], f'{account}_messages.json'), 'w+') as f:
            json.dump(messages, f, default=self._json_dateconverter, indent=4)

    def _save_id_by_timestamp(self, account, ids):
        with open(os.path.join(self._storage_path, account[0], f'{account}_id_timestamp.json'), 'w+') as f:
            json.dump(ids, f, default=self._json_dateconverter)

    @run_in_thread('file-io')
    def load(self):
        makedirs(self._storage_path)
        try:
            accounts = json.load(open(os.path.join(self._storage_path, 'accounts.json'), 'r'))
        except (OSError, IOError):
            pass
        else:
            self._accounts.update(accounts)
        try:
            public_keys = json.load(open(os.path.join(self._storage_path, 'public_keys.json'), 'r'))
        except (OSError, IOError):
            pass
        else:
            self._public_keys.update(public_keys)

    def _load_id_by_timestamp(self, account):
        try:
            with open(os.path.join(self._storage_path, account[0], f'{account}_id_timestamp.json'), 'r') as f:
                data = json.load(f)
                return data
        except (OSError, IOError) as e:
            raise e

    def _load_messages(self, account):
        try:
            with open(os.path.join(self._storage_path, account[0], f'{account}_messages.json'), 'r') as f:
                messages = json.load(f)
                return messages
        except (OSError, IOError) as e:
            raise e

    def __getitem__(self, key):
        deferred = defer.Deferred()

        @run_in_thread('file-io')
        def query(account, message_id):
            messages = []
            timestamp = None

            try:
                id_by_timestamp = self._load_id_by_timestamp(account)
            except (OSError, IOError):
                deferred.callback(messages)
                return

            if message_id is not None:
                try:
                    timestamp = id_by_timestamp[message_id]
                except KeyError:
                    deferred.callback(messages)
                    return
                else:
                    timestamp = ISOTimestamp(timestamp)

            try:
                messages = self._load_messages(account)
            except (OSError, IOError):
                deferred.callback(messages)
                return
            else:
                if timestamp is not None:
                    messages = [message for message in messages if ISOTimestamp(message['created_at']) > timestamp]
                    deferred.callback(messages)
                else:
                    deferred.callback(messages)
        query(key[0], key[1])
        return deferred

    def get_account(self, account):
        try:
            return SimpleNamespace(account=account, **self._accounts[account])
        except KeyError:
            return None

    def get_account_token(self, account):
        try:
            if datetime.datetime.now() < datetime.datetime.fromisoformat(str(self._accounts[account]['token_expire'])):
                return self._accounts[account]['api_token']
            return None
        except KeyError:
            return None

    def remove_account_token(self, account):
        if account in self._accounts:
            self._accounts[account]['api_token'] = ''
            self._save()

    def add_account(self, account):
        timestamp = datetime.datetime.now()

        if account not in self._accounts:
            self._accounts[account] = {'last_login': timestamp}
            self._save()
        else:
            self._accounts[account]['last_login'] = timestamp
            self._save()

    def get_public_key(self, account):
        try:
            return self._public_keys[account]['content']
        except KeyError:
            return None

    def remove_account(self, account):
        if account in self._accounts:
            del self._accounts[account]
            self._save()

    def remove_public_key(self, account):
        if account in self._public_keys:
            del self.public_keys[account]
            self._save()

    def add_account_token(self, account, token):
        timestamp = datetime.datetime.now()
        if account not in self._accounts:
            log.error(f'Updating API token for {account} failed')
            return StorageError
        self._accounts[account]['api_token'] = token
        self._accounts[account]['token_expire'] = timestamp + datetime.timedelta(seconds=26784000)
        self._save()

    @run_in_thread('file-io')
    def mark_conversation_read(self, account, contact):
        try:
            messages = self._load_messages(account)
        except (OSError, IOError):
            return
        else:
            for idx, message in enumerate(messages):
                if message['contact'] == contact:
                    message['disposition'] = []
                    messages[idx] = message
            self._save_messages(account, messages)

    @run_in_thread('file-io')
    def update(self, account, state, message_id):
        try:
            messages = self._load_messages(account)
        except (OSError, IOError):
            return

        try:
            id_by_timestamp = self._load_id_by_timestamp(account)
        except (OSError, IOError):
            return
        else:
            try:
                timestamp = id_by_timestamp[message_id]
            except KeyError:
                return
            else:
                for idx, message in enumerate(messages):
                    if message['created_at'] == timestamp and message['message_id'] == message_id:
                        if message['state'] != 'received':
                            message['state'] = state
                        if state == 'delivered':
                            try:
                                message['disposition'].remove('positive-delivery')
                            except ValueError:
                                pass
                        elif state == 'displayed':
                            message['disposition'] = []
                        messages[idx] = message
                        self._save_messages(account, messages)
                        break

    @run_in_thread('file-io')
    def add(self, account, contact, direction, content, content_type, timestamp, disposition_notification, message_id, state=None):
        try:
            msg_timestamp = datetime.datetime.fromisoformat(timestamp)
        except ValueError:
            msg_timestamp = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
        timestamp = datetime.datetime.now()

        messages = []
        id_by_timestamp = {}

        try:
            messages = self._load_messages(account)
        except (OSError, IOError):
            makedirs(os.path.join(self._storage_path, account[0]))

        timestamp_not_found = True
        try:
            id_by_timestamp = self._load_id_by_timestamp(account)
        except (OSError, IOError):
            pass
        else:
            try:
                created_at = id_by_timestamp[message_id]
            except KeyError:
                pass
            else:
                timestamp_not_found = False
                if content_type == 'message/imdn+json':
                    return

                items = [n for n in messages if n['created_at'] == created_at and n['message_id'] == message_id]
                if len(items) == 1:
                    return

        if content_type == 'text/pgp-public-key':
            key = account if direction == "outgoing" else contact
            self._public_keys[key] = {'content': content, 'created_at': timestamp}
            self._save()
            return

        if content_type == 'text/pgp-private-key':
            if timestamp_not_found:
                id_by_timestamp[message_id] = timestamp
                self._save_id_by_timestamp(account, id_by_timestamp)
            return

        if not isinstance(disposition_notification, list) and disposition_notification == '':
            disposition_notification = []

        message = {'account': account,
                   'direction': direction,
                   'contact': contact,
                   'content_type': content_type,
                   'content': content,
                   'created_at': timestamp,
                   'message_id': message_id,
                   'disposition': disposition_notification,
                   'state': state,
                   'msg_timestamp': msg_timestamp}
        messages.append(message)

        self._save_messages(account, messages)

        if timestamp_not_found:
            id_by_timestamp[message_id] = timestamp

        self._save_id_by_timestamp(account, id_by_timestamp)

    @run_in_thread('file-io')
    def removeChat(self, account, contact):
        try:
            messages = self._load_messages(account)
        except (OSError, IOError):
            pass
        else:
            messages[:] = [message for message in messages if message['contact'] != contact]

            self._save_messages(account, messages)

    @run_in_thread('file-io')
    def removeMessage(self, account, message_id):
        try:
            id_by_timestamp = self._load_id_by_timestamp(account)
        except (OSError, IOError):
            return
        else:
            try:
                timestamp = id_by_timestamp[message_id]
            except KeyError:
                return
            else:
                try:
                    messages = self._load_messages(account)
                except (OSError, IOError):
                    return
                else:
                    item = [n for n in messages if n['created_at'] == timestamp and n['message_id'] == message_id]
                    if len(item) == 1:
                        messages.remove(item[0])
                        self._save_messages(account, messages)


class CassandraMessageStorage(object):
    @run_in_thread('cassandra')
    def load(self):
        CassandraConnection()

    def __getitem__(self, key):
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_messages(key, message_id):
            messages = []
            try:
                timestamp = ChatMessageIdMapping.objects(ChatMessageIdMapping.message_id == message_id)[0]
            except (IndexError, InvalidRequest):
                timestamp = datetime.datetime.now() - datetime.timedelta(days=3)
            else:
                timestamp = timestamp.created_at
            for message in ChatMessage.objects(ChatMessage.account == key, ChatMessage.created_at > timestamp):
                timestamp_naive = message.msg_timestamp
                try:
                    timestamp_utc = timestamp_naive.replace(tzinfo=datetime.timezone.utc)
                except AttributeError:
                    continue
                message.msg_timestamp = timestamp_utc
                messages.append(message)
            deferred.callback(messages)

        query_messages(key[0], key[1])
        return deferred

    def get_account(self, account):
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_tokens(account):
            try:
                chat_account = ChatAccount.objects(ChatAccount.account == account)[0]
            except (IndexError, InvalidRequest):
                deferred.callback(None)
            else:
                deferred.callback(chat_account)

        query_tokens(account)
        return deferred

    def get_account_token(self, account):
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_tokens(account):
            try:
                chat_account = ChatAccount.objects(ChatAccount.account == account)[0]
            except (IndexError, InvalidRequest):
                deferred.callback(None)
            else:
                deferred.callback(chat_account.api_token)

        query_tokens(account)
        return deferred

    @run_in_thread('cassandra')
    def add_account(self, account):
        timestamp = datetime.datetime.now()

        try:
            ChatAccount.create(account=account, last_login=timestamp)
        except (CQLEngineException, InvalidRequest) as e:
            log.error(f'Storing account failed: {e}')
            raise StorageError

    @run_in_thread('cassandra')
    def add_account_token(self, account, token):
        try:
            chat_account = ChatAccount.objects(account=account)[0]
            chat_account.ttl(2678400).update(api_token=token)
        except IndexError:
            log.error(f'Updating API token for {account} failed')
            raise StorageError

    @run_in_thread('cassandra')
    def mark_conversation_read(self, account, contact):
        for message in ChatMessage.objects(ChatMessage.account == account):
            if message.contact == contact:
                if message.content_type == 'application/sylk-conversation-read':
                    message.delete()
                else:
                    message.disposition.clear()
                    message.save()

    def get_public_key(self, account):
        deferred = defer.Deferred()

        @run_in_thread('cassandra')
        def query_key(account):
            try:
                public_key = PublicKey.objects(PublicKey.account == account)[0]
            except (IndexError, InvalidRequest):
                deferred.callback(None)
            else:
                deferred.callback(public_key.public_key)

        query_key(account)
        return deferred

    @run_in_thread('cassandra')
    def update(self, account, state, message_id):
        try:
            timestamp = ChatMessageIdMapping.objects(ChatMessageIdMapping.message_id == message_id)[0]
        except IndexError:
            return
        else:
            try:
                message = ChatMessage.objects(ChatMessage.account == account,
                                              ChatMessage.created_at == timestamp.created_at,
                                              ChatMessage.message_id == message_id)[0]
            except IndexError:
                pass
            else:
                if message.state != 'received':
                    message.state = state
                if state == 'delivered':
                    try:
                        message.disposition.remove('positive-delivery')
                    except ValueError:
                        pass
                elif state == 'displayed':
                    message.disposition.clear()
                message.save()

    @run_in_thread('cassandra')
    def add(self, account, contact, direction, content, content_type, timestamp, disposition_notification, message_id, state=None):
        try:
            msg_timestamp = datetime.datetime.fromisoformat(timestamp)
        except ValueError:
            msg_timestamp = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
        timestamp = datetime.datetime.now()

        timestamp_not_found = True
        try:
            created_at = ChatMessageIdMapping.objects(ChatMessageIdMapping.message_id == message_id)[0]
        except IndexError:
            pass
        else:
            timestamp_not_found = False
            timestamp = created_at.created_at
            if content_type == 'message/imdn+json':
                return
            if ChatMessage.objects(ChatMessage.account == account,
                                   ChatMessage.created_at == created_at.created_at,
                                   ChatMessage.message_id == message_id).count() != 0:
                return

        if content_type == 'text/pgp-public-key':
            try:
                PublicKey.create(account=account if direction == "outgoing" else contact, public_key=content, created_at=timestamp)
                ChatMessageIdMapping.create(created_at=timestamp, message_id=message_id)
            except (CQLEngineException, InvalidRequest) as e:
                log.error(f'Storing public key failed: {e}')
                raise StorageError
            else:
                return

        if content_type == 'text/pgp-private-key':
            if timestamp_not_found:
                try:
                    ChatMessageIdMapping.create(created_at=timestamp, message_id=message_id)
                except (CQLEngineException, InvalidRequest):
                    pass
            return

        try:
            ChatMessage.create(account=account, direction=direction, contact=contact, content_type=content_type,
                               content=content, created_at=timestamp, message_id=message_id,
                               disposition=disposition_notification, state=state, msg_timestamp=msg_timestamp)
            if timestamp_not_found:
                ChatMessageIdMapping.create(created_at=timestamp, message_id=message_id)
        except (CQLEngineException, InvalidRequest) as e:
            log.error(f'Storing message failed: {e}')
            raise StorageError

    @run_in_thread('cassandra')
    def removeChat(self, account, contact):
        try:
            messages = ChatMessage.objects(ChatMessage.account == account)
        except LWTException:
            pass
        else:
            for message in messages:
                if message.contact == contact:
                    message.delete()

    @run_in_thread('cassandra')
    def removeMessage(self, account, message_id):
        try:
            timestamp = ChatMessageIdMapping.objects(ChatMessageIdMapping.message_id == message_id)[0]
        except IndexError:
            return
        else:
            try:
                ChatMessage.objects(ChatMessage.account == account,
                                    ChatMessage.created_at == timestamp.created_at,
                                    ChatMessage.message_id == message_id).if_exists().delete()
            except LWTException:
                pass


class TokenStorage(object, metaclass=Singleton):
    def __new__(self):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            return CassandraTokenStorage()
        else:
            return FileTokenStorage()


class MessageStorage(object, metaclass=Singleton):
    def __new__(self):
        if CASSANDRA_MODULES_AVAILABLE and CassandraConfig.cluster_contact_points:
            return CassandraMessageStorage()
        else:
            return FileMessageStorage()
