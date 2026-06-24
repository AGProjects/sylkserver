
import os

from datetime import datetime

from OpenSSL import SSL, crypto

from sylk.applications.xmppgateway.logger import log


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
        if chain_path is not None:
            context.use_certificate_chain_file(chain_path)
        context.check_privatekey()
        return context

    @staticmethod
    def _certificate_names(cert_path):
        with open(cert_path, 'rb') as f:
            certificate = crypto.load_certificate(crypto.FILETYPE_PEM, f.read())
        common_name = certificate.get_subject().commonName
        dns_names = []
        for index in range(certificate.get_extension_count()):
            extension = certificate.get_extension(index)
            if extension.get_short_name() == b'subjectAltName':
                for item in str(extension).split(','):
                    item = item.strip()
                    if item.lower().startswith('dns:'):
                        dns_names.append(item[4:])
                break
        if not dns_names and common_name:
            dns_names = [common_name]
        not_after = certificate.get_notAfter()
        if not_after is not None:
            try:
                expires = datetime.strptime(not_after.decode('ascii'), '%Y%m%d%H%M%SZ').strftime('%Y-%m-%d')
            except (ValueError, AttributeError):
                expires = not_after.decode('ascii', 'replace')
        else:
            expires = '(unknown)'
        if certificate.has_expired():
            expires += ' (EXPIRED)'
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

    def getContext(self):
        return self._default_context
