import base64
import hashlib
import os
import urllib.parse

from sipsimple.util import ISOTimestamp
from sipsimple.configuration.settings import SIPSimpleSettings

from sylk.web import server
from sylk.configuration.datatypes import URL
from .models import sylkrtc


class FileTransferData(object):
    def __init__(self, filename, filesize, filetype, transfer_id, sender, receiver, content=None):
        self.content = content
        self.filename = filename
        self.filesize = filesize
        self.filetype = filetype
        self.transfer_id = transfer_id
        self.sender = sylkrtc.SIPIdentity(uri=sender, display_name='')
        self.receiver = sylkrtc.SIPIdentity(uri=receiver, display_name='')
        self.hashed_sender = sender
        self.hashed_receiver = receiver
        self.prefix = self.hashed_sender[:1]
        self.path = self._get_initial_path()
        self.timestamp = str(ISOTimestamp.now())
        self.url = self._set_url()

    def _encode_and_hash_uri(self, uri):
        return base64.urlsafe_b64encode(hashlib.md5(uri.encode('utf-8')).digest()).rstrip(b'=\n').decode('utf-8')

    def _get_initial_path(self):
        settings = SIPSimpleSettings()
        return os.path.join(settings.file_transfer.directory.normalized, self.prefix, self.hashed_sender, self.hashed_receiver, self.transfer_id)

    def update_path_for_receiver(self):
        settings = SIPSimpleSettings()
        self.prefix = self.hashed_receiver[:1]
        self.path = os.path.join(settings.file_transfer.directory.normalized, self.prefix, self.hashed_receiver, self.hashed_sender, self.transfer_id)
        self.url = self._set_url()

    def _set_url(self):
        settings = SIPSimpleSettings()
        stripped_path = os.path.relpath(self.path, f'{settings.file_transfer.directory.normalized}/{self.prefix}')
        file_path = urllib.parse.quote(f'webrtcgateway/filetransfer/{stripped_path}/{self.filename}')
        return str(URL(f'{server.url}/{file_path}'))

    @property
    def full_path(self):
        return os.path.join(self.path, self.filename)

    @property
    def hashed_sender(self):
        return self._hashed_sender

    @hashed_sender.setter
    def hashed_sender(self, value):
        self._hashed_sender = self._encode_and_hash_uri(value)

    @property
    def hashed_receiver(self):
        return self._hashed_receiver

    @hashed_receiver.setter
    def hashed_receiver(self, value):
        self._hashed_receiver = self._encode_and_hash_uri(value)

    @property
    def formatted_file_size(self):
        return self.format_file_size(self.filesize)

    @staticmethod
    def format_file_size(size):
        infinite = float('infinity')
        boundaries = [(             1024, '%d bytes',               1),
                      (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                      (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                      (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
        for boundary, format, divisor in boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size
