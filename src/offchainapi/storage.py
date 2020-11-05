# Copyright (c) The Libra Core Contributors
# SPDX-License-Identifier: Apache-2.0

# The main storage interface.
from hashlib import sha256
import json

from .utils import JSONFlag, JSONSerializable, get_unique_string
from .database import Database


def key_join(strs):
    ''' Joins a sequence of strings to form a storage key. '''
    # Ensure this is parseable and one-to-one to avoid collisions.
    return '||'.join([f'[{len(s)}:{s}]' for s in strs])


class Storable:
    """Base class for objects that can be stored.

    Args:
        xtype (*): the type (or base type) of the objects to be stored.
    """

    def __init__(self, xtype):
        self.xtype = xtype
        self.factory = None

    def pre_proc(self, val):
        """ Pre-processing of objects before storage. By default
            it calls get_json_data_dict for JSONSerializable objects or
            their base type. eg int('10'). The result must be a structure
            that can be passed to json.dumps.
        """
        if issubclass(self.xtype, JSONSerializable):
            return val.get_json_data_dict(JSONFlag.STORE)
        else:
            return self.xtype(val)

    def post_proc(self, val):
        """ Post-processing to convert a json parsed structure into a Python
            object. It uses parse on JSONSerializable objects, and otherwise
            the type constructor.
        """
        if issubclass(self.xtype, JSONSerializable):
            return self.xtype.parse(val, JSONFlag.STORE)
        else:
            return self.xtype(val)


class StorableFactory:
    ''' This class maintains an overview of the full storage subsystem,
    and creates specific classes for values, lists and dictionary like
    types that can be stored persistently.

    Initialize the ``StorableFactory`` with a persistent key-value
    store ``db``, which is an implementation of ``Database``.
    '''

    def __init__(self, db):
        assert isinstance(db, Database)
        self.db = db


    def make_dir(self, name, root=None):
        ''' Makes a new value-like storable.

            Parameters:
                * name : a string representing the name of the object.
                * root : another storable object that acts as a logical
                  folder to this one.

        '''
        v = StorableValue(self.db, name, root)
        v.factory = self
        return v

    def make_dict(self, name, xtype, root):
        ''' A new map-like storable object.
            Parameters:
                * name : a string representing the name of the object.
                * xtype : the type of the object stored in the map.
                  It may be a simple type or a subclass of
                  JSONSerializable. The keys are always strings.
                * root : another storable object that acts as a logical
                  folder to this one.

        '''
        v = StorableDict(self.db, name, xtype, root)
        v.factory = self
        return v


class StorableDict(Storable):
    """ Implements a persistent dictionary like type. Entries are stored
        by key directly, and a separate doubly linked list structure is
        stored to enable traversal of keys and values.

        Supports:
            * __getitem__(self, key)
            * __setitem__(self, key, value)
            * keys(self)
            * values(self)
            * __len__(self)
            * __contains__(self, item)
            * __delitem__(self, key)

        Keys should be strings or any object with a unique str representation.
        """

    def __init__(self, db, name, xtype, root=None):

        if root is None:
            self.root = ['']
        else:
            self.root = root.base_key()
        self.name = name
        self.db = db
        self.xtype = xtype

        self.prefix = key_join(self.base_key())

    def base_key(self):
        return self.root + [self.name]

    def try_get(self, key):
        """
        Returns value if key exists in storage, otherwise returns None
        """
        val = self.db.try_get(self.prefix, key)
        if val is None:
            return None
        return self.post_proc(json.loads(val))

    def __getitem__(self, key):
        return self.post_proc(json.loads(self.db.get(self.prefix, key)))

    def __setitem__(self, key, value):
        data = json.dumps(self.pre_proc(value))
        self.db.put(self.prefix, key, data)

    def keys(self):
        ''' An iterator over the keys of the dictionary. '''
        return self.db.getkeys(self.prefix)

    def __len__(self):
        return self.db.count(self.prefix)

    def is_empty(self):
        ''' Returns True if dict is empty and False if it contains some elements.'''
        return self.db.count(self.prefix) == 0

    def __delitem__(self, key):
        self.db.delete(self.prefix, key)

    def __contains__(self, key):
        return self.db.isin(self.prefix, key)


class StorableValue:
    """ Implements a cached persistent value. The value is stored to storage
        but a cached variant is stored for quick reads.
    """

    def __init__(self, db, name, root=None):
        if root is None:
            self.root = ['']
        else:
            self.root = root.base_key()

        self.name = name
        self.db = db
        self.prefix = key_join(self.base_key())

    def base_key(self):
        return self.root + [ self.name ]
