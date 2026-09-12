"""Bounded console output; full structural observations stay in data/."""

import json
import re
from collections import Counter
from pathlib import Path

import pymupdf

path = Path("data/reference/gims-attestation-question-bank.pdf")
doc = pymupdf.open(path)
fonts = Counter()
codes = Counter()
headings = []
samples = []
empty = []
images = 0
for page in doc:
    d = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    if not page.get_text().strip():
        empty.append(page.number + 1)
    images += len(page.get_image_info())
    for b in d["blocks"]:
        for line in b.get("lines", []):
            spans = line["spans"]
            text = "".join(s["text"] for s in spans).strip()
            for s in spans:
                fonts[(s["font"], round(s["size"], 2), s["flags"], s["color"])] += 1
            for code in re.findall(r"[А-ЯЁA-Z]+\.\d+\.\d+\.\d+", text):
                codes[code.split(".")[0]] += 1
            if (re.match(r"^\d+\.", text) or "Раздел" in text or "Тема" in text) and len(text) > 10:
                headings.append({"page": page.number + 1, "text": text, "bbox": line["bbox"]})
            if page.number in (0, 1, 100, 500, 1051):
                samples.append(
                    {
                        "page": page.number + 1,
                        "bbox": line["bbox"],
                        "spans": [
                            {
                                "text": s["text"],
                                "font": s["font"],
                                "flags": s["flags"],
                                "color": s["color"],
                            }
                            for s in spans
                        ],
                    }
                )
report = {
    "pages": len(doc),
    "metadata": doc.metadata,
    "empty_pages": empty,
    "fonts": [(k, v) for k, v in fonts.most_common()],
    "code_prefix_occurrences": codes,
    "image_placements": images,
    "headings": headings,
    "samples": samples,
}
out = path.parent / "probe.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        {k: v for k, v in report.items() if k not in ("samples", "headings")},
        ensure_ascii=False,
        indent=2,
    )
)
print(json.dumps(samples[:32], ensure_ascii=False, indent=2))

headings = []
fills = Counter()
fill_examples = {}
code_depth = Counter()
sizes = Counter()
for page in doc:
    sizes[tuple(page.rect)] += 1
    for b in page.get_text("blocks"):
        t = b[4].strip()
        if re.match(r"^[А-ЯЁ]+\.\d", t):
            if len(t.splitlines()[0].split()) > 1:
                headings.append((page.number + 1, t))
            else:
                code_depth[len(t.splitlines()[0].split("."))] += 1
    for drawing in page.get_drawings():
        f = drawing["fill"]
        if f and max(f) - min(f) > 0.1:
            key = str(f)
            fills[key] += 1
            fill_examples.setdefault(key, (page.number + 1, tuple(drawing["rect"])))
(path.parent / "headings-probe.json").write_text(
    json.dumps(headings, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("sizes", sizes, "code_depth", code_depth, "fills", fills, "fill_examples", fill_examples)
print("headings", len(headings), json.dumps(headings[:8] + headings[-6:], ensure_ascii=False))
