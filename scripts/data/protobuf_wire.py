"""The protobuf wire format, read without a schema.

A message is a sequence of fields, each a varint key — field number × 8 + wire type — then a value:
a varint (wire type 0), 8 or 4 fixed bytes (1, 5) or a varint length and that many bytes (2). That
is the whole of it (protobuf.dev, "Encoding"); groups (3, 4) are obsolete and treated as malformed.
Nothing here knows what a field means — callers do — and nothing is guessed: a buffer that does not
parse to its last byte is not a message.
"""


class Malformed(ValueError):
    """The bytes are not a protobuf message (or not one that ends where the buffer does)."""


def varint(data, pos):
    """``(value, next position)`` of the varint at ``pos``."""
    value = shift = 0
    while True:
        if pos >= len(data):
            raise Malformed("varint runs off the end")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift >= 70:
            raise Malformed("varint too long")


def _parse(data):
    """``[(field, wire type, value, start, end)]``: :func:`fields`, with where each value lies."""
    out, pos, end = [], 0, len(data)
    while pos < end:
        key, pos = varint(data, pos)
        field, wire = key >> 3, key & 7
        start = pos
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > end:
                raise Malformed("fixed-size value runs off the end")
            value, pos = data[pos:pos + size], pos + size
        elif wire == 2:
            length, start = varint(data, pos)
            if start + length > end:
                raise Malformed("length runs off the end")
            value, pos = data[start:start + length], start + length
        else:
            raise Malformed(f"wire type {wire}")
        out.append((field, wire, value, start, pos))
    return out


def fields(data):
    """``[(field, wire type, value)]`` of one message; raises :class:`Malformed`.

    A wire-type-2 value is returned as bytes, whether it is a string, raw bytes or a nested message
    — only the caller can say which.
    """
    return [(field, wire, value) for field, wire, value, _start, _end in _parse(data)]


def field_path(data, start, end):
    """The field numbers, outermost first, of the value that holds ``data[start:end]``.

    Read the way :func:`strings` reads: the path goes down while the range lies inside one
    length-delimited value that is not printable text and parses as a message to its last byte, and
    stops at the value that is not (raw bytes, a string). ``()`` when ``data`` is not a message, or
    the range is not inside any one value — it covers a field's key, or runs across two fields.
    """
    path, buf, base = [], bytes(data), 0
    while _text(buf) is None:
        try:
            parsed = _parse(buf)
        except Malformed:
            break
        inside = next(((field, wire, s, e) for field, wire, _value, s, e in parsed
                       if s <= start - base and end - base <= e), None)
        if inside is None:
            break
        field, wire, s, e = inside
        path.append(field)
        if wire != 2:
            break
        buf, base = buf[s:e], base + s
    return tuple(path)


def values(data, *path):
    """Every value at ``path`` (field numbers from the outer message in), in field order.

    Each step but the last must be a nested message; a step that does not parse is
    :class:`Malformed`, so a path through bytes that are not messages fails loudly rather than
    returning a coincidence.
    """
    level = [bytes(data)]
    for depth, number in enumerate(path):
        nxt = []
        for buf in level:
            for field, wire, value in fields(buf):
                if field != number:
                    continue
                if depth < len(path) - 1 and wire != 2:
                    raise Malformed(f"field {number} is not a message")
                nxt.append(value)
        level = nxt
    return level


#: Control characters a typed text contains; any other one means the bytes are not text.
_TEXT_CONTROLS = frozenset("\n\r\t")


def _text(value):
    """``value`` as text when it is printable UTF-8 (line breaks and tabs allowed), else None."""
    try:
        text = bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text and all(ch.isprintable() or ch in _TEXT_CONTROLS for ch in text):
        return text
    return None


def strings(data):
    """Every text value in a message and the messages nested in it, depth first in field order.

    A length-delimited value is text when it is printable UTF-8; otherwise it is searched as a
    nested message when it parses as one to its last byte; otherwise (raw bytes, packed numbers) it
    is skipped. Without a schema "printable" is the only test that separates a word from bytes that
    happen to parse — so a short printable value that would also parse as a message is read as the
    text it looks like. Returns None when ``data`` itself is not a message.
    """
    out = []

    def walk(buf):
        for _field, wire, value in fields(buf):
            if wire != 2:
                continue
            text = _text(value)
            if text is not None:
                out.append(text)
                continue
            try:
                nested = fields(value) if value else []
            except Malformed:
                continue
            if nested:
                walk(value)
    try:
        walk(bytes(data))
    except Malformed:
        return None
    return out


def values_with_paths(data):
    """``[(path, value, text)]`` for every length-delimited value in a message and the messages
    nested in it, each message's values in field order before those nested in them; ``text`` is the
    value as text when it is printable UTF-8, else None.

    Every such value is listed, the nested messages included: without a schema, raw bytes (a 16-byte
    id) can parse as a message by chance, and listing only the innermost values would lose them. A
    value is searched inside on the rule :func:`strings` reads by — not when it is text, and only
    when it parses as a message to its last byte. Returns None when ``data`` is not a message.
    """
    out = []
    try:
        stack = [((), _parse(bytes(data)))]
    except Malformed:
        return None
    while stack:
        path, parsed = stack.pop()
        nested = []
        for field, wire, value, _start, _end in parsed:
            if wire != 2:
                continue
            text = _text(value)
            out.append((path + (field,), value, text))
            if text is None and value:
                try:
                    nested.append((path + (field,), _parse(value)))
                except Malformed:
                    pass
        stack.extend(reversed(nested))       # a stack, not recursion: nesting depth is unbounded
    return out
