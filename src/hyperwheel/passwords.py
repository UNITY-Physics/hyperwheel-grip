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
