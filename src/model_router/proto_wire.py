"""Tiny protobuf + Connect-RPC wire helpers (stdlib only).

Python port of the hand-rolled encode/decode in opencode-windsurf-auth
src/cloud-direct/wire.ts (MIT, see THIRD_PARTY.md). Only the primitives the
subscription gateways need: varints, length-delimited fields, fixed64
doubles, and the Connect-streaming 5-byte envelope
(flags | uint32-BE length | payload; flag 0x01 = gzip, 0x02 = end-of-stream).
"""

from __future__ import annotations

import gzip
import struct
from collections.abc import Iterator


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative varints are not supported")
    out = bytearray()
    while value > 127:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_tag(field: int, wire: int) -> bytes:
    # Multi-byte tags (field >= 16) fall out of varint encoding for free —
    # never encode the tag as a single byte.
    return encode_varint((field << 3) | wire)


def encode_bytes(field: int, payload: bytes) -> bytes:
    return encode_tag(field, 2) + encode_varint(len(payload)) + payload


def encode_string(field: int, text: str) -> bytes:
    return encode_bytes(field, text.encode("utf-8"))


def encode_message(field: int, body: bytes) -> bytes:
    return encode_bytes(field, body)


def encode_varint_field(field: int, value: int) -> bytes:
    return encode_tag(field, 0) + encode_varint(value)


def encode_double_field(field: int, value: float) -> bytes:
    return encode_tag(field, 1) + struct.pack("<d", value)


def encode_timestamp_body() -> bytes:
    import time

    now = time.time()
    seconds, nanos = int(now), int((now % 1) * 1_000_000_000)
    body = encode_varint_field(1, seconds)
    if nanos:
        body += encode_varint_field(2, nanos)
    return body


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
    raise ValueError("truncated varint")


def iter_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield (field_number, wire_type, value); varints as int, rest as bytes.

    Stops cleanly on truncated frames, deprecated group markers (wire 3/4),
    and unknown wire types rather than misaligning.
    """
    i, n = 0, len(data)
    while i < n:
        tag, i = decode_varint(data, i)
        num, wire = tag >> 3, tag & 0x7
        if wire == 0:
            value, i = decode_varint(data, i)
        elif wire == 1:
            if i + 8 > n:
                return
            value = data[i:i + 8]
            i += 8
        elif wire == 2:
            length, i = decode_varint(data, i)
            if i + length > n:
                return
            value = data[i:i + length]
            i += length
        elif wire == 5:
            if i + 4 > n:
                return
            value = data[i:i + 4]
            i += 4
        else:
            return
        yield num, wire, value


def field_text(payload: bytes, wanted: int) -> str:
    """First length-delimited field `wanted` as UTF-8 text ('' when absent)."""
    for num, wire, value in iter_fields(payload):
        if num == wanted and wire == 2 and isinstance(value, bytes):
            return value.decode("utf-8", "replace")
    return ""


def frame_connect(body: bytes, compress: bool = True) -> bytes:
    payload, flags = (gzip.compress(body), 0x01) if compress else (body, 0)
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def iter_connect_frames(resp) -> Iterator[tuple[int, bytes]]:
    """Yield (flags, payload) Connect frames from a live response stream.

    `resp` is the file-like object urllib.urlopen returns; reads block until
    bytes arrive, so frames surface as the server sends them. Gzip frames
    are decompressed here; a truncated final frame ends iteration.
    """
    while True:
        header = resp.read(5)
        if len(header) < 5:
            return
        flags = header[0]
        length = int.from_bytes(header[1:5], "big")
        payload = bytearray()
        while len(payload) < length:
            chunk = resp.read(length - len(payload))
            if not chunk:
                return
            payload += chunk
        raw = bytes(payload)
        if flags & 0x01:
            raw = gzip.decompress(raw)
        yield flags, raw
