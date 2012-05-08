# Copyright (C) 2012 AG Projects. See LICENSE for details
#

import hashlib
import random
import string

from application.python.descriptor import WriteOnceAttribute
from sipsimple.core import BaseSIPURI, SIPURI, SIPCoreError
from twisted.words.protocols.jabber.jid import JID


sylkserver_prefix = hashlib.md5('sylkserver').hexdigest()

def generate_sylk_resource():
    r = 'sylk-'+''.join(random.choice(string.ascii_letters+string.digits) for x in range(32))
    return r.encode('hex')

def is_sylk_resource(r):
    if r.startswith('urn:uuid:') or len(r) != 74:
        return False
    try:
        decoded = r.decode('hex')
    except TypeError:
        return False
    else:
        return decoded.startswith('sylk-')

def encode_resource(r):
    return r.encode('utf-8').encode('hex')

def decode_resource(r):
    return r.decode('hex').decode('utf-8')


class BaseURI(object):
    def __init__(self, user, host, resource=None):
        self.user = user
        self.host = host
        self.resource = resource

    @classmethod
    def parse(cls, value):
        if isinstance(value, BaseSIPURI):
            user = unicode(value.user)
            host = unicode(value.host)
            resource = unicode(value.parameters.get('gr', '')) or None
            return cls(user, host, resource)
        elif isinstance(value, JID):
            user = value.user
            host = value.host
            resource = value.resource
            return cls(user, host, resource)
        elif not isinstance(value, basestring):
            raise TypeError('uri needs to be a string')
        if not value.startswith(('sip:', 'sips:', 'xmpp:')):
            raise ValueError('invalid uri scheme for %s' % value)
        if value.startswith(('sip:', 'sips:')):
            try:
                uri = SIPURI.parse(value)
            except SIPCoreError:
                raise ValueError('invalid SIP uri: %s' % value)
            user = unicode(uri.user)
            host = unicode(uri.host)
            resource = unicode(uri.parameters.get('gr', '')) or None
        else:
            try:
                jid = JID(value[5:])
            except Exception:
                raise ValueError('invalid XMPP uri: %s' % value)
            user = jid.user
            host = jid.host
            resource = jid.resource
        return cls(user, host, resource)

    @classmethod
    def new(cls, uri):
        if not isinstance(uri, BaseURI):
            raise TypeError('%s is not a valid URI type' % type(uri))
        return cls(uri.user, uri.host, uri.resource)

    def as_sip_uri(self):
        uri = SIPURI(user=str(self.user), host=str(self.host))
        if self.resource is not None:
            uri.parameters['gr'] = self.resource.encode('utf-8')
        return uri

    def as_xmpp_jid(self):
        jid = JID(tuple=(self.user, self.host, self.resource))
        return jid

    def as_string(self, protocol):
        if protocol not in ('sip', 'xmpp'):
            raise ValueError('protocol must be one of "sip" or "xmpp"')
        if protocol == 'sip':
            uri = self.as_sip_uri()
            return unicode(str(uri))
        else:
            uri = self.as_xmpp_jid()
            return unicode(uri)

    def __eq__(self, other):
        if isinstance(other, BaseURI):
            return self.user == other.user and self.host == other.host and self.resource == other.resource
        elif isinstance(other, basestring):
            try:
                other = BaseURI.parse(other)
            except ValueError:
                return False
            else:
                return self.user == other.user and self.host == other.host and self.resource == other.resource
        else:
            return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    def __repr__(self):
        return '%s(user=%r, host=%r, resource=%r)' % (self.__class__.__name__, self.user, self.host, self.resource)

    def __unicode__(self):
        return u'%s@%s' % (self.user, self.host)

    def __str__(self):
        return unicode(self).encode('utf-8')


class URI(BaseURI):
    pass


class FrozenURI(BaseURI):
    user = WriteOnceAttribute()
    host = WriteOnceAttribute()
    resource = WriteOnceAttribute()

    def __hash__(self):
        return hash((self.user, self.host, self.resource))


class Identity(object):
    def __init__(self, uri, display_name=None):
        self.uri = uri
        self.display_name = display_name

    def __eq__(self, other):
        if isinstance(other, Identity):
            return self.uri == other.uri and self.display_name == other.display_name
        else:
            return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    def __unicode__(self):
        if self.display_name is not None:
            return u'%s <%s>' % (self.display_name, self.uri)
        else:
            return u'%s' % self.uri

    def __str__(self):
        return unicode(self).encode('utf-8')


