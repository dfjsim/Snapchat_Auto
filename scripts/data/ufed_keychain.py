"""Read a Cellebrite UFED keychain export (``backup_keychain_v2.plist``).

The export keeps every keychain item encrypted as iOS stores it — AES-256-GCM, the attributes under
a per-item metadata key wrapped by a class key, the secret under its own key — together with the
class keys and the per-item secret keys iOS would only release on the device. Reading it is three
GCM decryptions per item and a DER dictionary each for the attributes and the secret. The format is
described in ``docs/ufed_keychain_format.md``, which this module is written from.

``main(input_keychain, output_keychain)`` writes ``decrypted_keychain.plist`` — one dictionary per
item, the attributes plus the secret's ``v_Data`` — which is what the Memories reports search for
Snapchat's items. An item that cannot be read is skipped and counted, never raised.
"""
import datetime
import logging
import plistlib

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

CLASS_KEYS = "classKeyIdxToUnwrappedMetadataClassKey"
ENTRIES = "keychainEntries"


class FormatError(ValueError):
    """The bytes are not what the export format says they are."""


# --------------------------------------------------------------------------------------------- DER

def _tlv(buf, pos):
    """``(tag, value bytes, next position)`` of the DER element at ``pos``."""
    if pos + 2 > len(buf):
        raise FormatError("DER element runs off the end")
    tag, length = buf[pos], buf[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if not 0 < count <= 4 or pos + count > len(buf):
            raise FormatError("DER length")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    if pos + length > len(buf):
        raise FormatError("DER value runs off the end")
    return tag, buf[pos:pos + length], pos + length


def _elements(buf):
    out, pos = [], 0
    while pos < len(buf):
        tag, value, pos = _tlv(buf, pos)
        out.append((tag, value))
    return out


def _der_time(text, utc_time=False):
    fmt = "%y%m%d%H%M%S" if utc_time else "%Y%m%d%H%M%S"
    body = text.rstrip("Z")
    frac = ""
    if "." in body:
        body, frac = body.split(".", 1)
    stamp = datetime.datetime.strptime(body, fmt)
    if frac:
        stamp += datetime.timedelta(microseconds=int((frac + "000000")[:6]))
    return stamp


def der_value(tag, value):
    """One DER value as Python: text, bytes, int, bool, a datetime (UTC), a list or a dict."""
    if tag in (0x0C, 0x13, 0x16):
        return value.decode("utf-8", "replace")
    if tag == 0x04:
        return bytes(value)
    if tag == 0x02:
        return int.from_bytes(value, "big", signed=True)
    if tag == 0x01:
        return value != b"\x00"
    if tag == 0x05:
        return None
    if tag in (0x18, 0x17):
        try:
            return _der_time(value.decode("ascii"), utc_time=tag == 0x17)
        except (UnicodeDecodeError, ValueError):
            return bytes(value)
    if tag == 0x30:
        return [der_value(t, v) for t, v in _elements(value)]
    if tag == 0x31:
        return der_dict(value)
    return bytes(value)


def der_dict(content):
    """A DER dictionary's content (the SET's value): ``{key: value}``."""
    out = {}
    for tag, pair in _elements(content):
        if tag != 0x30:
            raise FormatError("a dictionary entry is not a SEQUENCE")
        parts = _elements(pair)
        if len(parts) != 2 or parts[0][0] not in (0x0C, 0x13, 0x16):
            raise FormatError("a dictionary entry is not a key and a value")
        out[parts[0][1].decode("utf-8", "replace")] = der_value(*parts[1])
    return out


def read_dictionary(plaintext):
    """The DER dictionary at the start of ``plaintext``; trailing padding (n bytes of n) allowed."""
    tag, content, end = _tlv(plaintext, 0)
    if tag != 0x31:
        raise FormatError("not a DER dictionary")
    tail = plaintext[end:]
    if tail and (len(tail) != tail[-1] or tail.count(tail[-1]) != len(tail)):
        raise FormatError("unexpected bytes after the dictionary")
    return der_dict(content)


# -------------------------------------------------------------------------------------- the items

def _authenticated_ciphertext(archive):
    """``(ciphertext, tag, nonce)`` from an ``_SFAuthenticatedCiphertext`` NSKeyedArchiver plist."""
    try:
        plist = plistlib.loads(archive)
        objects = plist["$objects"]
        root = objects[plist["$top"]["root"].data]

        def field(name):
            ref = root[name]
            return objects[ref.data] if isinstance(ref, plistlib.UID) else ref
        return field("SFCiphertext"), field("SFAuthenticationCode"), field("SFInitializationVector")
    except (plistlib.InvalidFileException, KeyError, IndexError, TypeError, AttributeError,
            ValueError) as error:
        raise FormatError(f"not an authenticated ciphertext ({error})") from error


def open_sealed(key, archive):
    """AES-256-GCM decryption of an archived ciphertext; the tag must verify."""
    ciphertext, tag, nonce = _authenticated_ciphertext(archive)
    try:
        return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)
    except (ValueError, TypeError) as error:
        raise FormatError(f"GCM tag does not verify ({error})") from error


def decrypt_item(entry, class_keys):
    """One entry's attributes and secret as a dict, or raise :class:`FormatError`.

    The attributes are required. The secret is added when its key is present and it decrypts;
    otherwise the item is returned without ``v_Data``.
    """
    meta = entry.get("metadata") or {}
    class_key = class_keys.get(str(entry.get("classKeyIdx")))
    if class_key is None:
        raise FormatError(f"no class key {entry.get('classKeyIdx')!r} in the export")
    metadata_key = open_sealed(class_key, meta.get("wrappedKey"))
    item = read_dictionary(open_sealed(metadata_key, meta.get("ciphertext")))
    data = entry.get("data") or {}
    if data.get("unwrappedKey") and data.get("ciphertext"):
        try:
            secret = read_dictionary(open_sealed(data["unwrappedKey"], data["ciphertext"]))
            for name, value in secret.items():
                if name == "TamperCheck" and name in item:
                    continue                       # the attributes carry one too; theirs is kept
                item[name] = value
        except FormatError as error:
            logger.debug(f"UFED keychain: row {entry.get('rowID')} secret not readable ({error})")
    for name in ("table", "rowID", "classKeyIdx"):
        if name in entry:
            item.setdefault(name, entry[name])
    return item


def decrypt_export(path):
    """``(items, problems)``: every readable item of the export at ``path``, and how many were not."""
    with open(path, "rb") as fh:
        export = plistlib.load(fh)
    if not isinstance(export, dict) or ENTRIES not in export:
        raise FormatError(f"{path} is not a UFED keychain export (no {ENTRIES})")
    class_keys = {str(k): v for k, v in (export.get(CLASS_KEYS) or {}).items()}
    items, problems = [], 0
    for entry in export[ENTRIES]:
        try:
            items.append(decrypt_item(entry, class_keys))
        except FormatError as error:
            problems += 1
            logger.debug(f"UFED keychain: row {entry.get('rowID')} not readable ({error})")
    return items, problems


def _plist_safe(value):
    """A value plistlib can write: None dropped by the caller, oversized ints kept as text."""
    if isinstance(value, int) and not isinstance(value, bool) and not -(1 << 63) <= value < (1 << 64):
        return str(value)
    if isinstance(value, list):
        return [_plist_safe(v) for v in value if v is not None]
    if isinstance(value, dict):
        return {k: _plist_safe(v) for k, v in value.items() if v is not None}
    return value


def write_decrypted_plist(items, path):
    with open(path, "wb") as fh:
        plistlib.dump([_plist_safe(item) for item in items], fh, fmt=plistlib.FMT_BINARY)


def main(input_keychain, output_keychain):
    """Decrypt ``input_keychain`` and write ``output_keychain`` (``decrypted_keychain.plist``)."""
    items, problems = decrypt_export(input_keychain)
    write_decrypted_plist(items, output_keychain)
    logger.info(f"UFED keychain: {len(items)} item(s) decrypted"
                + (f", {problems} not readable (class key or item key not in the export)"
                   if problems else ""))
    return items
