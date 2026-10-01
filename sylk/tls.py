
import re

from datetime import datetime, timezone

from application import log
from application.process import process
from gnutls.crypto import X509Certificate, X509PrivateKey
from OpenSSL import crypto


__all__ = 'Certificate', 'PrivateKey', 'log_certificate_file', 'log_certificate_expiry', 'CERTIFICATE_EXPIRY_WARNING_DAYS'


# Certificates expiring within this many days are logged with a warning at start
CERTIFICATE_EXPIRY_WARNING_DAYS = 30

_pem_certificate_re = re.compile(rb'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----', re.DOTALL)


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


def _x509_name(name):
    """Short description of an OpenSSL.crypto.X509Name: the CN, or the full DN when there is no CN"""
    if name.commonName:
        return name.commonName
    return '/'.join('%s=%s' % (key.decode('ascii', 'replace'), value.decode('utf-8', 'replace')) for key, value in name.get_components()) or '(empty)'


def log_certificate_expiry(label, not_after, logger=log, warning_days=CERTIFICATE_EXPIRY_WARNING_DAYS):
    """
    Log an error for an expired certificate or a warning for one expiring within
    warning_days. not_after is an aware datetime. Returns a short expiry string
    suitable for inclusion in an info line, e.g. '2026-12-30 14:00 UTC (90 days left)'.
    """
    remaining = not_after - datetime.now(timezone.utc)
    date = not_after.strftime('%Y-%m-%d %H:%M UTC')
    if remaining.total_seconds() <= 0:
        logger.error('TLS certificate %s EXPIRED on %s' % (label, date))
        return '%s (EXPIRED)' % date
    days = remaining.days
    if days < warning_days:
        logger.warning('TLS certificate %s expires in %d day%s, on %s' % (label, days, '' if days == 1 else 's', date))
    return '%s (%d day%s left)' % (date, days, '' if days == 1 else 's')


def _not_after(certificate):
    return datetime.strptime(certificate.get_notAfter().decode('ascii'), '%Y%m%d%H%M%SZ').replace(tzinfo=timezone.utc)


def log_certificate_file(label, path, logger=log, summarize=False, warning_days=CERTIFICATE_EXPIRY_WARNING_DAYS):
    """
    Log subject and expiry of every PEM certificate found in path (a certificate,
    a key + fullchain file or a chain file) and warn about expired or soon to
    expire ones. With summarize=True (for CA lists) only a summary line is logged,
    plus the warnings.
    """
    try:
        with open(path, 'rb') as f:
            data = f.read()
        certificates = [crypto.load_certificate(crypto.FILETYPE_PEM, match.group(0)) for match in _pem_certificate_re.finditer(data)]
    except Exception as e:
        logger.warning('Could not read %s certificates from %s: %s' % (label, path, e))
        return
    if not certificates:
        logger.warning('No %s certificates found in %s' % (label, path))
        return
    if summarize:
        earliest = None
        for certificate in certificates:
            not_after = _not_after(certificate)
            if (not_after - datetime.now(timezone.utc)).days < warning_days:
                log_certificate_expiry('%s %s' % (label, _x509_name(certificate.get_subject())), not_after, logger, warning_days)
            if earliest is None or not_after < earliest[0]:
                earliest = not_after, certificate
        logger.info('%s: %d certificate%s loaded from %s, earliest expiry %s (%s)' %
                    (label, len(certificates), '' if len(certificates) == 1 else 's', path,
                     earliest[0].strftime('%Y-%m-%d'), _x509_name(earliest[1].get_subject())))
    else:
        for index, certificate in enumerate(certificates):
            name = _x509_name(certificate.get_subject())
            what = '%s %s' % (label, 'certificate' if index == 0 else 'chain[%d]' % index)
            expires = log_certificate_expiry('%s (%s)' % (what, name), _not_after(certificate), logger, warning_days)
            logger.info('%s from %s: %s, expires %s' % (what, path, name, expires))
