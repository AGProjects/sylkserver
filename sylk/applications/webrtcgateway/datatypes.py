import base64
import hashlib
import json
import os
from datetime import datetime, timedelta
import urllib.parse

from sipsimple.util import ISOTimestamp
from sipsimple.payloads.rcsfthttp import FTHTTPDocument, FileInfo

from sipsimple.streams.msrp.chat import CPIMPayload, ChatIdentity, CPIMHeader, CPIMNamespace
from sylk.web import server
from sylk.configuration.datatypes import URL

from .configuration import GeneralConfig
from .models import sylkrtc


class FileTransferData(object):
    def __init__(self, filename, filesize, filetype, transfer_id, sender, receiver, content=None, url=None):
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
        self.until = self._set_until()
        self.url = self._set_url() if not url else url

    def _encode_and_hash_uri(self, uri):
        return base64.urlsafe_b64encode(hashlib.md5(uri.encode('utf-8')).digest()).rstrip(b'=\n').decode('utf-8')

    def _get_initial_path(self):
        return os.path.join(GeneralConfig.file_transfer_dir.normalized, self.prefix, self.hashed_sender, self.hashed_receiver, self.transfer_id)

    def update_path_for_receiver(self):
        self.prefix = self.hashed_receiver[:1]
        self.path = os.path.join(GeneralConfig.file_transfer_dir.normalized, self.prefix, self.hashed_receiver, self.hashed_sender, self.transfer_id)
        self.url = self._set_url()

    def _set_until(self):
        remove_after_days = GeneralConfig.filetransfer_expire_days
        return str(ISOTimestamp(datetime.now() + timedelta(days=remove_after_days)))

    def _set_url(self):
        stripped_path = os.path.relpath(self.path, f'{GeneralConfig.file_transfer_dir.normalized}/{self.prefix}')
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
    def message_payload(self):
        return f'File transfer available at {self.url} ({self.formatted_file_size})'

    def cpim_message_payload(self, metadata):
        return self.build_cpim_payload(self.sender.uri,
                                       self.receiver.uri,
                                       self.transfer_id,
                                       json.dumps(sylkrtc.FileTransferMessage(**metadata.__data__).__data__),
                                       content_type='application/sylk-file-transfer')

    def cpim_rcsfthttp_message_payload(self, metadata):
        return self.build_cpim_payload(self.sender.uri,
                                       self.receiver.uri,
                                       self.transfer_id,
                                       FTHTTPDocument.create(file=[FileInfo(file_size=metadata.filesize,
                                                                            file_name=metadata.filename,
                                                                            content_type=metadata.filetype,
                                                                            url=metadata.url,
                                                                            until=metadata.until,
                                                                            hash=metadata.hash)]),
                                       content_type=FTHTTPDocument.content_type)

    @property
    def formatted_file_size(self):
        return self.format_file_size(self.filesize)

    @staticmethod
    def build_cpim_payload(account, uri, message_id, content, content_type='text/plain'):
        ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
        additional_headers = [CPIMHeader('Message-ID', ns, message_id)]
        additional_headers.append(CPIMHeader('Disposition-Notification', ns, 'positive-delivery, display'))
        payload = CPIMPayload(content,
                              content_type,
                              charset='utf-8',
                              sender=ChatIdentity(account, None),
                              recipients=[ChatIdentity(uri, None)],
                              timestamp=str(ISOTimestamp.now().replace(microsecond=0)),
                              additional_headers=additional_headers)
        payload, content_type = payload.encode()
        return payload

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
