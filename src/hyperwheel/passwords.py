"""Readable generated passwords: three made-up five-letter chunks, such as ``borak-kefim-tumos``.

Each chunk is consonant, vowel, consonant, vowel, consonant, all lower case,
so it can be read out and typed without mistakes. Letters that are easy to
mix up (``l``, ``c``, ``q``, ``w``, ``x``, ``y``) aren't used. That gives
15 x 5 x 15 x 5 x 15 = 84,375 chunks, so three chunks are about 49 bits:
plenty for a login that is slowed down after failed tries. Passwords are
always generated, never chosen, so that holds for every one.
"""

from __future__ import annotations

import math
import re
import secrets

CONSONANTS = "bdfghjkmnprstvz"
VOWELS = "aeiou"
CHUNKS = 3
PATTERN = re.compile(rf"[{CONSONANTS}][{VOWELS}][{CONSONANTS}][{VOWELS}][{CONSONANTS}]"
                     rf"(-[{CONSONANTS}][{VOWELS}][{CONSONANTS}][{VOWELS}][{CONSONANTS}]){{{CHUNKS - 1}}}")


def chunk(choice=secrets.choice) -> str:
    c, v = CONSONANTS, VOWELS
    return choice(c) + choice(v) + choice(c) + choice(v) + choice(c)


def readable(choice=secrets.choice) -> str:
    return "-".join(chunk(choice) for _ in range(CHUNKS))


def bits() -> float:
    per_chunk = len(CONSONANTS) ** 3 * len(VOWELS) ** 2
    return CHUNKS * math.log2(per_chunk)


def same(typed: str, password: str) -> bool:
    """Whether what was typed back is the password, ignoring case, spaces around it and the dashes' spacing."""
    def norm(s: str) -> str:
        return re.sub(r"\s*-\s*", "-", s.strip().lower())
    return secrets.compare_digest(norm(typed).encode(), norm(password).encode())


# ---- SHA-512 crypt -----------------------------------------------------------------------------------
#
# The admin password reaches every Pi as a hash in /etc/shadow's format, made on the
# release signer's Mac, which has no yescrypt. SHA-512 crypt ("$6$", Ulrich Drepper's
# specification, which every Linux accepts) is plain hashlib, so it's written out here.
# The hash is published, so it uses many rounds: guessing one password costs
# ROUNDS / 5000 times what it costs at the default, on top of the salt. SHA-512 is
# quick on the Pi's 64-bit processor (1,000,000 rounds take about 0.6 s per admin
# login) and slower than SHA-256 on graphics cards, which guessers use.

ROUNDS = 1_000_000
_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ORDER = ((0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26), (6, 27, 48), (28, 49, 7),
          (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
          (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41))
HASH_PATTERN = re.compile(r"\$6\$rounds=(\d{4,9})\$([./0-9A-Za-z]{8,16})\$[./0-9A-Za-z]{86}")


def _b64(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = ""
    for _ in range(n):
        out += _B64[w & 0x3F]
        w >>= 6
    return out


def sha512_crypt(password: str, salt: str | None = None, rounds: int = ROUNDS) -> str:
    """``$6$rounds=<n>$<salt>$<hash>``, as crypt(3) makes it."""
    import hashlib
    if salt is None:
        salt = "".join(secrets.choice(_B64) for _ in range(16))
    salt = salt[:16]
    rounds = max(1000, min(999_999_999, rounds))
    key, s = password.encode(), salt.encode()
    b = hashlib.sha512(key + s + key).digest()
    a = hashlib.sha512(key + s)
    n = len(key)
    while n > 64:
        a.update(b)
        n -= 64
    a.update(b[:n])
    n = len(key)
    while n:
        a.update(b if n & 1 else key)
        n >>= 1
    c = a.digest()
    dp = hashlib.sha512(key * len(key)).digest()
    p = (dp * (len(key) // 64 + 1))[:len(key)]
    ds = hashlib.sha512(s * (16 + c[0])).digest()
    sp = (ds * (len(s) // 64 + 1))[:len(s)]
    for i in range(rounds):
        h = hashlib.sha512(p if i & 1 else c)
        if i % 3:
            h.update(sp)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()
    encoded = "".join(_b64(c[x], c[y], c[z], 4) for x, y, z in _ORDER) + _b64(0, 0, c[63], 2)
    return f"$6$rounds={rounds}${salt}${encoded}"


def matches(password: str, hashed: str) -> bool:
    """Whether a ``$6$rounds=`` hash is this password's."""
    m = HASH_PATTERN.fullmatch(hashed)
    if not m:
        return False
    return secrets.compare_digest(sha512_crypt(password, m.group(2), int(m.group(1))).encode(), hashed.encode())
