"""Small helpers shared across modules."""

import logging
import math
import re
import unicodedata

log = logging.getLogger("audiomagic")


def db_to_lin(db):
    if db is None or db <= -120:
        return 0.0
    return 10.0 ** (db / 20.0)


def lin_to_db(x, floor=-120.0):
    if x <= 0:
        return floor
    return max(floor, 20.0 * math.log10(x))


def slugify(text, fallback="untitled"):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:48] or fallback


def safe_dirname(name):
    """A folder name that keeps the user's spelling but is safe on disk."""
    name = str(name).strip()
    name = re.sub(r"[/\\\x00-\x1f]", "-", name)
    name = name.strip(". ")
    return name[:80] or "Untitled"


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v
