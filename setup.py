#!/usr/bin/python

# Copyright (C) 2010-2011 AG Projects. See LICENSE for details
#

import glob
import os

from distutils.core import setup

from sylk import __version__


def find_packages(toplevel):
    return [directory.replace(os.path.sep, '.') for directory, subdirs, files in os.walk(toplevel) if '__init__.py' in files]

setup(name         = "sylkserver",
      version      = __version__,
      author       = "AG Projects",
      author_email = "support@ag-projects.com",
      url          = "http://sylkserver.com",
      description  = "SylkServer - A state of the art, extensible SIP Application Server",
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

