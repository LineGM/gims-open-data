import json
import re
from collections import Counter
from pathlib import Path

import pymupdf

d = pymupdf.open("data/reference/gims-attestation-question-bank.pdf")
layouts = Counter()
anomalies = []
codes = []
headings = []
for p in d:
    verticals = Counter(
        round(x["rect"].x0, 1)
        for x in p.get_drawings()
        if x["rect"].width < 1 and x["rect"].height > 20
    )
    layouts[tuple(sorted(verticals))] += 1
    for b in p.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)[
        "blocks"
    ]:
        for line in b.get("lines", []):
            t = "".join(s["text"] for s in line["spans"]).strip()
            if re.match(r"^[А-ЯЁ]+\.\d", t):
                if re.fullmatch(r"[А-ЯЁ]+(?:\.\d+){2,3}", t):
                    codes.append(t)
                else:
                    anomalies.append((p.number + 1, t, list(line["bbox"])))
            if (
                t
                and line["bbox"][0] < 115
                and line["bbox"][1] > 65
                and not re.fullmatch(r"[А-ЯЁ]+(?:\.\d+){2,3}", t)
            ):
                headings.append((p.number + 1, t, list(line["bbox"])))
out = {
    "layouts": layouts.most_common(),
    "codes": len(codes),
    "duplicates": [(k, v) for k, v in Counter(codes).items() if v > 1],
    "anomalies": anomalies,
    "left_column_exceptions": headings,
}
Path("data/reference/layout-probe.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        {k: v for k, v in out.items() if k not in ("anomalies", "left_column_exceptions")},
        ensure_ascii=False,
    )
)
print("left_column_exceptions", json.dumps(headings[:35], ensure_ascii=False))
