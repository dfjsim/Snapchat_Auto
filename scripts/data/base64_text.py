"""Whether a text is base64, and the bytes it stands for.

An id the app keeps as bytes in one store is often written as base64 in another — in a cache claim's
key, in a FlatBuffers string — and not always in the same alphabet or with the same padding. One rule
decides when a text is read as base64 at all, so ``--trace-ids``, the claim-link survey and the
creative-tools item matcher (:mod:`scripts.data.ctp_items`) agree on which texts are ids.
"""
import base64
import binascii
import re

_HEXISH = re.compile(r"^[0-9a-fA-F-]+$")
_B64_STD = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_B64_URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def base64_bytes(text):
    """The bytes ``text`` encodes, when it reads as base64; else None.

    It reads as base64 when it is padded with ``=``, uses ``+`` or ``/``, or mixes upper case, lower
    case and digits — and decodes, in one alphabet, to bytes that encode back to exactly ``text``.
    Hex is not read as base64 (a UUID, a CACHE_KEY or a number is made of base64 characters too, and
    means something else), and neither is a plain word or username.
    """
    if _HEXISH.match(text):
        return None
    std, url = _B64_STD.match(text), _B64_URL.match(text)
    if not (std or url):
        return None
    looks = any(ch in text for ch in "=+/") or (
        any(ch.isupper() for ch in text) and any(ch.islower() for ch in text)
        and any(ch.isdigit() for ch in text))
    body = text.rstrip("=")
    if not looks or len(body) % 4 == 1 or (body != text and len(text) % 4):
        return None
    decode, encode = ((base64.b64decode, base64.b64encode) if std
                      else (base64.urlsafe_b64decode, base64.urlsafe_b64encode))
    try:
        raw = decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw if encode(raw).decode("ascii").rstrip("=") == body else None
