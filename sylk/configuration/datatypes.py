# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import os
import re
import socket
import sys
import urllib
import urlparse

from application.python.descriptor import classproperty
from application.system import host
from sipsimple.configuration.datatypes import AudioCodecList, Hostname, SIPTransport


class AudioCodecs(list):
    def __new__(cls, value):
        if isinstance(value, (tuple, list)):
            return [str(x) for x in value if x in AudioCodecList.available_values] or None
        elif isinstance(value, basestring):
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


class ResourcePath(object):
    def __init__(self, path):
        self.path = os.path.normpath(str(path))

    def __getstate__(self):
        return unicode(self.path)

    def __setstate__(self, state):
        self.__init__(state)

    @property
    def normalized(self):
        path = os.path.expanduser(self.path)
        if os.path.isabs(path):
            return os.path.realpath(path)
        return os.path.realpath(os.path.join(self.resources_directory, path))

    @classproperty
    def resources_directory(cls):
        from sylk.configuration import ServerConfig
        if ServerConfig.resources_dir is not None:
            return os.path.realpath(ServerConfig.resources_dir)
        else:
            binary_directory = os.path.dirname(os.path.realpath(sys.argv[0]))
            if os.path.basename(binary_directory) == 'bin':
                application_directory = os.path.dirname(binary_directory)
                resources_component = 'share/sylkserver'
            else:
                application_directory = binary_directory
                resources_component = 'resources'
            return os.path.realpath(os.path.join(application_directory, resources_component))

    def __eq__(self, other):
        try:
            return self.path == other.path
        except AttributeError:
            return False

    def __hash__(self):
        return hash(self.path)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.path)

    def __unicode__(self):
        return unicode(self.path)


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
        allowed = xrange(1024, 65537, 2)
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
        return unicode(self)

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
        return u'%s:%d;transport=%s' % (self.host, self.port, self.transport)


class NillablePath(unicode):
    def __new__(cls, path):
        path = os.path.normpath(path)
        if not os.path.exists(path):
            return None
        return unicode.__new__(cls, path)

    @property
    def normalized(self):
        return os.path.expanduser(self)


class Path(unicode):
    def __new__(cls, path):
        path = os.path.normpath(path)
        return unicode.__new__(cls, path)

    @property
    def normalized(self):
        return os.path.expanduser(self)


class URL(object):
    """A class describing an URL and providing access to its elements"""

    def __init__(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
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
        self.path = urllib.url2pathname(path)
        self.query_items = dict(urlparse.parse_qsl(query))
        self.fragment = fragment

    def __str__(self):
        return urlparse.urlunsplit((self.scheme, self.netloc, urllib.pathname2url(self.path), self.query, self.fragment))

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.__str__())

    url = property(__str__)

    @property
    def query(self):
        return urllib.urlencode(self.query_items)

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

