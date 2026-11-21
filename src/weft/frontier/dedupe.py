"""Exact and near-duplicate detection on extracted text.

Exact: SHA-256 of the words, so whitespace and case changes still match. Near: a 64-bit simhash
over three-word shingles. Unrelated texts land about 32 bits apart. A one-word change moves a
short page (60 words) by about 6 bits and a long one (300 words) by about 3, so the threshold is
set for short pages. Below MIN_WORDS the simhash is not used at all.
"""

import hashlib
import re

NEAR_BITS = 6  # simhashes this close or closer are the same page
MIN_WORDS = 30  # below this a simhash is too noisy to trust; only the exact hash is used
_WORD = re.compile(r"\w+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(words(text)).encode()).hexdigest()


def simhash(text: str) -> int:
    """Unsigned 64-bit simhash. Each shingle votes on each bit; ties go to 0."""
    tokens = words(text)
    shingles = [" ".join(tokens[i : i + 3]) for i in range(max(1, len(tokens) - 2))]
    votes = [0] * 64
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            votes[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if votes[bit] > 0)


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_signed(h: int) -> int:
    """SQLite integers are signed 64-bit."""
    return h - (1 << 64) if h >= 1 << 63 else h


def from_signed(h: int) -> int:
    return h + (1 << 64) if h < 0 else h
