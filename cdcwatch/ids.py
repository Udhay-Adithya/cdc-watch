"""Identity extraction: Neo IDs and registration numbers.

The Neo ID format is rigid -- validated against a real 498-row CDC list,
every entry matched letter-digit alternation, exactly 8 characters.  That
lets us pull IDs out of freeform text without relying on column headers or
table structure, which is what makes the whole pipeline robust.
"""
import re
import unicodedata

# B5R7O9J8 -> (letter,digit) x4.  \b stops us matching inside a longer token.
NEO_ID_RE = re.compile(r"\b(?:[A-Z][0-9]){4}\b")
# 23BCE7625
REG_NO_RE = re.compile(r"\b[0-9]{2}[A-Z]{3}[0-9]{4}\b")
# Company-issued reference numbers, e.g. TCS CT20264998331 / DT20268185049.
# Some drives publish results against these and never mention a Neo ID.
REF_ID_RE = re.compile(r"\b[A-Z]{2}[0-9]{9,12}\b")
# What a user may register via /addid. Deliberately loose -- these are matched
# only as exact whole-word tokens the user supplied, never pattern-scanned, so
# a broad shape costs nothing.
EXTRA_ID_RE = re.compile(r"[A-Z0-9][A-Z0-9\-/]{4,}")

# Zero-width and exotic spaces survive copy-paste out of Excel and Word and
# will silently break an exact-match compare.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def normalize(text):
    """Fold text to a form safe for exact-token matching."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_INVISIBLE)
    # NFKC leaves NBSP alone; collapse every space variant to a plain space.
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.upper()


def find_neo_ids(text):
    """All Neo IDs in `text`, order preserved, deduplicated."""
    return _dedupe(NEO_ID_RE.findall(normalize(text)))


def find_reg_nos(text):
    return _dedupe(REG_NO_RE.findall(normalize(text)))


def find_ref_ids(text):
    return _dedupe(REF_ID_RE.findall(normalize(text)))


def token_present(text, token):
    """Is `token` present in `text` as a whole word?"""
    token = normalize(token).strip()
    if not token:
        return False
    return re.search(r"\b{}\b".format(re.escape(token)), normalize(text)) is not None


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
