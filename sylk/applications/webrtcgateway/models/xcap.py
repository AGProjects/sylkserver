from .jsonobjects import (ArrayProperty, BooleanProperty, JSONArray,
                          JSONObject, ObjectProperty, StringProperty)


class ContactURI(JSONObject):
    id = StringProperty()
    uri = StringProperty()
    type = StringProperty(optional=True)
    attributes = ObjectProperty(JSONObject, optional=True)
    default = BooleanProperty(optional=True)


class EventHandling(JSONObject):
    policy = StringProperty()
    subscribe = BooleanProperty()


class ContactURIs(JSONArray):
    item_type = ContactURI


class Contact(JSONObject):
    id = StringProperty()
    name = StringProperty()
    uris = ArrayProperty(ContactURIs)
    dialog = ObjectProperty(EventHandling)
    presence = ObjectProperty(EventHandling)
    attributes = ObjectProperty(JSONObject, optional=True)
    defaultUri = ObjectProperty(ContactURI, optional=True)

    def __init__(self, **data):
        if 'default_uri' in data and 'defaultUri' not in data:
            data['defaultUri'] = data.pop('default_uri')
        super().__init__(**data)

        if self.defaultUri is None and self.uris:
            self.defaultUri = self.uris[0]


class Contacts(JSONArray):
    item_type = Contact


class Group(JSONObject):
    id = StringProperty()
    name = StringProperty()
    attributes = ObjectProperty(JSONObject, optional=True)
    contacts = ArrayProperty(Contacts)


class Policy(JSONObject):
    id = StringProperty()
    name = StringProperty()
    uri = StringProperty()
    dialog = ObjectProperty(EventHandling)
    presence = ObjectProperty(EventHandling)
    attributes = ObjectProperty(JSONObject, optional=True)


class Policies(JSONArray):
    item_type = Policy


class Groups(JSONArray):
    item_type = Group


class AddressBook(JSONObject):
    contacts = ArrayProperty(Contacts)
    groups = ArrayProperty(Groups)
    policies = ArrayProperty(Policies)


class XCAPMapper:
    __classmap__ = {
        'contact': Contact,
        'group': Group,
        'policy': Policy,
        'addressbook': AddressBook,
    }

    @classmethod
    def from_payload(cls, payload, obj_type=None):
        if obj_type is None:
            if isinstance(payload, dict) and any(k in payload for k in ('contacts', 'groups', 'policies')):
                obj_type = 'addressbook'
            else:
                raise ValueError("Cannot detect type, please specify obj_type")

        model_class = cls.__classmap__.get(obj_type.lower())
        if model_class is None:
            raise ValueError(f"Unknown type: {obj_type}")

        return model_class(**payload)
