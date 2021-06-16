
import os
import sys

from application.python.descriptor import classproperty


class Resources(object):
    """Provide access to SylkServer's resources"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            binary_directory = os.path.dirname(os.path.realpath(sys.argv[0]))
            if os.path.basename(binary_directory) == 'bin':
                application_directory = os.path.dirname(binary_directory)
                resources_component = 'share/sylkserver'
            else:
                application_directory = binary_directory
                resources_component = 'resources'
            cls._cached_directory = os.path.join(application_directory, resources_component)
        return cls._cached_directory

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource or '')


class VarResources(object):
    """Provide access to SylkServer's resources that should go in /var"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            binary_directory = os.path.dirname(os.path.realpath(sys.argv[0]))
            if os.path.basename(binary_directory) == 'bin':
                path = '/var'
            else:
                path = 'var'
            cls._cached_directory = os.path.abspath(path)
        return cls._cached_directory

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource or '')

