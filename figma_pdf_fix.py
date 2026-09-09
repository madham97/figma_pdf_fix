#!/usr/bin/env python3
"""
Fix a Figma-exported PDF for ATS parsing without changing its appearance.

Figma exports PDFs with unnamed Type 3 fonts, layer-order text, and a page
size equal to the frame's pixel dimensions. This rebuilds the file as a
"sandwich": the page rasterised as the visual layer, plus a clean, correctly
ordered text layer drawn invisibly on top.

Usage:  python figma_pdf_fix.py input.pdf output.pdf [--dpi 240]

Requires: pdfplumber pypdfium2 pillow reportlab
"""
import argparse, io

import pdfplumber
import pypdfium2 as pdfium
from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from PIL import Image, ImageChops

# reportlab ASCII85-wraps embedded images by default, costing ~25% for nothing.
rl_config.useA85 = 0

FONT = "Helvetica"
LINE_TOL = 3.0      # pt: words within this vertical distance are one line
JPEG_QUALITY = 82
# A resume is flat colour, so an adaptive palette reproduces it with less loss
# than JPEG at roughly half the bytes. Pages with photographs or gradients band
# under palettisation, so fall back to JPEG when the error says we are one.
PALETTE_COLORS = 64
BAND_QUANTILE = 0.999   # ignore a handful of outlier pixels on glyph edges
BAND_LIMIT = 32         # per-channel error tolerated at that quantile

# Figma emits the first glyph of a text block as its own run, leaving a gap
# where no space character exists. Real word breaks always carry a literal
# whitespace glyph, so the two cases separate cleanly; the em limit keeps
# column gutters (which are also space-less) from being glued together.
WHITESPACE = set(" \n\r\t  ")
SPLIT_MAX_EM = 0.6


def extract_lines(page):
    """Words -> visually ordered lines, with Figma's first-glyph splits healed."""
    seps = [c for c in page.chars if c["text"] in WHITESPACE]

    words = []
    for w in page.extract_words(extra_attrs=["size"]):
        t = "".join(ch for ch in w["text"] if ch not in "\n ").strip()
        if t:
            words.append(dict(t=t, x0=w["x0"], x1=w["x1"], top=w["top"],
                              bot=w["bottom"], size=w["size"]))

    words.sort(key=lambda w: (round(w["top"], 1), w["x0"]))

    lines, cur = [], []
    for w in words:
        if cur and abs(w["top"] - cur[0]["top"]) > LINE_TOL:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)

    out, healed = [], 0
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        merged = [ln[0]]
        for w in ln[1:]:
            p = merged[-1]
            gap = w["x0"] - p["x1"]
            spaced = any(abs(c["top"] - p["top"]) < LINE_TOL
                         and p["x1"] - 0.5 <= c["x0"] and c["x1"] <= w["x0"] + 0.5
                         for c in seps)
            if not spaced and 0 <= gap < SPLIT_MAX_EM * p["size"]:
                p.update(t=p["t"] + w["t"], x1=w["x1"],
                         top=min(p["top"], w["top"]), bot=max(p["bot"], w["bot"]))
                healed += 1
            else:
                merged.append(w)
        out.append(merged)

    if healed:
        print(f"  healed {healed} split word(s)")
    return out


def banding(im, pal):
    """Per-channel palettisation error at BAND_QUANTILE, ignoring outliers."""
    hist = ImageChops.difference(im, pal.convert("RGB")).convert("L").histogram()
    cutoff = sum(hist) * BAND_QUANTILE
    run = 0
    for level, count in enumerate(hist):
        run += count
        if run >= cutoff:
            return level
    return 255


def encode(im):
    """Palette PNG when it is faithful, JPEG when the page is photographic."""
    pal = im.convert("P", palette=Image.ADAPTIVE, colors=PALETTE_COLORS)
    buf = io.BytesIO()
    if banding(im, pal) <= BAND_LIMIT:
        pal.save(buf, "PNG", optimize=True)
        kind = f"png/{PALETTE_COLORS}"
    else:
        im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True,
                progressive=True)
        kind = f"jpeg/{JPEG_QUALITY}"
    buf.seek(0)
    return buf, kind


def draw_page(c, page, src_page, pw, ph, dpi):
    sw, sh = page.width, page.height
    sx, sy = pw / sw, ph / sh
    skew = abs((sw / sh) / (pw / ph) - 1) * 100
    print(f"source {sw:.0f}x{sh:.0f}pt -> A4, scale {sx:.4f}"
          + (f"  [WARNING: {skew:.1f}% aspect distortion]" if skew > 1 else ""))

    lines = extract_lines(page)
    print(f"{sum(len(l) for l in lines)} words on {len(lines)} lines recovered")

    bmp = src_page.render(scale=(pw / 72 * dpi) / sw)
    im = bmp.to_pil().convert("RGB")
    buf, kind = encode(im)
    print(f"raster {bmp.width}x{bmp.height}px, {kind}, {len(buf.getvalue())//1024} KB")

    c.drawImage(ImageReader(buf), 0, 0, width=pw, height=ph)

    # Each word is placed at its own measured origin so the invisible text
    # tracks the glyphs underneath it; a run is stretched only to span its own
    # width, keeping the scale near 100% instead of smearing a whole line.
    for ln in lines:
        to = c.beginText()
        to.setTextRenderMode(3)                    # invisible
        for i, w in enumerate(ln):
            size = max(1.0, w["size"] * sy)
            last = i == len(ln) - 1
            to.setFont(FONT, size)
            # pdfplumber measures down from the top; reportlab up from the bottom
            to.setTextOrigin(w["x0"] * sx, ph - w["bot"] * sy + size * 0.18)
            # Scale to the word's own width only. The separator that follows is
            # there so every extractor sees an explicit break rather than having
            # to infer one from a gap; its drawn width is irrelevant because the
            # next word sets its own origin.
            natural = stringWidth(w["t"], FONT, size)
            span = (w["x1"] - w["x0"]) * sx
            to.setHorizScale(100.0 * span / natural if natural > 0 else 100.0)
            to.textOut(w["t"] if last else w["t"] + " ")
        c.drawText(to)

    for a in page.annots:                          # carry over hyperlinks
        if a.get("uri"):
            c.linkURL(a["uri"],
                      (a["x0"] * sx, ph - a["bottom"] * sy,
                       a["x1"] * sx, ph - a["top"] * sy),
                      relative=0, thickness=0)


def build(src, out, dpi=240, meta=None):
    pw, ph = A4
    pdf = pdfplumber.open(src)
    raster = pdfium.PdfDocument(src)
    c = canvas.Canvas(out, pagesize=A4, lang="en-US", pageCompression=1)
    for k, v in (meta or {}).items():
        getattr(c, f"set{k.capitalize()}")(v)

    for i, page in enumerate(pdf.pages):
        print(f"--- page {i + 1}/{len(pdf.pages)} ---")
        draw_page(c, page, raster[i], pw, ph, dpi)
        c.showPage()

    c.save()
    print("written", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--dpi", type=int, default=240)
    ap.add_argument("--title", default="Hammad Aamer - Resume")
    ap.add_argument("--author", default="Hammad Aamer")
    ap.add_argument("--subject", default="Resume")
    a = ap.parse_args()
    build(a.src, a.out, a.dpi,
          meta={"title": a.title, "author": a.author,
                "subject": a.subject, "creator": a.author})
