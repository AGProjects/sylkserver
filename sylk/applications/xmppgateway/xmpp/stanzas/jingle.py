
# Copyright (c) AG Projects
# Copyright (c) Uday Verma
# Copyright (c) Ralph Meijer.
#

"""
XMPP Jingle Protocol.

This protocol is specified in
    * XEP-0166 - http://xmpp.org/extensions/xep-0166.html
    * XEP-0167 - http://xmpp.org/extensions/xep-0167.html
    * XEP-0176 - http://xmpp.org/extensions/xep-0176.html
    * XEP-0177 - http://xmpp.org/extensions/xep-0177.html
"""

from twisted.words.xish import domish
from twisted.words.protocols.jabber import error

from wokkel.generic import Request
from wokkel.subprotocols import IQHandlerMixin, XMPPHandler


NS_JINGLE_BASE = 'urn:xmpp:jingle'

NS_JINGLE = NS_JINGLE_BASE + ':1'
NS_JINGLE_ERRORS = NS_JINGLE_BASE + ':errors:1'
NS_JINGLE_APPS_BASE = NS_JINGLE_BASE + ':apps'

NS_JINGLE_APPS_RTP       = NS_JINGLE_APPS_BASE + ':rtp:1'
NS_JINGLE_APPS_RTP_INFO  = NS_JINGLE_APPS_BASE + ':rtp:info:1'
NS_JINGLE_APPS_RTP_AUDIO = NS_JINGLE_APPS_BASE + ':rtp:audio'
NS_JINGLE_APPS_RTP_VIDEO = NS_JINGLE_APPS_BASE + ':rtp:video'

NS_JINGLE_ICE_UDP_TRANSPORT = NS_JINGLE_BASE + ':transports:ice-udp:1'
NS_JINGLE_RAW_UDP_TRANSPORT = NS_JINGLE_BASE + ':transports:raw-udp:1'

# XPath for Jingle IQ requests
IQ_JINGLE_REQUEST = '/iq[@type="get" or @type="set"]/jingle[@xmlns="' + NS_JINGLE + '"]'


class Parameter(object):
    """
    A class representing a payload parameter
    """
    def __init__(self, name, value):
        self.name, self.value = name, value

    @classmethod
    def fromElement(cls, element):
        return cls(element.getAttribute('name'), element.getAttribute('value'))

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'parameter'))
        element['name'] = self.name
        element['value'] = self.value or ''
        return element


class Crypto(object):
    """
    A crypto method which makes up the encryption to be used
    """
    def __init__(self, crypto_suite, key_params, tag, session_params=None):
        self.crypto_suite, self.key_params, self.tag, self.session_params = crypto_suite, key_params, tag, session_params

    @classmethod
    def fromElement(cls, element):
        return cls(element.getAttribute('crypto-suite'),
                element.getAttribute('key-params'),
                element.getAttribute('tag'),
                element.getAttribute('session-params'))

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'crypto'))
        element['crypto-suite'] = self.crypto_suite
        element['key-params'] = self.key_params
        if self.session_params:
            element['session-params'] = self.session_params
        element['tag'] = self.tag
        return element


class Encryption(object):
    """
    A class representing encryption method
    """
    def __init__(self, required=False, cryptos=None):
        self.required, self.cryptos = required, cryptos or []

    @classmethod
    def fromElement(cls, element):
        cryptos = []
        for child in element.elements():
            if child.name == 'crypto':
                cryptos.append(Crypto.fromElement(child))
            # TODO: parse ZRTP elements
        required = element.hasAttribute('required') and (element.getAttribute('required').lower() in ['true', '1'])
        return cls(required, cryptos)

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'encryption'))
        if self.required:
            element['required'] = '1'

        for c in self.cryptos:
            element.addChild(c.toElement(defaultUri))
        return element


class Bandwidth(object):
    """
    A class representing the bandwidth element
    """
    def __init__(self, typ, value):
        self.typ, self.value = typ, value

    @classmethod
    def fromElement(cls, element):
        return cls(element.getAttribute('type'), str(element))

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'bandwidth'))
        element['type'] = self.typ
        element.addContent(self.value)
        return element


class PayloadType(object):
    """
    A class representing payload type
    """
    def __init__(self, id, name, clockrate=0, channels=0, maxptime=None, ptime=None, parameters=None):
        self.id, self.name, self.clockrate, self.channels, \
        self.maxptime, self.ptime, self.parameters = \
                id, name, clockrate, channels, maxptime, ptime, parameters or []

    @classmethod
    def fromElement(cls, element):
        def _sga(v, t):
            """
            SafeGetAttribute
            """
            try:
                return t(element.getAttribute(v))
            except (TypeError, ValueError):
                return None

        params = []
        for c in element.children:
            params.append(Parameter.fromElement(c))

        return cls(int(element.getAttribute('id')),
                element.getAttribute('name'),
                _sga('clockrate', int) or 0,
                _sga('channels', int) or 0,
                _sga('maxptime', int) or 0,
                _sga('ptime', int) or 0,
                params)

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'payload-type'))

        def _aiv(k, v):
            """
            AppendIfValid
            """
            if v:
                element[k] = str(v)

        element['id'] = str(self.id)

        _aiv('name', self.name)
        _aiv('clockrate', self.clockrate)
        _aiv('channels', self.channels)
        _aiv('maxptime', self.maxptime)
        _aiv('ptime', self.ptime)

        for p in self.parameters:
            element.addChild(p.toElement())

        return element


class ICECandidate(object):
    """
    A class representing an ICE candidate
    """
    def __init__(self, component, foundation, generation,
            id, ip, network, port, priority, protocol, typ,
            related_addr=None, related_port=0):
        self.component, self.foundation, self.generation, \
            self.id, self.ip, self.network, self.port, self.priority, \
            self.protocol, self.typ, self.related_addr, self.related_port = \
                    component, foundation, generation, \
                    id, ip, network, port, priority, protocol, typ, \
                    related_addr, related_port

    @classmethod
    def fromElement(cls, element):
        def _gas(*names):
            """
            GetAttributeS
            """
            def default_val(t):
                return None if t is str else t()

            return [(t(element.getAttribute(name)) if element.hasAttribute(name) else default_val(t)) for name, t in names]

        return cls(*_gas(('component', int), ('foundation', int),
                         ('generation', int), ('id', str), ('ip', str),
                         ('network', int), ('port', int), ('priority', int), ('protocol', str),
                         ('type', str), ('rel-addr', str), ('rel-port', int)))

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'candidate'))
        def _aas(*names):
            """
            AddAttributeS
            """
            for n, v in names:
                if v is not None:
                    element[n] = str(v)

        _aas(*[('component', self.component),
            ('foundation', self.foundation),
            ('generation', self.generation),
            ('id', self.id),
            ('ip', self.ip),
            ('network', self.network),
            ('port', self.port),
            ('priority', self.priority),
            ('protocol', self.protocol),
            ('type', self.typ),
            ('rel-addr', self.related_addr),
            ('rel-port', self.related_port)])
        return element


class UDPCandidate(object):
    """
    A class representing a UDP candidate
    """
    def __init__(self, component, generation, id_, ip, port, protocol, type=None):
        self.component = component
        self.generation = generation
        self.id = id_
        self.ip = ip
        self.port = port
        self.protocol = protocol
        self.type = type

    @classmethod
    def fromElement(cls, element):
        def _gas(*names):
            """
            GetAttributeS
            """
            def default_val(t):
                return None if t is str else t()

            return [(t(element.getAttribute(name)) if element.hasAttribute(name) else default_val(t)) for name, t in names]

        return cls(*_gas(('component', int), ('generation', int),
                         ('id', str), ('ip', str),
                         ('port', int), ('protocol', str),
                         ('type', str)))

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'candidate'))
        def _aas(*names):
            """
            AddAttributeS
            """
            for n, v in names:
                if v:
                    element[n] = str(v)

        _aas(*[('component', self.component),
            ('generation', self.generation),
            ('id', self.id),
            ('ip', self.ip),
            ('port', self.port),
            ('protocol', self.protocol),
            ('type', self.type)])
        return element


class ICERemoteCandidate(object):
    """
    A class represeting a remote candidate entity
    """
    def __init__(self, component, ip, port):
        self.component, self.ip, self.port = component, ip, port

    @classmethod
    def fromElement(cls, element):
        return cls(int(element.getAttribute('component') or '0'),
                element.getAttribute('ip'),
                int(element.getAttribute('port') or '0'))

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri, 'remote-candidate'))
        element['component'] = str(self.component)
        element['ip'] = self.ip
        element['port'] = str(self.port)
        return element


class IceUdpTransport(object):
    """
    Represents the ICE-UDP transport type
    """
    def __init__(self, pwd=None, ufrag=None, candidates=None, remote_candidate=None):
        self.password, self.ufrag, self.candidates, self.remote_candidate = \
                pwd, ufrag, candidates or [], remote_candidate

    @classmethod
    def fromElement(cls, element):
        password = element.getAttribute('pwd') or None
        ufrag = element.getAttribute('ufrag') or None

        candidates = []
        remote_candidate = None
        for child in element.elements():
            if child.name == 'remote-candidate' and remote_candidate is None:
                remote_candidate = ICERemoteCandidate.fromElement(child)
            elif child.name == 'candidate':
                candidates.append(ICECandidate.fromElement(child))

        return cls(pwd=password, ufrag=ufrag, candidates=candidates,
                remote_candidate=remote_candidate)

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri or NS_JINGLE_ICE_UDP_TRANSPORT, 'transport'))
        if self.password:
            element['pwd'] = self.password
        if self.ufrag:
            element['ufrag'] = self.ufrag

        if self.remote_candidate:
            element.addChild(self.remote_candidate.toElement())
        elif self.candidates:
            for c in self.candidates:
                element.addChild(c.toElement())

        return element


class RawUdpTransport(object):
    """
    Represents the Raw-UDP transport type
    """
    def __init__(self, candidates=None):
        self.candidates = candidates or []

    @classmethod
    def fromElement(cls, element):
        candidates = []
        for child in element.elements():
            if child.name == 'candidate':
                candidates.append(UDPCandidate.fromElement(child))

        return cls(candidates=candidates)

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri or NS_JINGLE_RAW_UDP_TRANSPORT, 'transport'))
        for c in self.candidates:
            element.addChild(c.toElement())
        return element


class RTPDescription(object):
    """
    A class representing a RTP description
    """
    def __init__(self, name=None, media=None, ssrc=None, payloads=None,
            encryption=None, bandwidth=None):
        self.name, self.media, self.ssrc, self.payloads, \
                self.encryption, self.bandwidth = \
                name, media, ssrc, payloads or [], encryption, bandwidth

    @classmethod
    def fromElement(cls, element):
        plds = []
        encryption, bandwidth = None, None

        for child in element.elements():
            if child.name == 'payload-type':
                plds.append(PayloadType.fromElement(child))
            if child.name == 'encryption':
                encryption = Encryption.fromElement(child)
            if child.name == 'bandwidth':
                bandwidth = Bandwidth.fromElement(child)

        return cls(element.getAttribute('name'),
                element.getAttribute('media'),
                element.getAttribute('ssrc'), plds, encryption,
                bandwidth)

    def toElement(self, defaultUri=None):
        element = domish.Element((defaultUri or NS_JINGLE_APPS_RTP, 'description'))
        if self.name:
            element['name'] = self.name
        if self.media:
            element['media'] = self.media
        for p in self.payloads:
            element.addChild(p.toElement(defaultUri))
        if self.encryption:
            element.addChild(self.encryption.toElement(defaultUri))
        if self.bandwidth:
            element.addChild(self.bandwidth.toElement(defaultUri))

        return element


class Content(object):
    """
    A class indicating a single content item within a jingle request.
    """
    def __init__(self, creator, name, disposition=None, senders=None):
        self.creator, self.name, self.disposition, self.senders = \
                creator, name, disposition, senders
        self.description = None
        self.transport = None

    @classmethod
    def fromElement(cls, element):
        creator = element.getAttribute('creator')
        name = element.getAttribute('name')
        disposition = element.getAttribute('disposition')
        senders = element.getAttribute('senders')

        description, transport = None, None
        for c in element.elements():
            if c.name == 'description' and c.uri == NS_JINGLE_APPS_RTP:
                description = RTPDescription.fromElement(c)
            elif c.name == 'transport' and c.uri == NS_JINGLE_ICE_UDP_TRANSPORT:
                transport = IceUdpTransport.fromElement(c)
            elif c.name == 'transport' and c.uri == NS_JINGLE_RAW_UDP_TRANSPORT:
                transport = RawUdpTransport.fromElement(c)

        ret = cls(creator, name, disposition, senders)
        ret.description = description
        ret.transport = transport
        return ret

    def toElement(self):
        element = domish.Element((None, 'content'))
        element['creator'] = self.creator
        element['name'] = self.name
        if self.disposition:
            element['disposition'] = self.disposition
        if self.senders:
            element['senders'] = self.senders
        if self.description:
            element.addChild(self.description.toElement())
        if self.transport:
            element.addChild(self.transport.toElement())
        return element


class EmptyType(unicode):
    @classmethod
    def fromElement(cls, element):
        return cls(element.name)

    def toElement(self):
        return domish.Element((None, self))

class ReasonType(EmptyType):
    pass

class AlternativeSessionReason(unicode):
    def __new__(cls, value):
        obj = unicode.__new__(cls, 'alternative-session')
        obj.sid = value
        return obj

    @classmethod
    def fromElement(cls, element):
        return cls(element.firstChildElement().children[0])

    def toElement(self):
        element = domish.Element((None, self))
        element.addElement('sid', content=self.sid)
        return element

class Reason(object):
    def __init__(self, reason, text=None):
        self.value = reason
        self.text = text

    @classmethod
    def fromElement(cls, element):
        reason = None
        text = None
        for c in element.children:
            if c.name == 'text':
                text = c.children[0]
            elif c.name == 'alternative-session':
                reason = AlternativeSessionReason.fromElement(c)
            else:
                reason = ReasonType.fromElement(c)
        return cls(reason, text)

    def toElement(self):
        element = domish.Element((None, 'reason'))
        element.addChild(self.value.toElement())
        if self.text:
            element.addElement('text', content=self.text)
        return element

class Info(unicode):

    @classmethod
    def fromElement(cls, element):
        return cls(element.name)

    def toElement(self):
        return domish.Element((NS_JINGLE_APPS_RTP_INFO, self))

class MuteInfo(Info):

    def __new__(cls, value, creator, name):
        obj = unicode.__new__(cls, value)
        obj.creator = creator
        obj.name = name
        return obj

    @classmethod
    def fromElement(cls, element):
        return cls(element.name, element['creator'], element['name'])

    def toElement(self):
        element = super(MuteInfo, self).toElement()
        element['creator'] = self.creator
        element['name'] = self.name
        return element


class Jingle(object):
    """
    A class representing a Jingle element within an IQ request
    """
    def __init__(self, action, sid, initiator=None, responder=None, content=None, reason=None, info=None):
        self.action = action
        self.sid = sid
        self.initiator = initiator
        self.responder = responder
        self.reason = reason
        self.info = info
        if not hasattr(content, '__iter__'):
            if content is not None:
                self.content = [content]
            else:
                self.content = []
        else:
            self.content = content

    @classmethod
    def fromElement(cls, element):
        action = element.getAttribute('action')
        initiator = element.getAttribute('initiator')
        responder = element.getAttribute('responder')
        sid = element.getAttribute('sid')

        content = []
        reason = None
        info = None
        for c in element.elements():
            if c.name == 'content':
                content.append(Content.fromElement(c))
            elif c.name == 'reason':
                reason = Reason.fromElement(c)
            elif c.uri == NS_JINGLE_APPS_RTP_INFO:
                if c.name in ('mute', 'unmute'):
                    info = MuteInfo.fromElement(c)
                else:
                    info = Info.fromElement(c)
        return cls(action, sid, initiator, responder, content=content, reason=reason, info=info)

    def toElement(self):
        element = domish.Element((NS_JINGLE, 'jingle'))
        element['action'] = self.action
        element['sid'] = self.sid
        if self.initiator:
            element['initiator'] = self.initiator
        if self.responder:
            element['responder'] = self.responder
        for c in self.content:
            element.addChild(c.toElement())
        if self.reason:
            element.addChild(self.reason.toElement())
        if self.info:
            element.addChild(self.info.toElement())
        return element


class JingleIq(Request):
    stanzaKind = 'iq'
    stanzaType = 'set'
    timeout = None
    childParsers = {(NS_JINGLE, 'jingle'): '_parseJingleElement'}

    def __init__(self, sender=None, recipient=None, jingle=None):
        Request.__init__(self, recipient, sender, self.stanzaType)
        self.jingle = jingle

    def _parseJingleElement(self, element):
        self.jingle = Jingle.fromElement(element)

    def toElement(self):
        element = Request.toElement(self)
        element.addChild(self.jingle.toElement())
        return element


class JingleHandler(XMPPHandler, IQHandlerMixin):

    iqHandlers = {IQ_JINGLE_REQUEST: '_onJingleRequest'}

    def connectionInitialized(self):
        self.xmlstream.addObserver(IQ_JINGLE_REQUEST, self.handleRequest)

    def sessionTerminate(self, sender, recipient, sid, reason=None):
        jingle = Jingle('session-terminate', sid, reason=reason)
        return JingleIq(sender=sender, recipient=recipient, jingle=jingle)

    def sessionInfo(self, sender, recipient, sid, info=None):
        jingle = Jingle('session-info', sid, info=info)
        return JingleIq(sender=sender, recipient=recipient, jingle=jingle)

    def sessionAccept(self, sender, recipient, payload):
        payload.action = 'session-accept'
        return JingleIq(sender=sender, recipient=recipient, jingle=payload)

    def sessionInitiate(self, sender, recipient, payload):
        payload.action = 'session-initiate'
        return JingleIq(sender=sender, recipient=recipient, jingle=payload)

    def _onJingleRequest(self, iq):
        request = JingleIq.fromElement(iq)
        method_name = 'on'+''.join(item.capitalize() for item in request.jingle.action.lower().split('-'))
        handler = getattr(self, method_name, None)
        if callable(handler):
            handler(request)
        else:
            raise error.StanzaError('bad-request')

