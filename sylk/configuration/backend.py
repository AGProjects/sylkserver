# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

__all__ = ['MemoryBackend']

from sipsimple.configuration.backend import IConfigurationBackend
from zope.interface import implements


class MemoryBackend(object):
    """
    Implementation of a configuration backend that stores data in 
    memory.
    """

    implements(IConfigurationBackend)

    def __init__(self):
        self._data = {}

    def load(self):
        return self._data

    def save(self, data):
        self._data = data

