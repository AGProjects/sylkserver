
import os
import re

from datetime import datetime, timezone

from cryptography import x509
from OpenSSL import SSL, crypto

from sylk.applications.xmppgateway.logger import log
from sylk.tls import log_certificate_expiry


_pem_certificate_re = re.compile(rb'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----', re.DOTALL)


def ssl_error_reason(error):
    """A one line description of an OpenSSL.SSL.Error"""
    try:
        reasons = [item[2] for item in error.args[0] if item[2]]
    except (IndexError, TypeError):
        reasons = []
    return ', '.join(reasons) or str(error) or error.__class__.__name__


class _StaticContextFactory(object):
    """
    Minimal IOpenSSLContextFactory wrapper returning a fixed, pre-built context.
    Used to present a specific server certificate when initiating an outgoing
    S2S connection, so the peer can validate our identity.
    """

    def __init__(self, context):
        self._context = context

    def getContext(self):
        return self._context


class SNIContextFactory(object):
    """
    OpenSSL context factory that selects the server certificate based on the
    TLS SNI servername sent by the peer during the S2S handshake.

    Certificates are discovered from a directory and each one is indexed by the
    DNS names found in its certificate (Subject Alternative Name, or the Common
    Name when no SAN is present), so selection always reflects what the
    certificate actually covers, regardless of how the files are named. Two
    directory layouts are supported:

      - Let's Encrypt native layout: each immediate subdirectory that contains
        both 'privkey.pem' and 'fullchain.pem' is loaded as one certificate
        (e.g. /etc/letsencrypt/live/<lineage>/). This lets the gateway read
        certbot's storage directly, with no manual file assembly.

      - Flat layout: a single '<name>.pem' file containing the unencrypted
        private key followed by the certificate and any intermediate CA
        certificates (key + fullchain concatenated).

    Selection for an incoming connection:
      - exact match on the SNI name (e.g. 'sylk.link')
      - otherwise a wildcard match, so 'conference.sylk.link' matches a
        certificate carrying '*.sylk.link'
      - peers that send no SNI, or an unmatched name, get the default context
        built from the 'certificate'/'ca_file' settings.

    Note: when pointing at /etc/letsencrypt/live, the SylkServer process must
    have read access to the private keys there (they are root-only by default).
    """

    def __init__(self, default_cert_path, certificates_directory=None, chain_path=None):
        self._contexts = {}     # exact DNS name -> context
        self._wildcards = {}    # base domain of a '*.<base>' name -> context
        self._fingerprints = {}  # certificate SHA-256 fingerprint -> label it was first loaded under
        self._default_context = self._load_context(default_cert_path, chain_path=chain_path)
        # The callback only needs to live on the context the listening socket
        # starts with; it may swap the connection to a per-domain context.
        self._default_context.set_tlsext_servername_callback(self._select_context)
        self._add_certificate('default', default_cert_path, context=self._default_context)
        if certificates_directory:
            self._load_directory(certificates_directory)

    def _load_directory(self, directory):
        if not os.path.isdir(directory):
            log.error('XMPP S2S certificates directory %s could not be found' % directory)
            return
        log.info('XMPP S2S loading certificates from directory %s' % directory)
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            cert_path = key_path = None
            if os.path.isdir(path):
                # Let's Encrypt live/<lineage>/ layout
                fullchain = os.path.join(path, 'fullchain.pem')
                privkey = os.path.join(path, 'privkey.pem')
                if os.path.isfile(fullchain) and os.path.isfile(privkey):
                    cert_path, key_path = fullchain, privkey
            elif entry.endswith('.pem'):
                # single file holding key + fullchain
                cert_path = key_path = path
            if cert_path is None:
                continue
            self._add_certificate(entry, cert_path, key_path=key_path)

    @staticmethod
    def _load_context(cert_path, key_path=None, chain_path=None):
        context = SSL.Context(SSL.SSLv23_METHOD)
        # cert_path holds the certificate (plus chain, in the fullchain case);
        # key_path holds the private key (the same file in the flat layout).
        context.use_certificate_chain_file(cert_path)
        context.use_privatekey_file(key_path or cert_path)
        context.check_privatekey()
        if chain_path is not None:
            # chain_path is the ca_file setting, which is normally a CA list
            # (/etc/sylkserver/tls/ca.crt holds well over 100 roots), not a
            # chain for our certificate. It must not be loaded with
            # use_certificate_chain_file(): that replaces the server
            # certificate with the first certificate in the file, OpenSSL then
            # drops the no longer matching private key and the context is
            # unusable ("no private key assigned"). Use it as the trust store,
            # and send along only the intermediates it holds for our own
            # certificate that cert_path itself does not already provide.
            context.load_verify_locations(chain_path)
            SNIContextFactory._add_issuer_chain(context, cert_path, chain_path)
        return context

    @staticmethod
    def _load_certificates(path):
        with open(path, 'rb') as f:
            data = f.read()
        certificates = []
        for block in _pem_certificate_re.findall(data):
            try:
                certificates.append(crypto.load_certificate(crypto.FILETYPE_PEM, block))
            except crypto.Error:
                log.warning('Skipping unreadable certificate in %s' % path)
        return certificates

    @staticmethod
    def _add_issuer_chain(context, cert_path, chain_path):
        own_chain = SNIContextFactory._load_certificates(cert_path)
        if not own_chain:
            return
        own_subjects = {certificate.get_subject().der(): certificate for certificate in own_chain}
        candidates = {}
        for certificate in SNIContextFactory._load_certificates(chain_path):
            candidates.setdefault(certificate.get_subject().der(), certificate)
        current = own_chain[0]
        for _ in range(10):  # guard against loops in broken bundles
            issuer = current.get_issuer()
            if issuer.der() == current.get_subject().der():
                break  # self-signed: end of the chain
            if issuer.der() in own_subjects:
                current = own_subjects[issuer.der()]  # already sent from cert_path
                continue
            certificate = candidates.get(issuer.der())
            if certificate is None or certificate.get_issuer().der() == certificate.get_subject().der():
                break  # issuer unknown, or a root, which peers must already have
            if certificate.has_expired():
                log.warning('XMPP S2S intermediate certificate %s from %s has expired' % (certificate.get_subject().commonName, chain_path))
            context.add_extra_chain_cert(certificate)
            log.info('XMPP S2S added intermediate certificate %s from %s' % (certificate.get_subject().commonName, chain_path))
            current = certificate

    @staticmethod
    def _certificate_names(cert_path):
        with open(cert_path, 'rb') as f:
            certificate = crypto.load_certificate(crypto.FILETYPE_PEM, f.read())
        common_name = certificate.get_subject().commonName
        # pyOpenSSL removed the X509 extension API (get_extension and friends),
        # read the Subject Alternative Name through cryptography instead.
        try:
            san = certificate.to_cryptography().extensions.get_extension_for_class(x509.SubjectAlternativeName)
        except x509.ExtensionNotFound:
            dns_names = []
        else:
            dns_names = san.value.get_values_for_type(x509.DNSName)
        if not dns_names and common_name:
            dns_names = [common_name]
        try:
            not_after = datetime.strptime(certificate.get_notAfter().decode('ascii'), '%Y%m%d%H%M%SZ').replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            expires = '(unknown)'
        else:
            expires = log_certificate_expiry('XMPP S2S %s (%s)' % (cert_path, common_name), not_after, logger=log)
        fingerprint = certificate.digest('sha256').decode('ascii')
        return common_name, dns_names, expires, fingerprint

    def _add_certificate(self, label, cert_path, key_path=None, context=None):
        try:
            common_name, dns_names, expires, fingerprint = self._certificate_names(cert_path)
        except Exception:
            log.exception('Reading XMPP S2S certificate %s' % cert_path)
            return
        # An identical certificate already loaded (commonly the default cert also
        # appearing under the certificates directory) is skipped to avoid noise.
        existing = self._fingerprints.get(fingerprint)
        if existing is not None and context is None:
            log.info('XMPP S2S certificate [%s] from %s is identical to [%s], skipping' % (label, cert_path, existing))
            return
        if context is None:
            try:
                context = self._load_context(cert_path, key_path=key_path)
            except (SSL.Error, OSError) as e:
                reason = ssl_error_reason(e) if isinstance(e, SSL.Error) else (e.strerror or str(e))
                log.error('Skipping XMPP S2S certificate [%s] from %s: %s' % (label, cert_path, reason))
                return
            except Exception:
                log.exception('Loading XMPP S2S certificate from %s' % cert_path)
                return
        self._fingerprints.setdefault(fingerprint, label)
        for name in dns_names:
            name = name.lower()
            if name.startswith('*.'):
                self._wildcards[name[2:]] = context
            else:
                self._contexts[name] = context
        log.info('XMPP S2S certificate [%s] loaded from %s: CN=%s, names=%s, expires=%s' %
                 (label, cert_path, common_name, ', '.join(dns_names) or '(none)', expires))

    def _select_context(self, connection):
        name = connection.get_servername()
        if not name:
            return
        try:
            name = name.decode('ascii').lower()
        except (AttributeError, UnicodeDecodeError):
            return
        context = self._contexts.get(name)
        if context is None and '.' in name:
            # match a '*.<base>' certificate for a single-label subdomain
            context = self._wildcards.get(name.split('.', 1)[1])
        if context is not None:
            connection.set_context(context)

    def _context_for_name(self, name):
        name = (name or '').lower()
        context = self._contexts.get(name)
        if context is None and '.' in name:
            context = self._wildcards.get(name.split('.', 1)[1])
        return context or self._default_context

    def client_context_factory(self, name):
        """
        Return an IOpenSSLContextFactory presenting the certificate that covers
        `name`, for use when initiating an outgoing S2S connection as `name`
        (so the remote server can validate our certificate). Falls back to the
        default certificate when no specific match exists.
        """
        return _StaticContextFactory(self._context_for_name(name))

    def getContext(self):
        return self._default_context
