"""Normalise every print job to A4 page size.

The Canon's port-9100 PDF interpreter prints PDFs at the page size
declared in the file and warns when that doesn't match the loaded
paper. Since our host always has A4 loaded, we rewrite every page to
be A4-sized:

- Page already A4 (within ~0.7mm tolerance): pass through.
- Page smaller than A4: place on a blank A4 page, centered.
- Page larger than A4: scale down to fit (aspect-ratio-preserving),
  then center on a blank A4 page.

For JPEG / TIFF inputs (rare — iOS prefers PDF when our `pdl` includes
both `application/pdf` and `image/jpeg`), we decode with Pillow, save
as a PDF at a DPI computed to give the right physical size, then run
the result through the same A4 normalisation.

Unknown formats and any transcode failure return the original bytes
unchanged — better to print wrong than to abort the job.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# 1 point = 1/72 inch; A4 = 210 × 297 mm = 595.276 × 841.890 pt
A4_W_PT = 595.0
A4_H_PT = 842.0
TOLERANCE_PT = 2.0          # ~0.7mm
IMG_MARGIN_PT = 36          # 0.5 inch on each side for image-to-PDF wrapping


def transcode_to_a4(data: bytes) -> bytes:
    """Return PDF bytes whose every page is A4. Pass-through on failure."""
    try:
        if data[:4] == b"%PDF":
            return _pdf_to_a4(data)
        if data[:3] == b"\xff\xd8\xff":
            return _image_to_a4(data)
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            return _image_to_a4(data)
    except Exception:
        log.exception("transcode failed; forwarding raw bytes")
    return data


def _pdf_to_a4(pdf_bytes: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter, Transformation

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    rewrote_any = False

    for page in reader.pages:
        src_w = float(page.mediabox.width)
        src_h = float(page.mediabox.height)
        if (abs(src_w - A4_W_PT) < TOLERANCE_PT
                and abs(src_h - A4_H_PT) < TOLERANCE_PT):
            writer.add_page(page)
            continue
        # Scale-to-fit A4 with aspect preserved; never upscale (cap at 1.0).
        scale = min(A4_W_PT / src_w, A4_H_PT / src_h, 1.0)
        target_w = src_w * scale
        target_h = src_h * scale
        tx = (A4_W_PT - target_w) / 2
        ty = (A4_H_PT - target_h) / 2
        new_page = writer.add_blank_page(width=A4_W_PT, height=A4_H_PT)
        new_page.merge_transformed_page(
            page, Transformation().scale(scale).translate(tx, ty))
        rewrote_any = True

    if not rewrote_any:
        return pdf_bytes
    buf = io.BytesIO()
    writer.write(buf)
    log.info("transcoded PDF to A4: %d -> %d bytes",
             len(pdf_bytes), buf.tell())
    return buf.getvalue()


def _image_to_a4(image_bytes: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    iw, ih = img.size
    # Target page size in points: scale-to-fit inside A4 minus margins,
    # preserve aspect ratio, never upscale.
    max_w = A4_W_PT - 2 * IMG_MARGIN_PT
    max_h = A4_H_PT - 2 * IMG_MARGIN_PT
    scale = min(max_w / iw, max_h / ih, 1.0)
    # PIL's PDF page size = pixels / DPI inches = (72 * pixels) / DPI points.
    # We want page width = scale * iw points, so DPI = 72 / scale.
    dpi = 72.0 / scale
    tmp = io.BytesIO()
    img.save(tmp, format="PDF", resolution=dpi)
    log.info("wrapped %dx%d image into PDF page (%.0fx%.0f pt, DPI=%.0f)",
             iw, ih, iw * scale, ih * scale, dpi)
    return _pdf_to_a4(tmp.getvalue())
