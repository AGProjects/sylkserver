
import cPickle as pickle
import os

from application.python.types import Singleton
from collections import defaultdict
from sipsimple.threading import run_in_thread

from sylk.configuration import ServerConfig


__all__ = 'TokenStorage',


# TODO: This implementation is a prototype. It should be refactored to store tokens in a
# distributed DB so other SylkServer instances can access them.  Also add some metadata
# like the modification date so we know when a token was refreshed, and thus it's ok to
# scrap it after a reasonable amount of time.


class TokenStorage(object):
    __metaclass__ = Singleton

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
        return self._tokens[key]

    def add(self, account, contact_params):
        data = {
            'token': contact_params['pn_tok'],
            'platform': contact_params['pn_type'],
            'silent': contact_params['pn_silent'],
            'app': contact_params['pn_app']
        }
        if account in self._tokens:
            if isinstance(self._tokens[account], set):
                self._tokens[account] = {}
            self._tokens[account][contact_params['pn_device']] = data
        else:
            self._tokens[account] = {contact_params['pn_device']: data}
        self._save()

    def remove(self, account, device_id):
        try:
            del self._tokens[account][device_id]
        except KeyError as e:
            pass
        self._save()
