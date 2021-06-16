
from application import log
from application.process import process
from gnutls.crypto import X509Certificate, X509PrivateKey


__all__ = 'Certificate', 'PrivateKey'


def file_content(filename):
    path = process.configuration.file(filename)
    if path is None:
        raise Exception("File '%s' does not exist" % filename)
    try:
        f = open(path, 'rt')
    except Exception:
        raise Exception("File '%s' could not be open" % filename)
    try:
        return f.read()
    finally:
        f.close()


class Certificate(object):
    """Configuration data type. Used to create a gnutls.crypto.X509Certificate object
       from a file given in the configuration file."""
    def __new__(cls, value):
        if isinstance(value, str):
            try:
                return X509Certificate(file_content(value))
            except Exception as e:
                log.warn("Certificate file '%s' could not be loaded: %s" % (value, str(e)))
                return None
        else:
            raise TypeError('value should be a string')


class PrivateKey(object):
    """Configuration data type. Used to create a gnutls.crypto.X509PrivateKey object
       from a file given in the configuration file."""
    def __new__(cls, value):
        if isinstance(value, str):
            try:
                return X509PrivateKey(file_content(value))
            except Exception as e:
                log.warn("Private key file '%s' could not be loaded: %s" % (value, str(e)))
                return None
        else:
            raise TypeError('value should be a string')
