#!/usr/bin/python3

from distutils.core import setup

import glob
import os

import sylk


def find_packages(root):
    return [directory.replace(os.path.sep, '.') for directory, sub_dirs, files in os.walk(root) if '__init__.py' in files]


def list_resources(source_directory, destination_directory):
    return [(directory.replace(source_directory, destination_directory), [os.path.join(directory, f) for f in files]) for directory, sub_dirs, files in os.walk(source_directory)]


setup(
    name='sylkserver',
    version=sylk.__version__,

    description='SylkServer - An Extensible RTC Application Server',
    url='http://sylkserver.com/',

    author='AG Projects',
    author_email='support@ag-projects.com',

    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Service Providers',
        'License :: GNU General Public License 3',
        'Operating System :: OS Independent',
        'Programming Language :: Python'
    ],

    requires=[],

    packages=find_packages('sylk'),
    scripts=['sylk-server', 'sylk-db-maintenance'],
    data_files=[('/etc/sylkserver', glob.glob('*.ini.sample')),
                ('/etc/sylkserver/tls', glob.glob('resources/tls/*.crt'))] + list_resources('resources', destination_directory='share/sylkserver')
)
