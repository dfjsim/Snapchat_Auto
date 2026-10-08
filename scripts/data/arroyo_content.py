"""What an ``arroyo.db`` ``conversation_message`` row *is*: its content type, and its body.

``conversation_message.message_content`` is a protobuf. Field 4 of it is the message envelope, and
two of the envelope's fields are what this module reads:

* **4.2 — the content type.** The same value as the row's ``content_type`` column (the field is
  absent when the value is 0). The column and the field agree on every row of every test extraction,
  so the column can be trusted as the message's type and the field used to check it.
* **4.4 — the body.** A message holding exactly one of the fields below; *which* field is present
  says what kind of message this is::

      4.4.2   text                     4.4.2.1 = the text
      4.4.3   media sent in the chat   one entry per item
      4.4.5   something shared         a Story, a Spotlight or public-profile Snap, a map pin, …
      4.4.6   voice note
      4.4.7   reply to a Snap or Story 4.4.7.3 = the Snap replied to; 4.4.7.11.1 = the reply's text
      4.4.8   app event                the field under 4.4.8 names the event (see _EVENTS)
      4.4.11  Snap
      4.4.19  Tiny Snap                4.4.19.1.1 = its text
      4.4.24  bot response             4.4.24.2.1.1 = the response text, one per part
      4.4.25  notification item
      4.4.26  poll                     4.4.26.1 = question, 4.4.26.2 = each option

A **reply** deserves the warning it gets in the reports: the media a reply row carries is the Snap
being replied to — typically the other person's Story — not something the replier sent, and its text
(4.4.7.11.1) is the reply, not a caption on that media.

Everything is read straight off the wire format (field numbers and lengths are unambiguous); nothing
here guesses at a schema. A body or an event this module does not know is reported by its field
number, never guessed at.
"""
from __future__ import annotations

import base64
import re
import struct
import uuid
from io import BytesIO

from scripts.data import ccl_bplist, protobuf_wire

# --------------------------------------------------------------------------- content_type

# conversation_message.content_type -> (label, category). The category is what the reports need to
# know about a row with no cached file: "media" means the message carries media (so the file is
# simply not here), "event" that it is an app event (so there never was a file).
CONTENT_TYPES = {
    0: ("Snap", "media"),
    1: ("Text", "text"),
    2: ("Media", "media"),
    3: ("Shared content", "share"),
    4: ("Voice note", "media"),
    5: ("Sticker", "sticker"),
    6: ("App event", "event"),
    8: ("Location", "other"),
    9: ("Saved to camera roll", "event"),
    10: ("Screenshot", "event"),
    11: ("Screen recording", "event"),
    12: ("Missed video call", "event"),
    13: ("Missed audio call", "event"),
    14: ("Group invite link changed", "event"),
    15: ("Mini app", "other"),
    16: ("Live location share", "other"),
    17: ("Creative tool item", "other"),
    18: ("Family Center invite", "other"),
    19: ("Family Center invite accepted", "event"),
    20: ("Left Family Center", "event"),
    21: ("Snap not viewable", "other"),
    22: ("Snapchat+ gift", "event"),
    23: ("Bot response (not a member)", "other"),
    24: ("Upgrade prompt", "other"),
    25: ("Prompt lens response", "other"),
    26: ("Tiny Snap", "media"),
    27: ("Countdown", "event"),
    28: ("Map reaction", "other"),
    29: ("My AI response (Spectacles)", "other"),
    30: ("Snap remixed", "event"),
    31: ("Sticker cut out", "event"),
    32: ("Friend place alert", "event"),
    33: ("AI response", "other"),
    34: ("Bot response", "other"),
    35: ("Notification item", "other"),
    36: ("Event update", "event"),
    37: ("Poll", "other"),
}


def content_type_label(value):
    """The readable name of a content_type, or "" for a value not in the table."""
    try:
        return CONTENT_TYPES.get(int(value), ("", ""))[0]
    except (TypeError, ValueError):
        return ""


def content_type_category(value):
    try:
        return CONTENT_TYPES.get(int(value), ("", ""))[1]
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------------------- wire format

# The reader is shared with every other schema-less decode in the project
# (scripts/data/protobuf_wire.py); the private names are kept because this module uses them.
_Malformed = protobuf_wire.Malformed
_varint = protobuf_wire.varint
_fields = protobuf_wire.fields


class _Msg:
    """One decoded message level: ``one(n)`` / ``all(n)`` / ``int(n)`` / ``text(n)``."""

    def __init__(self, data):
        self.fields = _fields(bytes(data)) if data else []

    def all(self, number, wire=None):
        return [v for f, w, v in self.fields if f == number and (wire is None or w == wire)]

    def one(self, number):
        """The last length-delimited value of ``number`` as a message (last one wins), or None."""
        found = self.all(number, 2)
        if not found:
            return None
        try:
            return _Msg(found[-1])
        except _Malformed:
            return None

    def has(self, number):
        return any(f == number for f, _w, _v in self.fields)

    def int(self, number, default=None):
        found = self.all(number, 0)
        return found[-1] if found else default

    def text(self, number, default=""):
        found = self.all(number, 2)
        if not found:
            return default
        try:
            return bytes(found[-1]).decode("utf-8")
        except UnicodeDecodeError:
            return default

    def double(self, number):
        found = self.all(number, 1)
        return struct.unpack("<d", found[-1])[0] if found else None

    def user(self, number):
        """A user id: a message whose field 1 is 16 bytes, written as a UUID. "" when absent."""
        inner = self.one(number)
        if inner is None:
            return ""
        raw = inner.all(1, 2)
        return str(uuid.UUID(bytes=bytes(raw[-1]))) if raw and len(raw[-1]) == 16 else ""

    def users(self, number):
        out = []
        for raw in self.all(number, 2):
            try:
                inner = _Msg(raw).all(1, 2)
            except _Malformed:
                continue
            if inner and len(inner[-1]) == 16:
                out.append(str(uuid.UUID(bytes=bytes(inner[-1]))))
        return out


def _body(blob):
    """``(field number under 4.4, that field's message)`` or ``(None, None)``."""
    if blob is None or isinstance(blob, str):
        return None, None
    try:
        envelope = _Msg(blob).one(4)
        contents = envelope.one(4) if envelope is not None else None
    except _Malformed:
        return None, None
    if contents is None or not contents.fields:
        return None, None
    number = contents.fields[0][0]
    return number, contents.one(number)


def embedded_content_type(blob):
    """The content type stored at 4.2 (0 when absent), or None if there is no envelope."""
    if blob is None or isinstance(blob, str):
        return None
    try:
        envelope = _Msg(blob).one(4)
    except _Malformed:
        return None
    return None if envelope is None else envelope.int(2, 0)


# --------------------------------------------------------------------------- the text

def message_text(blob):
    """The text the message carries, read from the field that holds it; "" when it has none.

    4.4.2.1 for a text message, 4.4.7.11.1 for the text of a reply to a Snap or Story, 4.4.19.1.1
    for a Tiny Snap, and every 4.4.24.2.1.1 part of a bot response, joined.
    """
    number, body = _body(blob)
    if body is None:
        return ""
    if number == 2:
        return body.text(1)
    if number == 7:
        reply = body.one(11)
        return reply.text(1) if reply is not None else ""
    if number == 19:
        inner = body.one(1)
        return inner.text(1) if inner is not None else ""
    if number == 24:
        parts = []
        for raw in body.all(2, 2):
            try:
                part = _Msg(raw).one(1)
            except _Malformed:
                continue
            if part is not None and part.text(1):
                parts.append(part.text(1))
        return "\n".join(parts)
    return ""


# --------------------------------------------------------------------------- the body

def _duration(ms):
    seconds = round(ms / 1000)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _span(seconds):
    if seconds % 86400 == 0 and seconds:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0 and seconds:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{seconds} s"


def _who(user):
    return user or "someone"


_CALL_STATUS = {0: "started", 1: "ended", 2: "left", 3: "joined", 4: "missed"}
_CAPTURE_WHERE = {0: "the chat", 1: "the friendship profile", 2: "the group profile", 3: "the call"}
_JOIN_ORIGIN = {1: " (group invite sticker)", 2: " (invite link)", 3: " (community)",
                4: " (public group)"}


def _screen_capture(e):
    what = "screen-recorded" if e.int(2, 0) == 1 else "took a screenshot of"
    where = _CAPTURE_WHERE.get(e.int(3, 0), "the chat")
    removed = " (no longer a member of the group)" if e.int(4, 0) == 2 else ""
    return f"{_who(e.user(1))}{removed} {what} {where}"


def _call(e):
    kind = "Video" if e.int(2, 0) == 1 else "Audio"
    text = f"{kind} call {_CALL_STATUS.get(e.int(1, 0), 'status ' + str(e.int(1)))}"
    if e.user(3):
        text += f" — {e.user(3)}"
    if e.int(4):
        text += f", duration {_duration(e.int(4))}"
    people = e.users(5)
    if people:
        text += f", participants: {', '.join(people)}"
    return text


def _participants(e):
    parts = []
    for raw in e.all(1, 2):
        try:
            entry = _Msg(raw)
        except _Malformed:
            continue
        who = _who(entry.user(1))
        change, reason = entry.int(2, 0), entry.int(4, 0)
        if change == 1:
            parts.append(f"{who} created the group")
        elif change == 2:
            parts.append(f"{who} was removed from the group" if reason == 2
                         else f"{who} left the group")
        else:
            parts.append(f"{who} was added to the group{_JOIN_ORIGIN.get(entry.int(3, 0), '')}")
    by = e.user(3) or e.user(2)
    return "; ".join(parts) + (f" — by {by}" if by else "")


def _saved(e):
    counts = []
    for raw in e.all(3, 2):
        try:
            entry = _Msg(raw)
        except _Malformed:
            continue
        noun = {1: "photo", 2: "video"}.get(entry.int(1, 0), "item")
        count = entry.int(2, 1)
        counts.append(f"{count} {noun}{'s' if count != 1 else ''}")
    target = e.int(2)
    source = f" from message {target}" if target is not None else ""
    return f"{_who(e.user(1))} saved {', '.join(counts) or 'media'}{source} to the camera roll"


def _retention(e):
    policy = e.one(2)
    dynamic = policy.one(1) if policy is not None else None
    detail = []
    if dynamic is not None:
        if dynamic.int(5):
            detail.append("messages are kept")
        if dynamic.int(4) is not None:
            detail.append(f"viewed messages delete after {_span(dynamic.int(4))}")
        if dynamic.int(3) is not None:
            detail.append(f"unviewed messages after {_span(dynamic.int(3))}")
    text = f"{_who(e.user(1))} changed when this chat's messages delete"
    return text + (f": {', '.join(detail)}" if detail else "")


def _streak(e):
    state = {1: "started", 2: "ended", 3: "restored"}.get(e.int(1, 0), "changed")
    count = e.int(2)
    return f"Streak {state}" + (f" ({count} days)" if count else "")


def _countdown(e):
    state = {1: "created", 2: "deleted", 3: "updated", 4: "started"}.get(e.int(2, 0), "changed")
    name = e.text(3)
    return f"Countdown “{name}” {state}" if name else f"Countdown {state}"


def _place_alert(e):
    kind = {1: "home safe", 2: "custom place"}.get(e.int(1, 0), "")
    state = {1: "on", 2: "complete", 3: "off", 4: "expired", 5: "invalid (friend stopped sharing)",
             6: "invalid (location permission)"}.get(e.int(2, 0), "")
    name = e.text(3)
    return "Friend place alert" + (f" “{name}”" if name else "") + \
        (f" ({kind})" if kind else "") + (f": {state}" if state else "")


# 4.4.8.<n> -> a function of the event's message returning a sentence. The field number under 4.4.8
# is the event; the ones with a fixed wording take no fields.
_EVENTS = {
    1: _screen_capture,
    2: _call,
    3: _participants,
    4: lambda e: (f"{_who(e.user(1))} renamed the group from “{e.text(2)}” to “{e.text(3)}”"),
    5: lambda e: (f"{_who(e.user(1))} deleted "
                  f"{ {1: 'a chat message', 2: 'a Snap'}.get(e.int(2, 0), 'a message') }"),
    6: lambda e: (f"{_who(e.user(1))} created the group “{e.text(3)}” with "
                  f"{len(e.users(2))} participant(s)"),
    7: _saved,
    8: _retention,
    9: lambda e: "A game was closed",
    10: lambda e: (f"{_who(e.user(1))} "
                   f"{ {1: 'created', 2: 'deleted'}.get(e.int(2, 0), 'changed') } "
                   f"the group's invite link"),
    11: lambda e: f"Group invite prompt for “{e.text(2)}”",
    12: lambda e: "App update" + (f": “{e.text(3)}”" if e.text(3) else ""),
    13: lambda e: ("Live location sharing ended"
                   + {1: " (session expired)", 2: " (stopped)"}.get(e.int(2, 0), "")
                   + (f" — {e.user(1)}" if e.user(1) else "")),
    14: lambda e: "Contact joined notice",
    15: lambda e: "Family Center invite accepted",
    16: lambda e: "Left Family Center",
    17: lambda e: "Snapchat for web notice",
    18: lambda e: "A reply was added to a Story",
    19: lambda e: "Chat wallpaper " + ("remixed" if e.int(1, 0) == 1 else "changed"),
    20: lambda e: "Snapchat+ gift",
    21: _streak,
    22: lambda e: "My AI welcome message",
    23: lambda e: "Group live location notice",
    24: lambda e: f"{_who(e.user(1))} changed how Snaps can be viewed after opening",
    25: _countdown,
    26: lambda e: "A Snap was remixed",
    27: lambda e: "A sticker was made from a photo",
    28: _place_alert,
    29: lambda e: "Brand collaboration intro",
    30: lambda e: "Welcome message",
    31: lambda e: "Sponsored welcome message",
    32: lambda e: ("Location request accepted" if e.int(1, 0) == 1 else
                   "Live location request accepted" if e.int(1, 0) == 2 else "Location request"),
    33: lambda e: "Event " + {1: "created", 2: "deleted", 3: "updated",
                              4: "started"}.get(e.int(2, 0), "changed"),
}

_SHARES = {
    5: lambda s: "Shared a Story",
    14: lambda s: "Shared a public profile Snap",
    16: lambda s: "Shared a Spotlight Snap",
    18: lambda s: _map_pin(s),
    24: lambda s: _saved_story(s),
    35: lambda s: "Shared a sports game",
    37: lambda s: "Shared an event",
}


def _saved_story(share):
    """A saved Story: .1 is who posted it, .2 the Snap itself, whose .18.1 names its poster too.

    The two ids are written independently — one by the share, one inside the Snap — so the poster is
    only stated when they agree (or only one is there). When they disagree both are given, because
    choosing one would be a guess.
    """
    creator = share.user(1)
    snap = share.one(2)
    attribution = snap.one(18) if snap is not None else None
    poster = attribution.text(1) if attribution is not None else ""
    if creator and poster and creator.lower() != poster.lower():
        return f"Shared a saved Story (story id names {creator}, the Snap names {poster})"
    who = creator or poster
    return f"Shared a saved Story posted by {who}" if who else "Shared a saved Story"


def _map_pin(pin):
    lat, lon = pin.double(1), pin.double(2)
    text = "Shared a map pin"
    if lat is not None and lon is not None:
        text += f" at {lat:.6f}, {lon:.6f}"
    if pin.text(6):
        text += f" “{pin.text(6)}”"
    return text


def referenced_message(blob):
    """The server_message_id an app event is about, or None.

    A save to the camera roll (4.4.8.7) names the message whose media was saved at 4.4.8.7.2 — a
    ``server_message_id`` of the same conversation — so a report can link the event to that message.
    """
    number, body = _body(blob)
    if number != 8 or body is None:
        return None
    event = body.one(7)
    return event.int(2) if event is not None else None


def event_number(blob):
    """The field under 4.4.8 of an app-event row, or None when the row is not an app event."""
    number, body = _body(blob)
    if number != 8 or body is None or not body.fields:
        return None
    return body.fields[0][0]


def describe(blob):
    """A short description of the body, or "" for a plain text, media or Snap message.

    The reports show it beside (never as) the text a person typed: for an app event it *is* the
    content; for a reply, a share or a voice note it says what the row carries.
    """
    number, body = _body(blob)
    if number is None or number in (2, 3, 11):
        return ""
    if body is None:
        return f"Body field 4.4.{number} (not described)"
    try:
        if number == 8:
            if not body.fields:
                return "App event (empty)"
            event = body.fields[0][0]
            handler = _EVENTS.get(event)
            inner = body.one(event) or _Msg(b"")
            return handler(inner) if handler else f"App event 4.4.8.{event} (not described)"
        if number == 5:
            if not body.fields:
                return "Shared content (empty)"
            kind = body.fields[0][0]
            handler = _SHARES.get(kind)
            return handler(body.one(kind) or _Msg(b"")) if handler else \
                f"Shared content (4.4.5.{kind}, not described)"
        if number == 7:
            reply = next((f for f, _w, _v in body.fields if f in (11, 12, 15, 17)), None)
            kind = {12: " with media", 15: " with a voice note", 17: " with a Snap"}.get(reply, "")
            return (f"Reply to a Snap or Story{kind} — the media is the Snap replied to, "
                    f"not something the replier sent")
        if number == 6:
            return "Voice note"
        if number == 19:
            return "Tiny Snap"
        if number == 24:
            return "Bot response"
        if number == 25:
            title, subtitle = body.text(3), body.text(4)
            return "Notification: " + " — ".join(t for t in (title, subtitle) if t)
        if number == 26:
            options = [bytes(o).decode("utf-8", "replace") for o in body.all(2, 2)]
            return f"Poll: “{body.text(1)}”" + (f" — options: {' / '.join(options)}"
                                                if options else "")
        if number == 14:
            return "Creative tool item"
        if number == 18:
            return "Prompt lens response"
    except _Malformed:
        return f"Body field 4.4.{number} (could not be read)"
    return f"Body field 4.4.{number} (not described)"


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# --------------------------------------------------------------------------- content ids

#: The ids a message names its media by — what a cache claim's key can carry — and where each is
#: read. Each one is a link rule of its own, and the cache_controller report says which one made a
#: link. An id shorter than MIN_CONTENT_ID characters is not used: inside a claim key it could match
#: by chance.
CONTENT_ID_RULES = {
    "media": "a media id the message's local_message_references names (a MEDIA_ID — one per "
             "photo or video of the message)",
    "share": "the id of the item shared in the message (message_content field 4.4.5.5.1)",
    "sticker": "the id of the sticker the message carries (message_content field 4.4.14.2.6), "
               "in base64",
    "sticker-name": "the sticker the message names (message_content field 4.4.4.1.2)",
}
MIN_CONTENT_ID = 8


def _first_text(blob, *path):
    try:
        values = protobuf_wire.values(blob, *path)
    except (protobuf_wire.Malformed, TypeError):
        return None
    try:
        return bytes(values[0]).decode("utf-8") if values and values[0] else None
    except UnicodeDecodeError:
        return None


def sticker_id(blob):
    """``(id, rule)`` of the sticker a Sticker message (content_type 5) carries, or ``(None, None)``.

    A sticker carried as a creative tool item (body 4.4.14) is named by the bytes at 4.4.14.2.6; the
    claim on its cached file holds them in base64, so that is the form returned. A sticker from a
    pack is named by the text at 4.4.4.1.2.
    """
    try:
        ids = protobuf_wire.values(blob, 4, 4, 14, 2, 6)
    except (protobuf_wire.Malformed, TypeError):
        ids = []
    if ids and ids[0]:
        return base64.b64encode(bytes(ids[0])).decode("ascii"), "sticker"
    name = _first_text(blob, 4, 4, 4, 1, 2)
    return (name, "sticker-name") if name else (None, None)


def shared_item_id(blob):
    """The id of the item a Shared content message (content_type 3) shares (4.4.5.5.1), or None."""
    return _first_text(blob, 4, 4, 5, 5, 1)


def _media_id(archive_bytes):
    """The ``MEDIA_ID`` of one keyed archive, read up to the end of its (last) UUID, as the chat join
    reads it — in any letter case. None when the bytes are not such an archive."""
    try:
        archive = ccl_bplist.deserialise_NsKeyedArchiver(ccl_bplist.load(BytesIO(archive_bytes)))
        value = archive["MEDIA_ID"]
    except Exception:                                              # noqa: BLE001 - not this plist
        return None
    last = None
    for last in _UUID_RE.finditer(value if isinstance(value, str) else ""):
        pass
    return value[:last.end()] if last else None


def media_references(blob):
    """Every media id a row's ``local_message_references`` names, in order.

    The column is a sequence of records, one per media item of the message: an 8-byte little-endian
    length, then an NSKeyedArchiver plist of that many bytes whose ``MEDIA_ID`` names the item. A
    message sent with several photos or videos has a record for each; reading only the first lost
    the others. A column that does not divide into such records is read as one archive after its
    first 8 bytes, as before.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)) or len(blob) <= 8:
        return []
    data, out, pos = bytes(blob), [], 0
    while pos + 8 < len(data):
        size = int.from_bytes(data[pos:pos + 8], "little")
        if size <= 0 or pos + 8 + size > len(data):
            break
        media = _media_id(data[pos + 8:pos + 8 + size])
        if media and media not in out:
            out.append(media)
        pos += 8 + size
    if not out:
        media = _media_id(data[8:])
        out = [media] if media else []
    return out


def media_reference(blob):
    """The first media id a row's ``local_message_references`` names, or None — see
    :func:`media_references`."""
    refs = media_references(blob)
    return refs[0] if refs else None


def content_ids(content_type, message_content, local_message_references=None):
    """``[(id, rule)]``: every id this message names its media by (see CONTENT_ID_RULES)."""
    out = [(media, "media") for media in media_references(local_message_references)]
    try:
        kind = int(content_type)
    except (TypeError, ValueError):
        kind = None
    if kind == 3:
        shared = shared_item_id(message_content)
        if shared:
            out.append((shared, "share"))
    elif kind == 5:
        sticker, rule = sticker_id(message_content)
        if sticker:
            out.append((sticker, rule))
    return [(cid, rule) for cid, rule in out if len(cid) >= MIN_CONTENT_ID]


def message_key(server_message_id, client_message_id=None):
    """How a message is found again across the reports: ``"<number>"`` by its server message id, or
    ``"c<client message id>"`` for one the server never numbered (not sent, or still sending) — the
    same ``c`` the Conversations report anchors such a message with. "" when it has neither."""
    number = message_number(server_message_id)
    if number:
        return number
    text = str(client_message_id if client_message_id is not None else "").strip()
    text = text[:-2] if text.endswith(".0") else text
    return f"c{text}" if text.lstrip("-").isdigit() else ""


def message_number(smid):
    """A server message id as its bare number: ``12``, ``"12"``, ``12.0`` and ``"12.0"`` are all
    ``"12"``. The reports write a message's id as ``<number>.<part>`` (the part a cache claim names,
    or ``.0`` from a float column); arroyo.db and a claim's key hold the number. "" when not one."""
    text = str(smid if smid is not None else "").strip()
    head = text.split(".")[0]
    return head if head.lstrip("-").isdigit() else ""


def name_users(text, names):
    """``text`` with each user id that ``names`` ({lowercase id: label}) knows replaced by its label.

    A description names users by id, because that is what the body holds, and the id is the one
    identifier of a user that never changes — so callers keep it in the label ("name (id)") or show
    it beside the text. An id with no known label is left as it is.
    """
    if not text or not names:
        return text
    return _UUID_RE.sub(lambda m: names.get(m.group(0).lower(), m.group(0)), text)
