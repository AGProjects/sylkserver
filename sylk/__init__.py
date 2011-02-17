# Copyright (C) 2010-2011 AG Projects. See LICENSE for details

"""SylkServer"""

__version__ = '1.0.1'

configuration_filename = "config.ini"


package_requirements = {'python-application': '1.2.6',
                        'python-sipsimple': '0.17.1'}

try:
    from application.dependency import ApplicationDependencies
except:
    class DependencyError(Exception): pass
    class ApplicationDependencies(object):
        def __init__(self, *args, **kwargs):
            pass
        def check(self):
            raise DependencyError("need python-application version %s or higher but it's not installed" % package_requirements['python-application'])

dependencies = ApplicationDependencies(**package_requirements)


