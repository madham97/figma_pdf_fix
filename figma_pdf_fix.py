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
import argparse, io, re, sys

import pdfplumber
import pypdfium2 as pdfium
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# Figma renders the first glyph of a text block as its own run with a wider
# advance, so extraction splits it off. The gap is LARGER than a real space,
# so no threshold works — these have to be listed explicitly.
REPAIRS = [
    (r"\bB uilt\b", "Built"),        (r"\bD eveloped\b", "Developed"),
    (r"\bTw ice\b", "Twice"),        (r"\bo f\b", "of"),
    (r"\bC ut\b", "Cut"),            (r"\bT rained\b", "Trained"),
    (r"\bI mplemented\b", "Implemented"), (r"\bM ember\b", "Member"),
    (r"\bA utomated\b", "Automated"), (r"\bL ifted\b", "Lifted"),
    (r"\bDe signed\b", "Designed"),  (r"\bImp roved\b", "Improved"),
]

LINE_TOL = 3.0      # pt: words within this vertical distance are one line
JPEG_QUALITY = 82


def extract_lines(page):
    """Words -> visually ordered lines, with repairs applied."""
    words = []
    for w in page.extract_words():
        t = w["text"].replace("\n", "").replace("\u2028", "").strip()
        if t:
            words.append(dict(t=t, x0=w["x0"], x1=w["x1"],
                              top=w["top"], bot=w["bottom"]))

    words.sort(key=lambda w: (round(w["top"], 1), w["x0"]))

    lines, cur = [], []
    for w in words:
        if cur and abs(w["top"] - cur[0]["top"]) > LINE_TOL:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)

    out = []
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        text = " ".join(w["t"] for w in ln)
        for pat, rep in REPAIRS:
            text = re.sub(pat, rep, text)
        out.append((text,
                    min(w["x0"] for w in ln), max(w["x1"] for w in ln),
                    min(w["top"] for w in ln), max(w["bot"] for w in ln)))
    return out


def build(src, out, dpi=240, meta=None):
    pw, ph = A4
    page = pdfplumber.open(src).pages[0]
    sw, sh = page.width, page.height
    sx, sy = pw / sw, ph / sh
    print(f"source {sw:.0f}x{sh:.0f}pt -> A4, scale {sx:.4f}")

    lines = extract_lines(page)
    print(f"{len(lines)} text lines recovered")

    # rasterise the visual layer
    bmp = pdfium.PdfDocument(src)[0].render(scale=(pw / 72 * dpi) / sw)
    buf = io.BytesIO()
    bmp.to_pil().convert("RGB").save(
        buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    buf.seek(0)
    print(f"raster {bmp.width}x{bmp.height}px, {len(buf.getvalue())//1024} KB")

    c = canvas.Canvas(out, pagesize=A4)
    for k, v in (meta or {}).items():
        getattr(c, f"set{k.capitalize()}")(v)

    c.drawImage(ImageReader(buf), 0, 0, width=pw, height=ph)

    for text, x0, x1, top, bot in lines:
        size = max(4.0, (bot - top) * sy * 0.78)
        to = c.beginText()
        to.setTextRenderMode(3)                    # invisible
        to.setFont("Helvetica", size)
        # pdfplumber measures down from the top; reportlab up from the bottom
        to.setTextOrigin(x0 * sx, ph - bot * sy + size * 0.18)
        actual = c.stringWidth(text, "Helvetica", size)
        if actual > 0:                             # match the visual run width
            to.setHorizScale(100.0 * (x1 - x0) * sx / actual)
        to.textLine(text)
        c.drawText(to)

    for a in page.annots:                          # carry over hyperlinks
        if a.get("uri"):
            c.linkURL(a["uri"],
                      (a["x0"] * sx, ph - a["bottom"] * sy,
                       a["x1"] * sx, ph - a["top"] * sy),
                      relative=0, thickness=0)

    c.showPage()
    c.save()
    print("written", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--dpi", type=int, default=240)
    a = ap.parse_args()
    build(a.src, a.out, a.dpi,
          meta={"title": "Hammad Aamer - Resume", "author": "Hammad Aamer"})
