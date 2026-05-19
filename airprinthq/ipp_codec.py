"""IPP wire-format codec (RFC 8010 + RFC 8011).

Parses and emits IPP messages as a structured representation that
preserves attribute order and raw value bytes, so decode -> encode is
byte-stable for any input we don't intentionally modify.

Message layout (RFC 8010):
    version-major (1) | version-minor (1)
    operation-id or status-code (2) | request-id (4)
    attribute-groups (each: 1-byte begin-tag + 0+ attributes)
    end-of-attributes-tag (0x03)
    optional data (after end tag, for Print-Job/Send-Document)

Attribute layout:
    value-tag (1) | name-length (2) | name (N) | value-length (2) | value (M)
    additional-values: same shape but name-length=0
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable

# Group (delimiter) tags - RFC 8010 §3.5.1
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED_GROUP = 0x05
TAG_SUBSCRIPTION = 0x06
TAG_EVENT_NOTIFICATION = 0x07

DELIMITER_TAGS = {
    TAG_OPERATION, TAG_JOB, TAG_END, TAG_PRINTER,
    TAG_UNSUPPORTED_GROUP, TAG_SUBSCRIPTION, TAG_EVENT_NOTIFICATION,
}

# Out-of-band value tags - RFC 8010 §3.5.2
TAG_UNSUPPORTED_VALUE = 0x10
TAG_UNKNOWN = 0x12
TAG_NO_VALUE = 0x13

# Integer value tags
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23

# Octet-string value tags
TAG_OCTET_STRING = 0x30
TAG_DATE_TIME = 0x31
TAG_RESOLUTION = 0x32
TAG_RANGE_OF_INTEGER = 0x33
TAG_BEG_COLLECTION = 0x34
TAG_TEXT_WITH_LANG = 0x35
TAG_NAME_WITH_LANG = 0x36
TAG_END_COLLECTION = 0x37

# Character-string value tags
TAG_TEXT_WITHOUT_LANG = 0x41
TAG_NAME_WITHOUT_LANG = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_URI_SCHEME = 0x46
TAG_CHARSET = 0x47
TAG_NATURAL_LANGUAGE = 0x48
TAG_MIME_MEDIA_TYPE = 0x49
TAG_MEMBER_ATTR_NAME = 0x4A

# Operations - RFC 8011 §4.2 (we list the ones we care about)
OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# Status codes - RFC 8011 §13
STATUS_OK = 0x0000
STATUS_OK_IGNORED = 0x0001
STATUS_CLIENT_ERROR_BAD_REQUEST = 0x0400
STATUS_CLIENT_ERROR_NOT_FOUND = 0x0406
STATUS_SERVER_ERROR_INTERNAL = 0x0500
STATUS_SERVER_ERROR_OP_NOT_SUPPORTED = 0x0501


@dataclass
class Attribute:
    """A single IPP attribute, possibly multi-valued.

    Each value is stored as (value_tag, raw_bytes) — raw_bytes is exactly
    what appears on the wire. Use the helpers below (decode_*) to interpret.
    """
    name: str
    values: list[tuple[int, bytes]] = field(default_factory=list)


@dataclass
class Group:
    tag: int
    attributes: list[Attribute] = field(default_factory=list)


@dataclass
class IppMessage:
    version: tuple[int, int] = (2, 0)
    operation_or_status: int = 0
    request_id: int = 1
    groups: list[Group] = field(default_factory=list)
    data: bytes = b""

    def group(self, tag: int) -> Group | None:
        for g in self.groups:
            if g.tag == tag:
                return g
        return None

    def attr(self, group_tag: int, name: str) -> Attribute | None:
        g = self.group(group_tag)
        if g is None:
            return None
        for a in g.attributes:
            if a.name == name:
                return a
        return None


# --- decode ------------------------------------------------------------

class IppParseError(ValueError):
    pass


def decode(buf: bytes) -> IppMessage:
    if len(buf) < 8:
        raise IppParseError("message shorter than 8-byte header")
    major, minor, opstat, request_id = struct.unpack(">BBHI", buf[:8])
    msg = IppMessage(version=(major, minor),
                     operation_or_status=opstat,
                     request_id=request_id)
    pos = 8
    current_group: Group | None = None
    current_attr: Attribute | None = None
    n = len(buf)

    while pos < n:
        tag = buf[pos]
        pos += 1
        if tag in DELIMITER_TAGS:
            current_attr = None
            if tag == TAG_END:
                # rest of buffer is document data
                msg.data = buf[pos:]
                return msg
            current_group = Group(tag=tag)
            msg.groups.append(current_group)
            continue

        # Value tag
        if pos + 2 > n:
            raise IppParseError("truncated name-length")
        name_len = struct.unpack(">H", buf[pos:pos + 2])[0]
        pos += 2
        if pos + name_len > n:
            raise IppParseError("truncated name")
        name = buf[pos:pos + name_len].decode("utf-8", errors="replace")
        pos += name_len
        if pos + 2 > n:
            raise IppParseError("truncated value-length")
        value_len = struct.unpack(">H", buf[pos:pos + 2])[0]
        pos += 2
        if pos + value_len > n:
            raise IppParseError("truncated value")
        value = buf[pos:pos + value_len]
        pos += value_len

        if name_len == 0:
            # additional value for the previous attribute
            if current_attr is None:
                raise IppParseError("additional value with no current attribute")
            current_attr.values.append((tag, value))
        else:
            if current_group is None:
                raise IppParseError(f"attribute {name!r} outside any group")
            current_attr = Attribute(name=name, values=[(tag, value)])
            current_group.attributes.append(current_attr)

    # Reached end of buffer without an end-of-attributes tag.
    return msg


# --- encode ------------------------------------------------------------

def encode(msg: IppMessage) -> bytes:
    out = bytearray()
    out += struct.pack(">BBHI", msg.version[0], msg.version[1],
                       msg.operation_or_status, msg.request_id)
    for g in msg.groups:
        out.append(g.tag)
        for a in g.attributes:
            name_bytes = a.name.encode("utf-8")
            if not a.values:
                raise ValueError(f"attribute {a.name!r} has no values")
            tag, value = a.values[0]
            out.append(tag)
            out += struct.pack(">H", len(name_bytes))
            out += name_bytes
            out += struct.pack(">H", len(value))
            out += value
            for tag, value in a.values[1:]:
                out.append(tag)
                out += struct.pack(">H", 0)  # empty name = additional value
                out += struct.pack(">H", len(value))
                out += value
    out.append(TAG_END)
    out += msg.data
    return bytes(out)


# --- typed value helpers ----------------------------------------------
#
# Use these on Attribute.values entries to interpret the raw bytes.
# For multi-valued attrs, iterate values and decode each entry whose tag
# is the type you expect.

def decode_integer(raw: bytes) -> int:
    if len(raw) != 4:
        raise ValueError(f"integer must be 4 bytes, got {len(raw)}")
    return struct.unpack(">i", raw)[0]


def encode_integer(value: int) -> bytes:
    return struct.pack(">i", value)


def decode_boolean(raw: bytes) -> bool:
    if len(raw) != 1:
        raise ValueError(f"boolean must be 1 byte, got {len(raw)}")
    return raw[0] != 0


def encode_boolean(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def decode_string(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def encode_string(value: str) -> bytes:
    return value.encode("utf-8")


def decode_resolution(raw: bytes) -> tuple[int, int, int]:
    """Returns (xres, yres, units). units: 3=dpi, 4=dpcm."""
    if len(raw) != 9:
        raise ValueError(f"resolution must be 9 bytes, got {len(raw)}")
    return struct.unpack(">iiB", raw)


def encode_resolution(xres: int, yres: int, units: int = 3) -> bytes:
    return struct.pack(">iiB", xres, yres, units)


def decode_range_of_integer(raw: bytes) -> tuple[int, int]:
    if len(raw) != 8:
        raise ValueError(f"rangeOfInteger must be 8 bytes, got {len(raw)}")
    return struct.unpack(">ii", raw)


def encode_range_of_integer(lo: int, hi: int) -> bytes:
    return struct.pack(">ii", lo, hi)


# --- attribute construction shortcuts ---------------------------------

def attr(name: str, tag: int, *values: bytes) -> Attribute:
    return Attribute(name=name, values=[(tag, v) for v in values])


def str_attr(name: str, tag: int, *values: str) -> Attribute:
    return Attribute(name=name, values=[(tag, v.encode("utf-8")) for v in values])


def int_attr(name: str, *values: int, tag: int = TAG_INTEGER) -> Attribute:
    return Attribute(name=name,
                     values=[(tag, struct.pack(">i", v)) for v in values])


def bool_attr(name: str, value: bool) -> Attribute:
    return Attribute(name=name,
                     values=[(TAG_BOOLEAN, b"\x01" if value else b"\x00")])
