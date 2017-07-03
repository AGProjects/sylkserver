
__all__ = ['TokenStorage']

import cPickle as pickle
import os

from application.python.types import Singleton
from collections import defaultdict
from sipsimple.threading import run_in_thread

from sylk.configuration import ServerConfig


# TODO: This implementation is a prototype. It should be refactored to store tokens in a
# distributed DB so other SylkServer instances can access them.  Also add some metadata
# like the modification date so we know when a token was refreshed, and thus it's ok to
# scrap it after a reasonable amount of time.


class TokenStorage(object):
    __metaclass__ = Singleton

    def __init__(self):
        self._tokens = defaultdict(set)

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

    def add(self, account, token):
        self._tokens[account].add(token)
        self._save()

    def remove(self, account, token):
        self._tokens[account].discard(token)
        self._save()
