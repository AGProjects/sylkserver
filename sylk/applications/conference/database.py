# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

__all__ = ['async_save_message', 'get_last_messages']

import os
import datetime
import time

from application.python import Null
from eventlib.twistedutil import block_on
from sqlobject import SQLObject, DateTimeCol, UnicodeCol
from twisted.internet.threads import deferToThread

from sylk.applications import ApplicationLogger
from sylk.applications.conference.configuration import ConferenceConfig
from sylk.database import Database


db = Database(ConferenceConfig.db_uri)
log = ApplicationLogger(os.path.dirname(__file__).split(os.path.sep)[-1])

class MessageHistory(SQLObject):
    class sqlmeta:
        table = ConferenceConfig.history_table
    _connection = db.connection
    date = DateTimeCol()
    room_uri = UnicodeCol()
    sip_from = UnicodeCol()
    cpim_body = UnicodeCol(sqlType='LONGTEXT')
    cpim_content_type = UnicodeCol()
    cpim_sender = UnicodeCol()
    cpim_recipient = UnicodeCol()
    cpim_timestamp = DateTimeCol()

db.create_table(MessageHistory)


def _save_message(sip_from, room_uri, cpim_body, cpim_content_type, cpim_sender, cpim_recipient, cpim_timestamp):
    for x in xrange(0, 3):
        try:
            return MessageHistory(date              = datetime.datetime.utcnow(),
                                room_uri          = room_uri,
                                sip_from          = sip_from,
                                cpim_body         = cpim_body,
                                cpim_content_type = cpim_content_type,
                                cpim_sender       = cpim_sender,
                                cpim_recipient    = cpim_recipient,
                                cpim_timestamp    = cpim_timestamp)
        except Exception, e:
            time.sleep(0.2)
    log.error('Could not save DB records: %s' % e)
    return

def async_save_message(sip_from, room_uri, cpim_body, cpim_content_type, cpim_sender, cpim_recipient, cpim_timestamp):
    if db.connection is not Null:
        return deferToThread(_save_message, sip_from, room_uri, cpim_body, cpim_content_type, cpim_sender, cpim_recipient, cpim_timestamp)

def _get_last_messages(room_uri, count):
    for x in xrange(0, 3):
        try:
            return list(MessageHistory.selectBy(room_uri=room_uri)[-count:])
        except Exception, e:
            time.sleep(0.2)
    log.error('Could not get DB records: %s' % e)
    return []

def get_last_messages(room_uri, count):
    if db.connection is not Null and count > 0:
        return block_on(deferToThread(_get_last_messages, room_uri, count))
    else:
        return []


