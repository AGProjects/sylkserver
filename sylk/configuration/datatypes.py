
import os
import re
import socket
import urllib.request, urllib.parse, urllib.error
import urllib.parse

from application import log
from application.system import host
from sipsimple.configuration.datatypes import AudioCodecList, Hostname, SIPTransport


class AudioCodecs(list):
    def __new__(cls, value):
        if isinstance(value, (tuple, list)):
            return [str(x) for x in value if x in AudioCodecList.available_values] or None
        elif isinstance(value, str):
            if value.lower() in ('none', ''):
                return None
            return [x for x in re.split(r'\s*,\s*', value) if x in AudioCodecList.available_values] or None
        else:
            raise TypeError("value must be a string, list or tuple")


class IPAddress(str):
    """An IP address in quad dotted number notation"""
    def __new__(cls, value):
        try:
            socket.inet_aton(value)
        except socket.error:
            raise ValueError("invalid IP address: %r" % value)
        except TypeError:
            raise TypeError("value must be a string")
        return str.__new__(cls, value)

    @property
    def normalized(self):
        if self == '0.0.0.0':
            return host.default_ip or '127.0.0.1'
        return str(self)


class Port(int):
    def __new__(cls, value):
        try:
            value = int(value)
        except ValueError:
            return None
        if not (0 <= value <= 65535):
            raise ValueError("illegal port value: %s" % value)
        return value


class PortRange(object):
    """A port range in the form start:end with start and end being even numbers in the [1024, 65536] range"""
    def __init__(self, value):
        self.start, self.end = [int(p) for p in value.split(':', 1)]
        allowed = range(1024, 65537, 2)
        if not (self.start in allowed and self.end in allowed and self.start < self.end):
            raise ValueError("bad range: %r: ports must be even numbers in the range [1024, 65536] with start < end" % value)


class SIPProxyAddress(object):
    _description_re = re.compile(r"^(?P<host>(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})|([a-zA-Z0-9\-_]+(\.[a-zA-Z0-9\-_]+)*))(:(?P<port>\d+))?(;transport=(?P<transport>.+))?$")

    def __new__(cls, description):
        if not description:
            return None
        if not cls._description_re.match(description):
            raise ValueError("illegal SIP proxy address: %s" % description)
        return super(SIPProxyAddress, cls).__new__(cls)

    def __init__(self, description):
        match = self.__class__._description_re.match(description)
        data = match.groupdict()
        host = data.get('host')
        port = data.get('port', None) or 5060
        transport = data.get('transport', None) or 'udp'
        self.host = Hostname(host)
        self.port = Port(port)
        if self.port == 0:
            raise ValueError("illegal port value: 0")
        self.transport = SIPTransport(transport)

    def __getstate__(self):
        return str(self)

    def __setstate__(self, state):
        if not self.__class__._description_re.match(state):
            raise ValueError("illegal SIP proxy address: %s" % state)
        self.__init__(state)

    def __eq__(self, other):
        try:
            return (self.host, self.port, self.transport) == (other.host, other.port, other.transport)
        except AttributeError:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.host, self.port, self.transport))

    def __unicode__(self):
        return '%s:%d;transport=%s' % (self.host, self.port, self.transport)


class Path(str):
    def __new__(cls, path):
        if path:
            path = os.path.normpath(path)
        return str.__new__(cls, path)

    @property
    def normalized(self):
        return os.path.expanduser(self)


class URL(object):
    """A class describing an URL and providing access to its elements"""

    def __init__(self, url):
        scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
        if netloc:
            if "@" in netloc:
                userinfo, hostport = netloc.split("@", 1)
                if ":" in userinfo:
                    username, password = userinfo.split(":", 1)
                else:
                    username, password = userinfo, None
            else:
                username = password = None
                hostport = netloc
            if ':' in hostport:
                host, port = hostport.split(':', 1)
            else:
                host, port = hostport, None
        else:
            username = password = host = port = None
        self.original_url = url
        self.scheme = scheme
        self.username = username
        self.password = password
        self.host = host
        self.port = int(port) if port is not None else None
        self.path = path
        self.query_items = dict(urllib.parse.parse_qsl(query))
        self.fragment = fragment

    def __str__(self):
        return urllib.parse.urlunsplit((self.scheme, self.netloc, self.path, self.query, self.fragment))

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.__str__())

    url = property(__str__)

    @property
    def query(self):
        return urllib.parse.urlencode(self.query_items)

    @property
    def netloc(self):
        authinfo = ':'.join(str(x) for x in (self.username, self.password) if x is not None) or None
        hostport = ':'.join(str(x) for x in (self.host or '', self.port) if x is not None)
        return '@'.join(x for x in (authinfo, hostport) if x is not None)


class SRTPEncryption(str):
    available_values = ('opportunistic', 'sdes', 'zrtp', 'disabled')

    def __new__(cls, value):
        value = str(value)
        if value not in cls.available_values:
            raise ValueError("illegal value for SRTP encryption: %s" % value)
        return value


class LogLevel(log.NamedLevel):
    __levelmap__ = {value.name: value for value in (log.level.DEBUG, log.level.INFO, log.level.WARNING, log.level.ERROR, log.level.CRITICAL)}

    def __new__(cls, value):
        value = str(value).upper()
        if value not in cls.__levelmap__:
            raise ValueError("illegal value for log level: %s" % value)
        log.level.current = cls.__levelmap__[value]
        return log.level.current


class VideoBitrate(int):
    def __new__(cls, value):
        min_bitrate = 64000
        max_bitrate = 4*1024*1024  # 4 Mb/s
        value = int(value)
        if not (min_bitrate <= value <= max_bitrate):
            raise ValueError('value must be an integer number between {} and {}'.format(min_bitrate, max_bitrate))
        return value


class VideoCodec(str):
    valid_values = 'h264', 'vp8', 'vp9'

    def __new__(cls, value):
        value = value.lower()
        if value not in cls.valid_values:
            raise ValueError('value must be one of: {!s}'.format(', '.join(cls.valid_values)))
        return str.__new__(cls, value)
