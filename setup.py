#!/usr/bin/python

# Copyright (C) 2010-2012 AG Projects. See LICENSE for details
#

import glob
import os
import re

from distutils.core import setup


def get_version():
    return re.search(r"""__version__\s+=\s+(?P<quote>['"])(?P<version>.+?)(?P=quote)""", open('sylk/__init__.py').read()).group('version')

def find_packages(toplevel):
    return [directory.replace(os.path.sep, '.') for directory, subdirs, files in os.walk(toplevel) if '__init__.py' in files]

setup(name         = "sylkserver",
      version      = get_version(),
      author       = "AG Projects",
      author_email = "support@ag-projects.com",
      url          = "http://sylkserver.com",
      description  = "SylkServer - An Extensible SIP Application Server",
      classifiers  = [
            "Development Status :: 5 - Production/Stable",
            "Intended Audience :: Service Providers",
            "License :: GNU General Public License 3",
            "Operating System :: OS Independent",
            "Programming Language :: Python"
                     ],
      packages     = find_packages('sylk'),
      scripts      = ['sylk-server'],
      data_files   = [('/etc/sylkserver/tls', []),
                      ('/var/lib/sylkserver', []),
                      ('share/sylkserver/sounds', glob.glob(os.path.join('resources', 'sounds', '*.wav'))),
                      ('share/sylkserver/sounds/moh', glob.glob(os.path.join('resources', 'sounds', 'moh','*.wav')))]
      )

