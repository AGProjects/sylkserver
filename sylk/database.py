# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

"""
Database connection factory

Each application that wants to connect to a database should instantiate a Database 
object with the URI it wants to connect to. As Database is a Singleton, same object
will be used if the same URI is specified.

A usage example can be found in the conference application database module.
"""

__all__ = ['Database']

from application import log
from application.python import Null
from application.python.types import Singleton
from sqlobject import connectionForURI


class Database(object):
    __metaclass__ = Singleton

    def __init__(self, uri):
        if uri == 'sqlite:/:memory:':
            log.warn("SQLite memory backend can't be used because it's not thread-safe")
            uri = None
        self.uri = uri
        if uri is not None:
            self.connection = connectionForURI(uri)
        else:
            self.connection = Null

    def create_table(self, klass):
        if klass._connection is Null or klass.tableExists():
            return
        else:
            log.warn('Table %s does not exists. Creating it now.' % klass.sqlmeta.table)
            saved = klass._connection.debug
            try:
                klass._connection.debug = True # log SQL used to create the table
                klass.createTable()
            finally:
                klass._connection.debug = saved

