
import hashlib
import codecs
import random
import string

from application.python.descriptor import WriteOnceAttribute
from sipsimple.core import BaseSIPURI, SIPURI, SIPCoreError
from twisted.words.protocols.jabber.jid import JID


sylkserver_prefix = hashlib.md5(b'sylkserver').hexdigest()


def generate_sylk_resource():
    r = 'sylk-'+''.join(random.choice(string.ascii_letters+string.digits) for x in range(32))
    return codecs.encode(r.encode('utf-8'), 'hex')


def is_sylk_resource(r):
    if r.startswith('urn:uuid:') or len(r) != 74:
        return False
    try:
        decoded = codecs.decode(r, 'hex')
    except TypeError:
        return False
    else:
        return decoded.startswith('sylk-')


def encode_resource(r):
    return codecs.encode(r.encode('utf-8'), 'hex')


def decode_resource(r):
    return codecs.decode(r, 'hex').decode('utf-8')


class BaseURI(object):
    def __init__(self, user, host, resource=None):
        self.user = user
        self.host = host
        self.resource = resource

    @classmethod
    def parse(cls, value):
        if isinstance(value, BaseSIPURI):
            user = value.user.decode()
            host = value.host.decode()
            resource = value.parameters.get('gr', '') or None
            return cls(user, host, resource)
        elif isinstance(value, JID):
            user = value.user
            host = value.host
            resource = value.resource
            return cls(user, host, resource)
        elif not isinstance(value, str):
            raise TypeError('uri needs to be a string')
        if not value.startswith(('sip:', 'sips:', 'xmpp:')):
            raise ValueError('invalid uri scheme for %s' % value)
        if value.startswith(('sip:', 'sips:')):
            try:
                uri = SIPURI.parse(value)
            except SIPCoreError:
                raise ValueError('invalid SIP uri: %s' % value)
            user = uri.user.decode()
            host = uri.host.decode()
            resource = uri.parameters.get('gr', '') or None
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
        return JID(tuple=(self.user, self.host, self.resource))

    def __eq__(self, other):
        if isinstance(other, BaseURI):
            return self.user == other.user and self.host == other.host and self.resource == other.resource
        elif isinstance(other, str):
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
        return '%s@%s' % (self.user, self.host)

    def __str__(self):
        return '%s@%s' % (self.user, self.host)


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
            return '%s <%s>' % (self.display_name, self.uri)
        else:
            return '%s' % self.uri

    def __str__(self):
        return str(self).encode('utf-8')

