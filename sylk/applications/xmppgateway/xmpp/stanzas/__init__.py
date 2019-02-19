
import hashlib

from twisted.words.xish import domish

from sylk import __version__ as SYLK_VERSION
from sylk.applications.xmppgateway.util import html2text


CHATSTATES_NS = 'http://jabber.org/protocol/chatstates'
RECEIPTS_NS   = 'urn:xmpp:receipts'
STANZAS_NS    = 'urn:ietf:params:xml:ns:xmpp-stanzas'
XML_NS        = 'http://www.w3.org/XML/1998/namespace'
MUC_NS        = 'http://jabber.org/protocol/muc'
MUC_USER_NS   = MUC_NS + '#user'
CAPS_NS       = 'http://jabber.org/protocol/caps'


SYLK_HASH = hashlib.sha1('SylkServer-%s' % SYLK_VERSION).hexdigest()
SYLK_CAPS = []


class BaseStanza(object):
    stanza_type = None     # to be defined by subclasses
    type = None

    def __init__(self, sender, recipient, id=None):
        self.sender = sender
        self.recipient = recipient
        self.id = id

    def to_xml_element(self):
        xml_element = domish.Element((None, self.stanza_type))
        xml_element['from'] = unicode(self.sender.uri.as_xmpp_jid())
        xml_element['to'] = unicode(self.recipient.uri.as_xmpp_jid())
        if self.type:
            xml_element['type'] = self.type
        if self.id is not None:
            xml_element['id'] = self.id
        return xml_element


class ErrorStanza(object):
    """
    Stanza representing an error of another stanza. It's not a base stanza type on its own.
    """

    def __init__(self, stanza_type, sender, recipient, error_type, conditions, id=None):
        self.stanza_type = stanza_type
        self.sender = sender
        self.recipient = recipient
        self.id = id
        self.conditions = conditions
        self.error_type = error_type

    @classmethod
    def from_stanza(cls, stanza, error_type, conditions):
        # In error stanzas sender and recipient are swapped
        return cls(stanza.stanza_type, stanza.recipient, stanza.sender, error_type, conditions, id=stanza.id)

    def to_xml_element(self):
        xml_element = domish.Element((None, self.stanza_type))
        xml_element['from'] = unicode(self.sender.uri.as_xmpp_jid())
        xml_element['to'] = unicode(self.recipient.uri.as_xmpp_jid())
        xml_element['type'] = 'error'
        if self.id is not None:
            xml_element['id'] = self.id
        error_element = domish.Element((None, 'error'))
        error_element['type'] = self.error_type
        [error_element.addChild(domish.Element((ns, condition))) for condition, ns in self.conditions]
        xml_element.addChild(error_element)
        return xml_element


class BaseMessageStanza(BaseStanza):
    stanza_type = 'message'

    def __init__(self, sender, recipient, body=None, html_body=None, id=None, use_receipt=False):
        super(BaseMessageStanza, self).__init__(sender, recipient, id=id)
        self.use_receipt = use_receipt
        if body is not None and html_body is None:
            self.body = body
            self.html_body = None
        elif body is None and html_body is not None:
            self.body = html2text(html_body)
            self.html_body = html_body
        else:
            self.body = body
            self.html_body = html_body

    def to_xml_element(self):
        xml_element = super(BaseMessageStanza, self).to_xml_element()
        if self.id is not None and self.recipient.uri.resource is not None and self.use_receipt:
            xml_element.addElement('request', defaultUri=RECEIPTS_NS)
        if self.body is not None:
            xml_element.addElement('body', content=self.body)
            if self.html_body is not None:
                xml_element.addElement('html', content=self.html_body)
        return xml_element


class NormalMessage(BaseMessageStanza):
    def __init__(self, sender, recipient, body=None, html_body=None, id=None, use_receipt=False):
        if body is None and html_body is None:
            raise ValueError('either body or html_body need to be set')
        super(NormalMessage, self).__init__(sender, recipient, body, html_body, id, use_receipt)


class ChatMessage(BaseMessageStanza):
    type = 'chat'

    def __init__(self, sender, recipient, body=None, html_body=None, id=None, use_receipt=True):
        if body is None and html_body is None:
            raise ValueError('either body or html_body need to be set')
        super(ChatMessage, self).__init__(sender, recipient, body, html_body, id, use_receipt)

    def to_xml_element(self):
        xml_element = super(ChatMessage, self).to_xml_element()
        xml_element.addElement('active', defaultUri=CHATSTATES_NS)
        return xml_element


class ChatComposingIndication(BaseMessageStanza):
    type = 'chat'

    def __init__(self, sender, recipient, state, id=None, use_receipt=False):
        super(ChatComposingIndication, self).__init__(sender, recipient, id=id, use_receipt=use_receipt)
        self.state = state

    def to_xml_element(self):
        xml_element = super(ChatComposingIndication, self).to_xml_element()
        xml_element.addElement(self.state, defaultUri=CHATSTATES_NS)
        return xml_element


class GroupChatMessage(BaseMessageStanza):
    type = 'groupchat'

    def __init__(self, sender, recipient, body=None, html_body=None, id=None):
        # TODO: add timestamp
        if body is None and html_body is None:
            raise ValueError('either body or html_body need to be set')
        super(GroupChatMessage, self).__init__(sender, recipient, body, html_body, id, False)


class MessageReceipt(BaseMessageStanza):
    def __init__(self, sender, recipient, receipt_id, id=None):
        super(MessageReceipt, self).__init__(sender, recipient, id=id, use_receipt=False)
        self.receipt_id = receipt_id

    def to_xml_element(self):
        xml_element = super(MessageReceipt, self).to_xml_element()
        receipt_element = domish.Element((RECEIPTS_NS, 'received'))
        receipt_element['id'] = self.receipt_id
        xml_element.addChild(receipt_element)
        return xml_element


class BasePresenceStanza(BaseStanza):
    stanza_type = 'presence'


class IncomingInvitationMessage(BaseMessageStanza):
    def __init__(self, sender, recipient, invited_user, reason=None, id=None):
        super(IncomingInvitationMessage, self).__init__(sender, recipient, body=None, html_body=None, id=id, use_receipt=False)
        self.invited_user = invited_user
        self.reason = reason

    def to_xml_element(self):
        xml_element = super(IncomingInvitationMessage, self).to_xml_element()
        child = xml_element.addElement((MUC_USER_NS, 'x'))
        child.addElement('invite')
        child.invite['to'] = unicode(self.invited_user.uri.as_xmpp_jid())
        if self.reason:
            child.invite.addElement('reason', content=self.reason)
        return xml_element


class OutgoingInvitationMessage(BaseMessageStanza):
    def __init__(self, sender, recipient, originator, reason=None, id=None):
        super(OutgoingInvitationMessage, self).__init__(sender, recipient, body=None, html_body=None, id=id, use_receipt=False)
        self.originator = originator
        self.reason = reason

    def to_xml_element(self):
        xml_element = super(OutgoingInvitationMessage, self).to_xml_element()
        child = xml_element.addElement((MUC_USER_NS, 'x'))
        child.addElement('invite')
        child.invite['from'] = unicode(self.originator.uri.as_xmpp_jid())
        if self.reason:
            child.invite.addElement('reason', content=self.reason)
        return xml_element


class AvailabilityPresence(BasePresenceStanza):
    def __init__(self, sender, recipient, available=True, show=None, statuses=None, priority=0, id=None):
        super(AvailabilityPresence, self).__init__(sender, recipient, id=id)
        self.available = available
        self.show = show
        self.priority = priority
        self.statuses = statuses or {}

    @property
    def available(self):
        return self.__dict__['available']

    @available.setter
    def available(self, available):
        if available:
            self.type = None
        else:
            self.type = 'unavailable'
        self.__dict__['available'] = available

    @property
    def status(self):
        status = self.statuses.get(None)
        if status is None:
            try:
                status = next(self.statuses.itervalues())
            except StopIteration:
                pass
        return status

    def to_xml_element(self):
        xml_element = super(BasePresenceStanza, self).to_xml_element()
        if self.available:
            if self.show is not None:
                xml_element.addElement('show', content=self.show)
            if self.priority != 0:
                xml_element.addElement('priority', content=unicode(self.priority))
            caps = xml_element.addElement('c', defaultUri=CAPS_NS)
            caps['node'] = 'http://sylkserver.com'
            caps['hash'] = 'sha-1'
            caps['ver'] = SYLK_HASH
            if SYLK_CAPS:
                caps['ext'] = ' '.join(SYLK_CAPS)
        for lang, text in self.statuses.iteritems():
            status = xml_element.addElement('status', content=text)
            if lang:
                status[(XML_NS, 'lang')] = lang
        return xml_element


class SubscriptionPresence(BasePresenceStanza):
    def __init__(self, sender, recipient, type, id=None):
        super(SubscriptionPresence, self).__init__(sender, recipient, id=id)
        self.type = type


class ProbePresence(BasePresenceStanza):
    type = 'probe'


class MUCAvailabilityPresence(AvailabilityPresence):
    def __init__(self, sender, recipient, available=True, show=None, statuses=None, priority=0, id=None, affiliation=None, jid=None, role=None, muc_statuses=None):
        super(MUCAvailabilityPresence, self).__init__(sender, recipient, available, show, statuses, priority, id)
        self.affiliation = affiliation or 'member'
        self.role = role or 'participant'
        self.muc_statuses = muc_statuses or []
        self.jid = jid

    def to_xml_element(self):
        xml_element = super(MUCAvailabilityPresence, self).to_xml_element()
        muc = xml_element.addElement('x', defaultUri=MUC_USER_NS)
        item = muc.addElement('item')
        if self.affiliation:
            item['affiliation'] = self.affiliation
        if self.role:
            item['role'] = self.role
        if self.jid:
            item['jid'] = unicode(self.jid.uri.as_xmpp_jid())
        for code in self.muc_statuses:
            status = muc.addElement('status')
            status['code'] = code
        return xml_element


class MUCErrorPresence(ErrorStanza):
    def to_xml_element(self):
        xml_element = super(MUCErrorPresence, self).to_xml_element()
        xml_element.addElement('x', defaultUri=MUC_USER_NS)
        return xml_element

