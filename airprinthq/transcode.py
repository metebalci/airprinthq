"""Normalise every print job to A4 page size.

The Canon's port-9100 PDF interpreter prints PDFs at the page size
declared in the file and warns when that doesn't match the loaded
paper. Since our host always has A4 loaded, we rewrite every page to
be A4-sized. The A4 target orientation follows the source page, so a
landscape page lands on landscape A4 instead of being shrunk to fit
portrait width:

- Page already A4 (within ~0.7mm tolerance): pass through.
- Page smaller than A4: place on a blank A4 page, centered, 100% size.
- Page slightly larger than A4 (e.g. Letter): place on a blank A4
  page centered at 100%, accept ~10pt margin-area clip per side.
- Page significantly larger than A4 (e.g. Legal, A3): scale down to
  fit (aspect-ratio-preserving), then center.

URF inputs (Apple Unified Raster Format, what iOS sends for monochrome
multi-copy jobs) are decoded by `airprinthq.urf` into a multi-page PDF,
then run through the same A4 pipeline. URF decoding failures raise —
we never pass raw URF bytes through, because the Canon's port-9100
dispatcher cannot decode URF and would print megabytes of garbage.

Unknown formats and non-URF transcode failures return the original
bytes unchanged — better to print wrong than to abort the job.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# 1 point = 1/72 inch; A4 = 210 × 297 mm = 595.276 × 841.890 pt
A4_W_PT = 595.0
A4_H_PT = 842.0
TOLERANCE_PT = 2.0          # ~0.7mm; pages within this of A4 are "already A4"
# Max allowed clip per side when centering an oversized page on A4 without
# scaling. ~10pt = ~3.5mm — comfortably inside the margin of typical
# Letter/A4 documents. Letter (612×792) is 17pt wider than A4 → 8.5pt per
# side, within tolerance: we center it instead of shrinking by 3%. Legal
# (612×1008) is 166pt taller → 83pt per side, beyond tolerance: fall back
# to scale-to-fit.
MAX_CLIP_PER_SIDE_PT = 10.0


def transcode_to_a4(data: bytes) -> bytes:
    """Return PDF bytes whose every page is A4.

    For URF input, raises on decode failure — we never pass raw URF
    through (Canon can't decode it). For PDF input, pass-through on
    decode failure. For other formats, pass through unchanged.
    """
    # URF must succeed-or-raise. Never pass raw URF bytes downstream.
    if data[:8] == b"UNIRAST\x00":
        from . import urf
        pdf_bytes = urf.to_pdf(data)
        return _pdf_to_a4(pdf_bytes)
    if data[:4] == b"%PDF":
        try:
            return _pdf_to_a4(data)
        except Exception:
            log.exception("PDF transcode failed; forwarding raw bytes")
    return data


def _pdf_to_a4(pdf_bytes: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter, Transformation

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    rewrote_any = False

    for page in reader.pages:
        src_w = float(page.mediabox.width)
        src_h = float(page.mediabox.height)
        # Match A4 orientation to the source page. A landscape page goes
        # onto landscape A4 (842x595); otherwise portrait A4 (595x842).
        # Without this a landscape photo gets shrunk to fit portrait
        # width and prints small with wide top/bottom margins.
        if src_w > src_h:
            a4_w, a4_h = A4_H_PT, A4_W_PT
        else:
            a4_w, a4_h = A4_W_PT, A4_H_PT
        if (abs(src_w - a4_w) < TOLERANCE_PT
                and abs(src_h - a4_h) < TOLERANCE_PT):
            writer.add_page(page)
            continue
        # If the source is "close enough" to A4 (Letter is the canonical
        # example), keep content at 100% and just center on A4, accepting
        # up to ~10pt of margin-area clip per side. Otherwise fall back to
        # scale-to-fit (Legal, A3, etc.).
        w_excess = max(0.0, (src_w - a4_w) / 2)
        h_excess = max(0.0, (src_h - a4_h) / 2)
        if (w_excess <= MAX_CLIP_PER_SIDE_PT
                and h_excess <= MAX_CLIP_PER_SIDE_PT):
            scale = 1.0
        else:
            scale = min(a4_w / src_w, a4_h / src_h, 1.0)
        target_w = src_w * scale
        target_h = src_h * scale
        tx = (a4_w - target_w) / 2
        ty = (a4_h - target_h) / 2
        new_page = writer.add_blank_page(width=a4_w, height=a4_h)
        new_page.merge_transformed_page(
            page, Transformation().scale(scale).translate(tx, ty))
        log.info("page %.0fx%.0f -> A4 %.0fx%.0f scale=%.3f",
                 src_w, src_h, a4_w, a4_h, scale)
        rewrote_any = True

    if not rewrote_any:
        return pdf_bytes
    buf = io.BytesIO()
    writer.write(buf)
    log.info("transcoded PDF to A4: %d -> %d bytes",
             len(pdf_bytes), buf.tell())
    return buf.getvalue()


