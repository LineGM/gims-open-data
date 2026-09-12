"""Independent bounded coverage and style audit. Never prints the entire PDF."""

import json
import re
from collections import Counter
from pathlib import Path

import pymupdf

from gims_open_data.reference_pdf import page_structure

root = Path("data/reference")
records = [
    json.loads(line)
    for line in (root / "parsed/questions.jsonl").read_text(encoding="utf-8").splitlines()
]
row_refs = {(ref["page"], ref["top"], ref["bottom"]) for q in records for ref in q["row_refs"]}
illustration_text = []
unassigned = []
nonborder_graphics = []
with pymupdf.open(root / "gims-attestation-question-bank.pdf") as doc:
    for page in doc:
        structure = page_structure(page)
        for line in structure["lines"]:
            t = line["text"].strip()
            if not t:
                continue
            x0, y0, x1, y1 = line["bbox"]
            rows = [r for r in structure["rows"] if r["top"] <= (y0 + y1) / 2 < r["bottom"]]
            if rows and rows[0]["columns"][2] <= x0 < rows[0]["columns"][3]:
                illustration_text.append({"page": page.number + 1, "text": t, "bbox": line["bbox"]})
            if (
                rows
                and (page.number + 1, rows[0]["top"], rows[0]["bottom"]) not in row_refs
                and t not in ("№", "No", "Вопрос", "Иллюстрация")
                and "Варианты ответа" not in t
            ):
                unassigned.append({"page": page.number + 1, "text": t, "bbox": line["bbox"]})
        for drawing in page.get_drawings():
            rect = drawing["rect"]
            if (
                325 < rect.x0 < 500
                and 325 < rect.x1 < 505
                and drawing["fill"]
                and max(drawing["fill"]) - min(drawing["fill"]) > 0.1
            ):
                nonborder_graphics.append(
                    {"page": page.number + 1, "rect": list(rect), "fill": drawing["fill"]}
                )
summary = json.loads((root / "parsed/summary.json").read_text(encoding="utf-8"))
report = {
    "parsed_positions": len(records),
    "covered_row_fragments": len(row_refs),
    "illustration_text_lines": len(illustration_text),
    "unassigned_row_text": unassigned,
    "colored_graphics": nonborder_graphics,
    "style_validation": {
        "confident_pdf_correct": summary["correct_known"],
        "available_for_live_agreement": 0,
        "result": "No distinguishable answer style; no labels inferred from live.",
    },
    "code_irregularities": [
        {"code": q["code"], "page": q["page"]}
        for q in records
        if not re.fullmatch(r"[А-ЯЁ]+(?:\.\d+){2,3}", q["code"])
    ],
    "incomplete_records": [
        {
            "code": q["code"],
            "page": q["page"],
            "text": q["question_text"],
            "answers": [a["text"] for a in q["answers"]],
        }
        for q in records
        if "incomplete_content" in q["issues"]
    ],
    "duplicate_code_groups": sum(n > 1 for n in Counter(q["code"] for q in records).values()),
}
(root / "parse-audit.json").write_text(
    json.dumps({**report, "illustration_text": illustration_text}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(json.dumps(report, ensure_ascii=False, indent=2))
