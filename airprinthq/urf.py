"""URF (Apple Unified Raster Format) decoder.

URF is Apple's proprietary raster format. iOS sends jobs as URF when it
rasterizes content client-side (notably for monochrome jobs). Never
officially published as a spec; this implementation is based on public
reverse-engineering (CUPS source, leaked PDFs, and inspection of real
iOS-generated files).

Not affiliated with Apple Inc. AirPrint is a trademark of Apple Inc.

File layout:

    "UNIRAST\\0"     8 bytes        — magic
    page_count       u32 big-endian — number of pages

    for each page:
        page_header  32 bytes       — see PageHeader below
        pixel_data   variable       — line-RLE encoded pixel rows

Per-page header (32 bytes, the fields we care about):

    offset  size  field
    0x00    u8    bits_per_pixel       (8 = grayscale, 24 = RGB, 32 = CMYK)
    0x01    u8    color_space          (informational; we don't use)
    0x02    u8    duplex
    0x03    u8    quality
    0x04    u32   ...                  (media type / slot — unused by decoder)
    0x08    u32   ...
    0x0C    u32   width_pixels
    0x10    u32   height_pixels
    0x14    u32   resolution_dpi
    0x18    u32   ...                  (media size — unused)
    0x1C    u32   ...

Pixel data (per page), repeated until height rows are produced:

    u8  row_repeat       — this row is to appear (row_repeat + 1) times

    Then packets until width pixels are produced for the row.
    Let `components = bits_per_pixel / 8` (bytes per pixel):

        u8 ctl
        if ctl == 0x80:        end of row, pad remaining pixels with 0x00.
        elif ctl <= 0x7F:      repeat next pixel (ctl + 1) times.
                               Read `components` bytes; emit them (ctl+1) times.
        else (0x81..0xFF):     literal run of (257 - ctl) pixels.
                               Read (257 - ctl) * components bytes; emit verbatim.

URF grayscale uses the standard graphics convention: 0x00 = black,
0xFF = white (paper). Same as PIL "L" mode, so no inversion needed.
Row buffer is initialized to 0xFF (paper white) so the EOL pad leaves
the remainder of the row at white — that's the URF semantic for "rest
of row is page background".
"""

from __future__ import annotations

import io
import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAGIC = b"UNIRAST\x00"


@dataclass
class UrfPage:
    width: int                 # pixels
    height: int                # pixels
    dpi: int
    bits_per_pixel: int
    pixels: bytes              # raw pixel buffer, length = width * height * components

    @property
    def components(self) -> int:
        return self.bits_per_pixel // 8

    @property
    def pil_mode(self) -> str:
        return {1: "L", 3: "RGB", 4: "CMYK"}[self.components]


class UrfDecodeError(ValueError):
    pass


def decode(data: bytes) -> list[UrfPage]:
    """Parse URF bytes into a list of pages."""
    if data[:8] != MAGIC:
        raise UrfDecodeError("not URF (bad magic)")
    page_count = struct.unpack(">I", data[8:12])[0]
    pos = 12
    pages: list[UrfPage] = []
    for i in range(page_count):
        if pos + 32 > len(data):
            raise UrfDecodeError(f"truncated header for page {i}")
        bpp = data[pos]
        width = struct.unpack(">I", data[pos + 0x0C:pos + 0x10])[0]
        height = struct.unpack(">I", data[pos + 0x10:pos + 0x14])[0]
        dpi = struct.unpack(">I", data[pos + 0x14:pos + 0x18])[0]
        pos += 32
        components = bpp // 8
        if components not in (1, 3, 4):
            raise UrfDecodeError(f"page {i}: unsupported bpp={bpp}")
        if width <= 0 or height <= 0:
            raise UrfDecodeError(
                f"page {i}: bad dimensions {width}x{height}")
        row_bytes = width * components
        # Init the page buffer to 0xFF (paper white) — this is the URF
        # default for "no toner". EOL pad leaves init values in place.
        page_pixels = bytearray(b"\xff" * (row_bytes * height))
        pos = _decode_pixels(data, pos, width, height, components, page_pixels)
        pages.append(UrfPage(width=width, height=height, dpi=dpi,
                             bits_per_pixel=bpp, pixels=bytes(page_pixels)))
    return pages


def _decode_pixels(data: bytes, pos: int, width: int, height: int,
                   components: int, out: bytearray) -> int:
    """Decode `height` rows of line-RLE into `out`. Returns updated `pos`."""
    row_bytes = width * components
    y = 0
    while y < height:
        if pos >= len(data):
            raise UrfDecodeError(f"EOF at row {y}/{height}")
        row_repeat = data[pos] + 1
        pos += 1
        row = bytearray(b"\xff" * row_bytes)
        x = 0  # in pixels
        while x < width:
            if pos >= len(data):
                raise UrfDecodeError(f"EOF mid-row at y={y} x={x}")
            ctl = data[pos]
            pos += 1
            if ctl == 0x80:
                # End of row: pad remaining pixels with 0x00 (already zero).
                break
            elif ctl <= 0x7F:
                run = ctl + 1
                if pos + components > len(data):
                    raise UrfDecodeError(f"EOF in repeat at y={y} x={x}")
                pixel = data[pos:pos + components]
                pos += components
                if x + run > width:
                    raise UrfDecodeError(
                        f"row overflow (repeat): y={y} x={x} run={run} width={width}")
                # Write the same pixel `run` times.
                start = x * components
                end = (x + run) * components
                # bytearray * int repeats; pixel * run for components > 1
                row[start:end] = bytes(pixel) * run
                x += run
            else:
                # Literal run of (257 - ctl) pixels.
                run = 257 - ctl
                nbytes = run * components
                if pos + nbytes > len(data):
                    raise UrfDecodeError(f"EOF in literal at y={y} x={x}")
                if x + run > width:
                    raise UrfDecodeError(
                        f"row overflow (literal): y={y} x={x} run={run} width={width}")
                row[x * components:(x + run) * components] = data[pos:pos + nbytes]
                pos += nbytes
                x += run
        # Emit this row `row_repeat` times (clamped to remaining height).
        copies = min(row_repeat, height - y)
        for r in range(copies):
            offset = (y + r) * row_bytes
            out[offset:offset + row_bytes] = row
        y += copies
    return pos


def to_pdf(urf_bytes: bytes) -> bytes:
    """Decode URF and re-encode as a multi-page PDF at the URF's native DPI."""
    from PIL import Image

    pages = decode(urf_bytes)
    if not pages:
        raise UrfDecodeError("no pages")

    images = []
    for p in pages:
        img = Image.frombytes(p.pil_mode, (p.width, p.height), p.pixels)
        images.append(img)

    buf = io.BytesIO()
    dpi = pages[0].dpi
    images[0].save(buf, format="PDF", save_all=True,
                   append_images=images[1:], resolution=float(dpi))
    log.info("decoded URF: %d page(s), first page %dx%d @ %d DPI -> %d-byte PDF",
             len(pages), pages[0].width, pages[0].height, dpi, buf.tell())
    return buf.getvalue()
