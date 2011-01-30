# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import os
import re
import socket
import sys

from sipsimple.configuration.datatypes import AudioCodecList
from sipsimple.util import classproperty


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
        if value == '0.0.0.0':
            raise ValueError("%s is not allowed, please specify a specific IP address" % value)
        else:
            try:
                socket.inet_aton(value)
            except socket.error:
                raise ValueError("invalid IP address: %r" % value)
            except TypeError:
                raise TypeError("value must be a string")
            return str(value)

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


