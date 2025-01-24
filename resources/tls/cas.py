#!/usr/bin/env python3
from gnutls.crypto import X509Certificate
from gnutls.errors import GNUTLSError

def trusted_cas(content):
    trusted_cas = []
    crt = ''
    start = False
    end = False

    content = content or ''
    content = content.decode() if isinstance(content, bytes) else content

    for line in content.split("\n"):
        if "BEGIN CERT" in line:
            start = True
            crt = line + "\n"
        elif "END CERT" in line:
            crt = crt + line + "\n"
            end = True
            start = False

            try:
                trusted_cas.append(X509Certificate(crt))
            except (GNUTLSError, ValueError) as e:
                continue
        elif start:
            crt = crt + line + "\n"

    return trusted_cas


if __name__ == '__main__':
    path = "./ca.crt"
    content = open(path, 'r').read()
    cas = trusted_cas(content)

    i = 1
    for certificate in cas:
        print('%3d %s' % (i, certificate.subject))
        i = i + 1


