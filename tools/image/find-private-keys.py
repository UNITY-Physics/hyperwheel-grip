"""List files holding a private key: a PEM or OpenSSH key block with its contents, not just the words.

    python3 tools/image/find-private-keys.py <folder> ...

Prints each file found and exits 1 if there's any. Libraries that handle keys (cryptography, paramiko) mention
"PRIVATE KEY" in their code, so the words alone aren't enough.
"""

import os
import re
import sys

KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----\r?\n(?:[A-Za-z0-9-]+: [^\n]*\r?\n)*\r?\n?[A-Za-z0-9+/=]{40,}")

found = []
for top in sys.argv[1:]:
    for folder, _, names in os.walk(top):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 50_000_000:
                continue
            try:
                with open(path, "rb") as f:
                    if KEY.search(f.read()):
                        found.append(path)
            except OSError:
                continue
for path in found:
    print(path)
sys.exit(1 if found else 0)
