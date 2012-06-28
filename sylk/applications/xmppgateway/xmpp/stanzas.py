# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from twisted.words.xish import domish


CHATSTATES_NS = 'http://jabber.org/protocol/chatstates'
RECEIPTS_NS   = 'urn:xmpp:receipts'
STANZAS_NS    = 'urn:ietf:params:xml:ns:xmpp-stanzas'
XML_NS        = 'http://www.w3.org/XML/1998/namespace'
MUC_NS        = 'http://jabber.org/protocol/muc'
MUC_USER_NS  = MUC_NS + '#user'
MUC_ADMIN_NS = MUC_NS + '#admin'
MUC_OWNER_NS = MUC_NS + '#owner'
MUC_ROOMINFO_NS = MUC_NS + '#roominfo'
MUC_CONFIG_NS   = MUC_NS + '#roomconfig'
MUC_REQUEST_NS  = MUC_NS + '#request'
MUC_REGISTER_NS = MUC_NS + '#register'
# TODO: review ^^


class BaseStanza(object):
    stanza_type = None     # to be defined by subclasses
    type = None

    def __init__(self, sender, recipient, id=None):
        self.sender = sender
        self.recipient = recipient
        self.id = id

    def to_xml_element(self):
        xml_element = domish.Element((None, self.stanza_type))
        xml_element['from'] = self.sender.uri.as_string('xmpp')
        xml_element['to'] = self.recipient.uri.as_string('xmpp')
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
        xml_element['from'] = self.sender.uri.as_string('xmpp')
        xml_element['to'] = self.recipient.uri.as_string('xmpp')
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

    def __init__(self, sender, recipient, id=None, use_receipt=False):
        super(BaseMessageStanza, self).__init__(sender, recipient, id=id)
        self.use_receipt = use_receipt

    def to_xml_element(self):
        xml_element = super(BaseMessageStanza, self).to_xml_element()
        if self.id is not None and self.recipient.uri.resource is not None and self.use_receipt:
            xml_element.addElement('request', defaultUri=RECEIPTS_NS)
        return xml_element


class NormalMessage(BaseMessageStanza):
    def __init__(self, sender, recipient, body, content_type='text/plain', id=None, use_receipt=False):
        super(NormalMessage, self).__init__(sender, recipient, id=id, use_receipt=use_receipt)
        self.body = body
        self.content_type = content_type

    def to_xml_element(self):
        xml_element = super(NormalMessage, self).to_xml_element()
        xml_element.addElement('body', content=self.body)    # TODO: what if content type is text/html ?
        return xml_element


class ChatMessage(BaseMessageStanza):
    type = 'chat'

    def __init__(self, sender, recipient, body, content_type='text/plain', id=None, use_receipt=True):
        super(ChatMessage, self).__init__(sender, recipient, id=id, use_receipt=use_receipt)
        self.body = body
        self.content_type = content_type

    def to_xml_element(self):
        xml_element = super(ChatMessage, self).to_xml_element()
        xml_element.addElement('active', defaultUri=CHATSTATES_NS)
        xml_element.addElement('body', content=self.body)    # TODO: what if content type is text/html ?
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

    def __init__(self, sender, recipient, body, content_type='text/plain', id=None):
        super(GroupChatMessage, self).__init__(sender, recipient, id=id, use_receipt=False)
        self.body = body
        self.content_type = content_type

    def to_xml_element(self):
        xml_element = super(GroupChatMessage, self).to_xml_element()
        xml_element.addElement('body', content=self.body)    # TODO: what if content type is text/html ?
        return xml_element


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


class AvailabilityPresence(BasePresenceStanza):
    def __init__(self, sender, recipient, available=True, show=None, statuses=None, priority=0, id=None):
        super(AvailabilityPresence, self).__init__(sender, recipient, id=id)
        self.available = available
        self.show = show
        self.priority = priority
        self.statuses = statuses or {}

    def _get_available(self):
        return self.__dict__['available']
    def _set_available(self, available):
        if available:
            self.type = None
        else:
            self.type = 'unavailable'
        self.__dict__['available'] = available
    available = property(_get_available, _set_available)
    del _get_available, _set_available

    @property
    def status(self):
        status = self.statuses.get(None)
        if status is None:
            try:
                status = self.statuses.itervalues().next()
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
            item['jid'] = self.jid.uri.as_string('xmpp')
        for code in self.muc_statuses:
            status = muc.addElement('status')
            status['code'] = code
        return xml_element


class MUCErrorPresence(ErrorStanza):
    def to_xml_element(self):
        xml_element = super(MUCErrorPresence, self).to_xml_element()
        xml_element.addElement('x', defaultUri=MUC_USER_NS)
        return xml_element

