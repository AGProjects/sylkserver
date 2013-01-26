# Copyright (C) 2013 AG Projects. See LICENSE for details
#

__all__ = ['log']

from sylk.applications import ApplicationLogger

log = ApplicationLogger.for_package(__package__)

