"""
Snap's ``TSAF`` container — the format behind ``Documents/user.plist`` and
``Documents/ClientEncryptionService.plist``, which are not property lists despite their names
(``plistlib`` raises "Invalid file" on both).

What is known of the layout, from the files themselves: a ``TSAF`` magic and a short header,
then length-free tokens, each a ``0x08`` byte, a UTF-8 string and a ``0x00`` terminator. A
field is a key token immediately followed by a value token; a ``,`` byte before a token marks a
nested object's type name (``User``, ``SCClientEncryption``); other single bytes between tokens
are values of types this module does not decode, so the key before one reads as having **no**
value rather than the next string. Nothing beyond that is claimed.

The keyed read — ``username``, ``user_id``, ``laguna_id`` and the ``client_encryption`` triple —
follows iLEAPP's ``snapchatAccount`` artifact (Alexis Brignoni, ``scripts/artifacts/snapchat.py``,
MIT), which established that ``user_id`` read this way matches the account's ``snapchatter``
row. ``user_id`` and ``laguna_id`` are only accepted when UUID-shaped, as there.
"""

import re

MAGIC = b"TSAF"

# A string token is one tag byte, the text, a NUL. 0x08 tags a string field; 0x1e tags the root
# object's type name (the first token of a cache entry such as sccache.*), which is a type like the
# ","-prefixed nested ones and takes no value.
_TOKEN_RE = re.compile(rb"([\x08\x1e])([^\x00]*)\x00")
_TAG_ROOT_TYPE = 0x1E
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: The keys ``account()`` reports, in the order they are shown. The three ``client_encryption``
#: fields sit under that nested object in the file; they are flattened here under the same names
#: iLEAPP uses so the two tools' reports can be compared field for field.
ACCOUNT_KEYS = ("username", "user_id", "laguna_id", "identifier", "encryption_key",
                "initialization_vector")
_UUID_KEYS = ("user_id", "laguna_id")


def is_tsaf(raw):
    return bool(raw) and raw[:4] == MAGIC


class Token(tuple):
    """``(text, is_value, is_type)``: ``is_value`` when the token begins right after the previous
    token's terminator (so it is that token's value); ``is_type`` when a ``,`` byte precedes it
    (a nested object's type name, which takes no value and is nobody's value)."""
    __slots__ = ()
    text = property(lambda self: self[0])
    is_value = property(lambda self: self[1])
    is_type = property(lambda self: self[2])


def tokens(raw):
    """The container's string tokens in file order (see :class:`Token`); ``[]`` when not TSAF.

    "Immediately follows" is the rule for a value, not "the next string": a key whose value is
    absent is stored as the key followed by a lone ``0x00`` (seen in a signed-out account's
    ``user.plist``), and reading the next string there would report the *following key's name*
    as the value.
    """
    if not is_tsaf(raw):
        return []
    out, prev_end = [], None
    for match in _TOKEN_RE.finditer(raw):
        text = match.group(2).decode("utf-8", "replace")
        is_type = (match.group(1)[0] == _TAG_ROOT_TYPE
                   or (match.start() > 0 and raw[match.start() - 1] == 0x2C))
        out.append(Token((text, prev_end is not None and match.start() == prev_end, is_type)))
        prev_end = match.end()
    return out


def _value_at(toks, i):
    """The value token of ``toks[i]``, or None."""
    if toks[i].is_type or i + 1 >= len(toks):
        return None
    nxt = toks[i + 1]
    return nxt if nxt.is_value and not nxt.is_type else None


def value(toks, key):
    """The value of ``key``, or ``""`` when the key is absent or carries no string value."""
    for i, tok in enumerate(toks):
        if tok.text == key and not tok.is_type:
            val = _value_at(toks, i)
            if val is not None:
                return val.text
    return ""


def read(path):
    """The file's bytes, or ``b""`` when it cannot be read — a missing file is a normal outcome."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def account(raw):
    """``{key: value}`` for :data:`ACCOUNT_KEYS`, absent keys omitted; ``{}`` when not TSAF.

    ``user_id`` and ``laguna_id`` are dropped unless UUID-shaped, so a stray string can never be
    reported as an account identifier.
    """
    toks = tokens(raw)
    if not toks:
        return {}
    out = {}
    for key in ACCOUNT_KEYS:
        val = value(toks, key)
        if key in _UUID_KEYS and val and not _UUID_RE.match(val):
            continue
        if val:
            out[key] = val
    return out


def fields(raw, limit=40):
    """Every ``(key, value)`` pair in order — the generic view a cache report shows of any TSAF
    file. Tokens that have no value (type names, keys of undecoded types) are listed with ``""``."""
    toks = tokens(raw)
    out, i = [], 0
    while i < len(toks) and len(out) < limit:
        val = _value_at(toks, i)
        if val is not None:
            out.append((toks[i].text, val.text))
            i += 2
        else:
            out.append((toks[i].text, ""))
            i += 1
    return out
