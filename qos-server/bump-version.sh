#!/bin/bash
#
# Increment sylk-qos-server's VERSION (semantic X.Y.Z -> X.Y.Z+1) and print it.
#
# Run this on every build/deploy so the version the daemon reports uniquely
# identifies the running build. The daemon prints VERSION at startup (in the
# "effective configuration" block), in the HTTP Server: header, and at
# GET /health — so you can always tell whether the box is running the latest
# code by comparing those against this number.
#
# Usage:
#   ./bump-version.sh            # X.Y.Z -> X.Y.(Z+1)
#   ./bump-version.sh minor      # X.Y.Z -> X.(Y+1).0
#   ./bump-version.sh major      # X.Y.Z -> (X+1).0.0
#
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
FILE="$DIR/sylk-qos-server.py"
PART="${1:-patch}"

new=$(python3 - "$FILE" "$PART" <<'PY'
import re, sys
path, part = sys.argv[1], sys.argv[2]
src = open(path).read()
m = re.search(r"^VERSION = '(\d+)\.(\d+)\.(\d+)'", src, re.M)
if not m:
    sys.exit("VERSION = 'X.Y.Z' line not found in %s" % path)
major, minor, patch = map(int, m.groups())
if part == 'major':
    major, minor, patch = major + 1, 0, 0
elif part == 'minor':
    minor, patch = minor + 1, 0
elif part == 'patch':
    patch += 1
else:
    sys.exit("unknown part %r (use major|minor|patch)" % part)
new = "%d.%d.%d" % (major, minor, patch)
src = src[:m.start()] + "VERSION = '%s'" % new + src[m.end():]
open(path, 'w').write(src)
print(new)
PY
)
echo "sylk-qos-server version -> $new"
