"""Offline layout census; detailed evidence stays in the ignored corpus namespace."""

import json
from collections import Counter
from pathlib import Path

import pymupdf

from gims_open_data.storage import atomic_json


def main():
    root = Path("data/reference/mchs-categorized")
    documents = [
        json.loads(s) for s in (root / "documents.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    census = []
    for source in documents:
        with pymupdf.open(root / f"pdf/objects/{source['document_id']}.pdf") as pdf:
            counts = Counter()
            styles = Counter()
            pages = []
            for page in pdf:
                spans = [
                    s
                    for b in page.get_text(
                        "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
                    )["blocks"]
                    for line in b.get("lines", [])
                    for s in line["spans"]
                    if s["text"].strip()
                ]
                counts["pages"] += 1
                counts["image_placements"] += len(page.get_image_info())
                for span in spans:
                    if span["bbox"][0] > 590:
                        styles[f"{span['font']}:{span['flags']}"] += 1
                rules = [d["rect"] for d in page.get_drawings()]
                horizontal = sorted(
                    {
                        round(r.y0, 2)
                        for r in rules
                        if r.height <= 1 and r.width > 25 and 40 < r.x0 < 85 and 85 < r.x1 < 125
                    }
                )
                pages.append(
                    {
                        "page": page.number + 1,
                        "size": list(page.rect),
                        "first_column_boundaries": horizontal,
                        "vertical_x": sorted(
                            {round(r.x0, 2) for r in rules if r.width <= 1 and r.height > 10}
                        ),
                        "number_spans": [s for s in spans if s["bbox"][0] < 95],
                        "empty_text": not bool(spans),
                    }
                )
            census.append(
                {
                    "category": source["category"],
                    "topic": source["topic_code"],
                    "sha256": source["document_id"],
                    "counts": dict(counts),
                    "right_column_styles": dict(styles),
                    "pages": pages,
                }
            )
    atomic_json(root / "layout-probe.json", {"documents": census})
    print(
        json.dumps(
            {
                "documents": len(census),
                "pages": sum(d["counts"]["pages"] for d in census),
                "empty_pages": [
                    [d["category"], d["topic"], p["page"]]
                    for d in census
                    for p in d["pages"]
                    if p["empty_text"]
                ],
                "no_rules": [
                    [d["category"], d["topic"], p["page"]]
                    for d in census
                    for p in d["pages"]
                    if not p["first_column_boundaries"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
