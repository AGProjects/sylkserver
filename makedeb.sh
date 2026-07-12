#!/bin/bash
if [ -d dist ]; then
    rm -r dist
fi

# remove stale MANIFEST, otherwise distutils sdist reuses it and
# newly added packages (e.g. sylk/payloads) are left out of the tarball
rm -f MANIFEST

python3 setup.py sdist

cd dist
tar zxvf *.tar.gz

cd sylkserver-?.?.?

debuild --no-sign

cd ..

ls
