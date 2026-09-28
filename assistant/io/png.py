"""Writing a PNG without an image library.

The screen capture path gets raw frames from GStreamer and has to put a file on
disk. GStreamer has no PNG encoder installed on a lot of systems, including this
one, and requiring Pillow or ImageMagick just to save a screenshot would undo the
point of this fork. PNG is simple enough to write directly: a signature, three
chunks, and zlib from the standard library.
"""

from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode(width: int, height: int, pixels: bytes, channels: int = 4) -> bytes:
    """Encode raw pixels as an 8-bit RGB or RGBA PNG.

    ``pixels`` is row-major with no padding between rows, which is the layout
    GStreamer's ``videoconvert`` hands over when asked for RGB or RGBA.
    """
    if width <= 0 or height <= 0:
        raise ValueError("A PNG needs a positive width and height.")
    colour_type = {3: 2, 4: 6}.get(channels)
    if colour_type is None:
        raise ValueError("Only 3 (RGB) or 4 (RGBA) channels can be written.")

    stride = width * channels
    if len(pixels) < stride * height:
        raise ValueError(
            f"Not enough pixel data for {width}x{height} at {channels} channels: "
            f"expected {stride * height}, got {len(pixels)}."
        )

    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(pixels[row * stride : (row + 1) * stride])

    return b"".join(
        (
            _SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), 6)),
            _chunk(b"IEND", b""),
        )
    )


def write(path: str, width: int, height: int, pixels: bytes, channels: int = 4) -> int:
    """Write a PNG and return how many bytes it took."""
    data = encode(width, height, pixels, channels)
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)
