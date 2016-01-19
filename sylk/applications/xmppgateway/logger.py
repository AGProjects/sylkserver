
__all__ = ['log']

from sylk.applications import ApplicationLogger

log = ApplicationLogger.for_package(__package__)

