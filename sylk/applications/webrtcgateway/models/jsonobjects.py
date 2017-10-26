

class Validator(object):
    def validate(self, value):
        """Check value and raise ValueError if invalid, else return the (possibly modified) value"""
        return value


class CompositeValidator(Validator):
    def __init__(self, *validators):
        if len(validators) < 2:
            raise TypeError('need at least two validators to create a CompositeValidator')
        if not all(isinstance(validator, Validator) for validator in validators):
            raise ValueError('validators need to be Validator instances')
        self.validators = validators

    def validate(self, value):
        for validator in self.validators:
            value = validator.validate(value)
        return value


class MultiType(tuple):
    """
    A collection of types for which isinstance(obj, multi_type) returns True if 'obj'
    is an instance of any of the types in the multi_type.
    Instantiating the multi_type will instantiate the first type in the multi_type.
    """

    # noinspection PyArgumentList
    def __new__(cls, *args):
        if not args:
            raise ValueError('{.__name__} must have at least one type'.format(cls))
        instance = super(MultiType, cls).__new__(cls, args)
        instance.__name__ = ', '.join(cls.__name__ for cls in args)
        instance.main_type = args[0]
        return instance

    def __call__(self, value):
        return self.main_type(value)


class AbstractProperty(object):
    data_type = object

    def __init__(self, optional=False, default=None, validator=None):
        if validator is not None and not isinstance(validator, Validator):
            raise TypeError('validator should be a Validator instance or None')
        self.default = default
        self.optional = optional
        self.validator = validator
        self.name = None  # will be set by the JSONObjectType metaclass when associating properties with objects

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.name, self.default)  # mandatory properties are guaranteed to be present, only optional ones can be missing

    def __set__(self, instance, value):
        if value is None and self.optional:
            instance.__dict__[self.name] = None
        else:
            instance.__dict__[self.name] = self._parse(value)

    def __delete__(self, instance):
        if not self.optional:
            raise AttributeError('Cannot delete mandatory property {property.name!r} of object {instance.__class__.__name__!r}'.format(instance=instance, property=self))
        try:
            del instance.__dict__[self.name]
        except KeyError:
            raise AttributeError(self.name)

    def _parse(self, value):
        if not isinstance(value, self.data_type):
            raise ValueError('Invalid value for {property.name!r} property: {value!r}'.format(property=self, value=value))
        if self.validator is not None:
            value = self.validator.validate(value)
        return value


class BooleanProperty(AbstractProperty):
    data_type = bool


class IntegerProperty(AbstractProperty):
    data_type = int, long


class NumberProperty(AbstractProperty):
    data_type = int, long, float


class StringProperty(AbstractProperty):
    data_type = str, unicode


class ArrayProperty(AbstractProperty):
    data_type = list, tuple

    def __init__(self, array_type, optional=False):
        if not issubclass(array_type, JSONArray):
            raise TypeError('array_type should be a subclass of JSONArray')
        super(ArrayProperty, self).__init__(optional=optional, default=None, validator=None)
        self.array_type = array_type

    def _parse(self, value):
        if type(value) is self.array_type:
            return value
        elif isinstance(value, self.data_type):
            return self.array_type(value)
        else:
            raise ValueError('Invalid value for {property.name!r} property: {value!r}'.format(property=self, value=value))


class ObjectProperty(AbstractProperty):
    data_type = dict

    def __init__(self, object_type, optional=False):
        if not issubclass(object_type, JSONObject):
            raise TypeError('object_type should be a subclass of JSONObject')
        super(ObjectProperty, self).__init__(optional=optional, default=None, validator=None)
        self.object_type = object_type

    def _parse(self, value):
        if type(value) is self.object_type:
            return value
        elif isinstance(value, self.data_type):
            return self.object_type(**value)
        else:
            raise ValueError('Invalid value for {property.name!r} property: {value!r}'.format(property=self, value=value))


class PropertyContainer(object):
    def __init__(self, cls):
        self.__dict__.update({item.name: item for cls in reversed(cls.__mro__) for item in cls.__dict__.itervalues() if isinstance(item, AbstractProperty)})

    def __getitem__(self, name):
        return self.__dict__[name]

    def __contains__(self, name):
        return name in self.__dict__

    def __iter__(self):
        return self.__dict__.itervalues()

    @property
    def names(self):
        return set(self.__dict__)

    # noinspection PyShadowingBuiltins
    def items(self, instance):
        return [(property.name, property.__get__(instance, None)) for property in self.__dict__.itervalues()]


class JSONObjectType(type):
    # noinspection PyShadowingBuiltins
    def __init__(cls, name, bases, dictionary):
        super(JSONObjectType, cls).__init__(name, bases, dictionary)
        for name, property in ((name, item) for name, item in dictionary.iteritems() if isinstance(item, AbstractProperty)):
            property.name = name
        cls.__properties__ = PropertyContainer(cls)


class JSONObject(object):
    __metaclass__ = JSONObjectType

    # noinspection PyShadowingBuiltins
    def __init__(self, **data):
        for property in self.__properties__:
            if property.name in data:
                property.__set__(self, data[property.name])
            elif not property.optional:
                raise ValueError('Mandatory property {property.name!r} of {object.__class__.__name__!r} object is missing'.format(property=property, object=self))

    @property
    def __data__(self):
        return {name: value.__data__ if isinstance(value, (JSONArray, JSONObject)) else value for name, value in self.__properties__.items(self) if value is not None or name in self.__dict__}


class ArrayParser(object):
    def __init__(self, cls):
        self.item_type = MultiType(*cls.item_type) if isinstance(cls.item_type, (list, tuple)) else cls.item_type
        self.item_validator = cls.item_validator  # this is only used for primitive item types
        if isinstance(self.item_type, JSONObjectType):
            self.parse_item = self.__parse_object_item
            self.parse_list = self.__parse_object_list
        elif isinstance(self.item_type, JSONArrayType):
            self.parse_item = self.__parse_array_item
            self.parse_list = self.__parse_array_list
        else:
            self.parse_item = self.__parse_primitive_item
            self.parse_list = self.__parse_primitive_list

    def __parse_primitive_item(self, item):
        if not isinstance(item, self.item_type):
            raise ValueError('Invalid value for {type.__name__}: {item!r}'.format(type=self.item_type, item=item))
        if self.item_validator is not None:
            item = self.item_validator.validate(item)
        return item

    def __parse_primitive_list(self, iterable):
        item_type = self.item_type
        for item in iterable:
            if not isinstance(item, item_type):
                raise ValueError('Invalid value for {type.__name__}: {item!r}'.format(type=item_type, item=item))
            if self.item_validator is not None:  # note: can be optimized by moving this test outside the loop (not sure if the decreased readability is worth it)
                item = self.item_validator.validate(item)
            yield item

    def __parse_array_item(self, item):
        try:
            return item if type(item) is self.item_type else self.item_type(item)
        except TypeError:
            raise ValueError('Invalid value for {type.__name__}: {item!r}'.format(type=self.item_type, item=item))

    def __parse_array_list(self, iterable):
        item_type = self.item_type
        for item in iterable:
            try:
                yield item if type(item) is item_type else item_type(item)
            except TypeError:
                raise ValueError('Invalid value for {type.__name__}: {item!r}'.format(type=item_type, item=item))

    def __parse_object_item(self, item):
        try:
            return item if type(item) is self.item_type else self.item_type(**item)
        except TypeError:
            raise ValueError('Invalid value for {type.__name__}: {item!r}'.format(type=self.item_type, item=item))

    def __parse_object_list(self, iterable):
        item_type = self.item_type
        for item in iterable:
            try:
                yield item if type(item) is item_type else item_type(**item)
            except TypeError:
                raise ValueError('Invalid value for {type.__name__}: {item!r}'.format(type=item_type, item=item))


class JSONArrayType(type):
    item_type = object

    item_validator = None
    list_validator = None

    def __init__(cls, name, bases, dictionary):
        super(JSONArrayType, cls).__init__(name, bases, dictionary)
        if cls.item_validator is not None and isinstance(cls.item_type, (JSONArrayType, JSONObjectType)):
            raise TypeError('item_validator is not used for JSONArray and JSONObject item types as they have their own validators')
        if cls.item_validator is not None and not isinstance(cls.item_validator, Validator):
            raise TypeError('item_validator should be a Validator instance or None')
        if cls.list_validator is not None and not isinstance(cls.list_validator, Validator):
            raise TypeError('list_validator should be a Validator instance or None')
        cls.parser = ArrayParser(cls)


class JSONArray(object):
    __metaclass__ = JSONArrayType

    item_type = object

    item_validator = None  # this should only be defined for primitive item types
    list_validator = None

    def __init__(self, iterable):
        if isinstance(iterable, basestring):  # prevent iterable primitive types from being interpreted as arrays
            raise ValueError('Invalid value for {.__class__.__name__}: {!r}'.format(self, iterable))
        items = list(self.parser.parse_list(iterable))
        if self.list_validator is not None:
            items = self.list_validator.validate(items)
        self.__items__ = items

    @property
    def __data__(self):
        return [item.__data__ for item in self.__items__] if isinstance(self.item_type, (JSONArrayType, JSONObjectType)) else self.__items__[:]

    def __repr__(self):
        return '{0.__class__.__name__}({0.__items__!r})'.format(self)

    def __contains__(self, item):
        return item in self.__items__

    def __iter__(self):
        return iter(self.__items__)

    def __len__(self):
        return len(self.__items__)

    def __reversed__(self):
        return reversed(self.__items__)

    __hash__ = None

    def __getitem__(self, index):
        return self.__items__[index]

    def __setitem__(self, index, value):
        value = self.parser.parse_item(value)
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone[index] = value
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__[index] = value

    def __delitem__(self, index):
        if self.list_validator is not None:
            clone = self.__items__[:]
            del clone[index]
            self.__items__ = self.list_validator.validate(clone)
        else:
            del self.__items__[index]

    def __getslice__(self, i, j):
        return self.__items__[i:j]

    def __setslice__(self, i, j, sequence):
        sequence = list(self.parser.parse_list(sequence))
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone[i:j] = sequence
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__[i:j] = sequence

    def __delslice__(self, i, j):
        if self.list_validator is not None:
            clone = self.__items__[:]
            del clone[i:j]
            self.__items__ = self.list_validator.validate(clone)
        else:
            del self.__items__[i:j]

    def __add__(self, other):
        if isinstance(other, JSONArray):
            return self.__class__(self.__items__ + other.__items__)
        else:
            return self.__class__(self.__items__ + other)

    def __radd__(self, other):
        if isinstance(other, JSONArray):
            return self.__class__(other.__items__ + self.__items__)
        else:
            return self.__class__(other + self.__items__)

    def __iadd__(self, other):
        if isinstance(other, JSONArray) and self.item_type == other.item_type:
            items = other.__items__
        else:
            items = list(self.parser.parse_list(other))
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone += items
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__ += items
        return self

    def __mul__(self, n):
        return self.__class__(self.__items__ * n)

    def __rmul__(self, n):
        return self.__class__(self.__items__ * n)

    def __imul__(self, n):
        if self.list_validator is not None:
            self.__items__ = self.list_validator.validate(n * self.__items__)
        else:
            self.__items__ *= n
        return self

    def __eq__(self, other):
        return self.__items__ == other.__items__ if isinstance(other, JSONArray) else self.__items__ == other

    def __ne__(self, other):
        return self.__items__ != other.__items__ if isinstance(other, JSONArray) else self.__items__ != other

    def __lt__(self, other):
        return self.__items__ < other.__items__ if isinstance(other, JSONArray) else self.__items__ < other

    def __le__(self, other):
        return self.__items__ <= other.__items__ if isinstance(other, JSONArray) else self.__items__ <= other

    def __gt__(self, other):
        return self.__items__ > other.__items__ if isinstance(other, JSONArray) else self.__items__ > other

    def __ge__(self, other):
        return self.__items__ >= other.__items__ if isinstance(other, JSONArray) else self.__items__ >= other

    def __format__(self, format_spec):
        return self.__items__.__format__(format_spec)

    def index(self, value, *args):
        return self.__items__.index(value, *args)

    def count(self, value):
        return self.__items__.count(value)

    def append(self, value):
        value = self.parser.parse_item(value)
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.append(value)
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.append(value)

    def insert(self, index, value):
        value = self.parser.parse_item(value)
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.insert(index, value)
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.insert(index, value)

    def extend(self, other):
        if isinstance(other, JSONArray) and self.item_type == other.item_type:
            items = other.__items__
        else:
            items = list(self.parser.parse_list(other))
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.extend(items)
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.extend(items)

    def pop(self, index=-1):
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.pop(index)
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.pop(index)

    def remove(self, value):
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.remove(value)
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.remove(value)

    def reverse(self):
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.reverse()
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.reverse()

    def sort(self, key=None, reverse=False):
        if self.list_validator is not None:
            clone = self.__items__[:]
            clone.sort(key=key, reverse=reverse)
            self.__items__ = self.list_validator.validate(clone)
        else:
            self.__items__.sort(key=key, reverse=reverse)


class BooleanArray(JSONArray):
    item_type = bool


class IntegerArray(JSONArray):
    item_type = int, long


class NumberArray(JSONArray):
    item_type = int, long, float


class StringArray(JSONArray):
    item_type = str, unicode


class ArrayOf(object):
    def __new__(cls, item_type, name='GenericArray', item_validator=None, list_validator=None):
        return JSONArrayType(name, (JSONArray,), dict(item_type=item_type, item_validator=item_validator, list_validator=list_validator))


JSONList = JSONArray
BooleanList = BooleanArray
IntegerList = IntegerArray
NumberList = NumberArray
StringList = StringArray

ListOf = ArrayOf
