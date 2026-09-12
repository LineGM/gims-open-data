"""Offline parser for the observed four-column, text-layer GIMS reference bank.

Geometry, not reading order, defines rows. Published codes are never identifiers:
the source PDF hash, page and sequential position disambiguate reused codes.
"""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import Field

from .media import file_hash
from .models import Model
from .storage import atomic_json

CODE = re.compile(r"^[А-ЯЁA-Z]+(?:\.\d+){1,4}\.?$")
HEADING = re.compile(r"^([А-ЯЁA-Z]+(?:\.\d+){1,3})\.?\s+(.+)", re.S)
BULLETS = "\uf0b7•●"
VERSION = "1.0"


def normalized(text: str) -> str:
    """Whitespace/typography only; numbers, negations and punctuation survive."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    replacements: dict[str, str | int | None] = {
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "\u00ad": "",
    }
    text = text.translate(str.maketrans(replacements))
    return " ".join(text.split())


class PdfAnswer(Model):
    order: int
    text: str
    correct_pdf: bool | None = None
    style_evidence: list[dict[str, Any]] = Field(default_factory=list)


class PdfQuestion(Model):
    code: str
    code_normalized: str
    page: int
    order: int
    prefix: str
    section_code: str | None = None
    section_title: str | None = None
    question_text: str = ""
    answers: list[PdfAnswer] = Field(default_factory=list)
    correct_answer_indexes: list[int] | None = None
    illustration_refs: list[dict[str, Any]] = Field(default_factory=list)
    graphic_refs: list[dict[str, Any]] = Field(default_factory=list)
    source: dict[str, Any]
    row_refs: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


def safe_output(path: Path, root: Path) -> None:
    """Derived writes must never target the immutable snapshot tree or state."""
    resolved = path.resolve()
    for name in ("snapshots", "state", "media"):
        protected = (root / name).resolve()
        if resolved == protected or resolved.is_relative_to(protected):
            raise ValueError(f"Reference output overlaps protected {name}")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def page_structure(page: Any) -> dict[str, Any]:
    import pymupdf

    lines = []
    for block in page.get_text(
        "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    )["blocks"]:
        for line in block.get("lines", []):
            groups: list[list[dict[str, Any]]] = []
            for span in line["spans"]:
                if (
                    not groups
                    or abs(span["bbox"][1] - groups[-1][-1]["bbox"][1]) > 2
                    or span["bbox"][0] - groups[-1][-1]["bbox"][2] > 7
                ):
                    groups.append([])
                groups[-1].append(span)
            for spans in groups:
                lines.append(
                    {
                        "text": "".join(s["text"] for s in spans),
                        "bbox": [
                            min(s["bbox"][0] for s in spans),
                            min(s["bbox"][1] for s in spans),
                            max(s["bbox"][2] for s in spans),
                            max(s["bbox"][3] for s in spans),
                        ],
                        "spans": spans,
                    }
                )
    drawings = page.get_drawings()
    # Word emits table borders as thin filled rectangles.
    horizontal = sorted(
        {
            round(d["rect"].y0, 2)
            for d in drawings
            if d["rect"].height <= 1
            and d["rect"].width > 25
            and d["rect"].x0 < 85
            and 90 < d["rect"].x1 < 145
        }
    )
    rows = []
    for top, bottom in zip(horizontal, horizontal[1:], strict=False):
        if bottom - top < 18:
            continue
        middle = (top + bottom) / 2
        vertical = [
            d["rect"]
            for d in drawings
            if d["rect"].width <= 1 and d["rect"].y0 <= middle <= d["rect"].y1
        ]
        columns = []
        for low, high in ((40, 90), (90, 145), (300, 345), (490, 535), (750, 815)):
            choices = [r for r in vertical if low < r.x0 < high]
            if choices:
                columns.append(float(max(choices, key=lambda r: r.height).x0))
        if len(columns) == 5:
            rows.append({"top": top, "bottom": bottom, "columns": columns})
    highlights = [
        list(d["rect"])
        for d in drawings
        if d.get("fill")
        and d["fill"][0] > 0.8
        and d["fill"][1] > 0.8
        and d["fill"][2] < 0.4
        and d["rect"].height > 3
    ]
    graphics = []
    for drawing in drawings:
        rect = drawing["rect"]
        if any(
            r["columns"][2] + 1 < rect.x0 <= rect.x1 < r["columns"][3] - 1
            and r["top"] < rect.y0 <= rect.y1 < r["bottom"]
            for r in rows
        ):
            graphics.append(json.loads(json.dumps(drawing, default=list)))
    return {
        "page": page.number + 1,
        "lines": lines,
        "rows": rows,
        "highlights": highlights,
        "images": [],
        "graphics": graphics,
    }


def inside(line: dict[str, Any], row: dict[str, Any], column: int) -> bool:
    x, y, _, y1 = line["bbox"]
    return bool(
        row["top"] <= (y + y1) / 2 < row["bottom"]
        and row["columns"][column] <= x < row["columns"][column + 1]
    )


def answer_lines(
    lines: list[dict[str, Any]], highlights: list[list[float]], previous: list[PdfAnswer]
) -> list[PdfAnswer]:
    answers = previous
    has_bullets = any(line["text"].strip() and line["text"].strip()[0] in BULLETS for line in lines)
    ys = sorted({round(line["bbox"][1], 2) for line in lines if line["text"].strip()})
    gaps = [b - a for a, b in zip(ys, ys[1:], strict=False) if b - a > 3]
    # Word paragraph spacing is ~0.6pt larger than wrapped-line spacing.
    # With only one spacing cluster each line is a paragraph; retain this inference.
    split_gap = min(gaps) + 0.3 if gaps and max(gaps) - min(gaps) > 0.3 else 0
    last_y: float | None = None
    for line in sorted(lines, key=lambda line: (round(line["bbox"][1] / 4), line["bbox"][0])):
        text = line["text"].strip()
        if not text:
            continue
        bullet = text[0] in BULLETS
        if not has_bullets:
            gap = line["bbox"][1] - last_y if last_y is not None else 0
            if not answers or (last_y is not None and gap > split_gap):
                answers.append(PdfAnswer(order=len(answers) + 1, text=""))
            last_y = line["bbox"][1]
        if bullet:
            answers.append(PdfAnswer(order=len(answers) + 1, text=""))
            text = text[1:].strip()
        if not text:
            continue
        if not answers:
            answers.append(PdfAnswer(order=1, text=""))
        answer = answers[-1]
        answer.text = (answer.text + " " + text).strip()
        x0, y0, x1, y1 = line["bbox"]
        marked = any(
            h[0] <= (x0 + x1) / 2 <= h[2] and h[1] <= (y0 + y1) / 2 <= h[3] for h in highlights
        )
        meaningful = [s for s in line["spans"] if s["text"].strip()]
        bold = bool(meaningful) and all(s["flags"] & 16 for s in meaningful)
        answer.style_evidence.append(
            {
                "bbox": line["bbox"],
                "bold": bold,
                "highlight": marked,
                "boundary_method": "bullet" if has_bullets else "paragraph_spacing",
                "fonts": sorted({s["font"] for s in meaningful}),
                "flags": sorted({s["flags"] for s in meaningful}),
                "colors": sorted({s.get("color", 0) for s in meaningful}),
            }
        )
    return answers


def finish_correct(question: PdfQuestion) -> None:
    marked = []
    for i, answer in enumerate(question.answers):
        evidence = answer.style_evidence
        if evidence and all(e["bold"] or e["highlight"] for e in evidence):
            marked.append(i)
    if len(marked) == 1 and len(question.answers) > 1:
        question.correct_answer_indexes = marked
        for i, answer in enumerate(question.answers):
            answer.correct_pdf = i in marked
    else:
        question.issues.append("correct_style_unknown")


def consume_page(
    structure: dict[str, Any],
    records: list[PdfQuestion],
    headings: list[dict[str, Any]],
    source: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    page = structure["page"]
    lines = structure["lines"]
    # Headings can wrap, and can begin in the middle of a page.
    outside = sorted(
        [
            line
            for line in lines
            if not any(
                r["top"] <= (line["bbox"][1] + line["bbox"][3]) / 2 < r["bottom"]
                for r in structure["rows"]
            )
        ],
        key=lambda line: (line["bbox"][1], line["bbox"][0]),
    )
    active: dict[str, Any] | None = None
    for line in outside:
        text = line["text"].strip()
        match = HEADING.match(text)
        if match:
            active = {
                "code": match[1],
                "heading": line["text"].rstrip(),
                "title": match[2],
                "page": page,
                "y": line["bbox"][1],
            }
            headings.append(active)
        elif active and text and text not in ("№", "No", "Вопрос", "Иллюстрация"):
            if line["bbox"][1] - active["y"] < 30 and "Варианты ответа" not in text:
                active["heading"] += "\n" + line["text"].rstrip()
                active["title"] += " " + text
    for row in structure["rows"]:
        row_text = {
            line["text"].strip()
            for line in lines
            if row["top"] <= (line["bbox"][1] + line["bbox"][3]) / 2 < row["bottom"]
        }
        if {"Вопрос", "Иллюстрация"}.issubset(row_text):
            continue
        code_lines = [line for line in lines if inside(line, row, 0) and line["text"].strip()]
        code = "\n".join(
            line["text"].strip() for line in sorted(code_lines, key=lambda line: line["bbox"][1])
        )
        compact = re.sub(r"\s+", "", code)
        if compact in ("№", "No"):
            continue
        if compact and not CODE.fullmatch(compact):
            issues.append({"page": page, "kind": "invalid_code", "code": code, "row": row})
            continue
        if compact:
            candidates = [h for h in headings if (h["page"], h["y"]) < (page, row["top"])]
            heading = candidates[-1] if candidates else None
            question = PdfQuestion(
                code=code,
                code_normalized=compact,
                page=page,
                order=len(records) + 1,
                prefix=compact.split(".")[0],
                section_code=heading["code"] if heading else None,
                section_title=heading["title"] if heading else None,
                source={**source, "page": page, "code": code},
            )
            records.append(question)
        elif records and records[-1].row_refs[-1]["page"] == page - 1:
            question = records[-1]
            question.issues.append("page_continuation")
        else:
            issues.append({"page": page, "kind": "orphan_row", "row": row})
            continue
        question.row_refs.append({"page": page, **row})
        for graphic in structure.get("graphics", []):
            if row["top"] <= (graphic["rect"][1] + graphic["rect"][3]) / 2 < row["bottom"]:
                question.graphic_refs.append({"page": page, **graphic})
                if "illustration_vector_overlay" not in question.issues:
                    question.issues.append("illustration_vector_overlay")
        question.question_text = " ".join(
            filter(
                None,
                [question.question_text]
                + [
                    line["text"].strip()
                    for line in sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
                    if inside(line, row, 1)
                ],
            )
        )
        question.answers = answer_lines(
            [line for line in lines if inside(line, row, 3)],
            structure["highlights"],
            question.answers,
        )
        if (
            any(
                e["boundary_method"] == "paragraph_spacing"
                for a in question.answers
                for e in a.style_evidence
            )
            and "answer_boundaries_inferred" not in question.issues
        ):
            question.issues.append("answer_boundaries_inferred")
        for im in structure["images"]:
            x0, y0, x1, y1 = im["bbox"]
            if row["top"] <= (y0 + y1) / 2 < row["bottom"]:
                role = (
                    "question"
                    if row["columns"][2] <= (x0 + x1) / 2 < row["columns"][3]
                    else "other"
                )
                question.illustration_refs.append({**im, "role": role})
                if role == "other":
                    question.issues.append("image_outside_illustration_column")


def duplicates(keys: list[str]) -> dict[str, int]:
    counts = Counter(keys)
    return {
        "unique": len(counts),
        "duplicate_groups": sum(v > 1 for v in counts.values()),
        "positions_in_duplicate_groups": sum(v for v in counts.values() if v > 1),
        "excess_positions": sum(v - 1 for v in counts.values()),
    }


def content_key(q: PdfQuestion, *, images: bool = True) -> str:
    return json.dumps(
        [
            normalized(q.question_text),
            [normalized(a.text) for a in q.answers],
            sorted(i["sha256"] for i in q.illustration_refs) if images else None,
        ],
        ensure_ascii=False,
    )


def taxonomy(records: list[PdfQuestion], headings: list[dict[str, Any]]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for q in records:
        node = tree.setdefault(q.prefix, {"code": q.prefix, "title": None, "children": {}})
        parts = (q.section_code or q.prefix).split(".")
        for depth in range(2, len(parts) + 1):
            code = ".".join(parts[:depth])
            node = node["children"].setdefault(
                code, {"code": code, "headings": [], "children": {}, "questions": []}
            )
            node["headings"] = [h for h in headings if h["code"] == code]
        node.setdefault("questions", []).append({"code": q.code, "page": q.page, "order": q.order})
    return {
        "prefixes": tree,
        "heading_occurrences": headings,
        "semantics": "Published headings; unnamed parent nodes are code hierarchy only.",
    }


def parse_pdf(pdf: Path, output: Path, media: Path, *, root: Path = Path("data")) -> dict[str, Any]:
    import pymupdf

    safe_output(output, root)
    safe_output(media, root)
    output.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    source = {"filename": pdf.name, "sha256_of_pdf": file_hash(pdf)}
    records: list[PdfQuestion] = []
    headings: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    cache: dict[int, dict[str, Any]] = {}
    fonts: Counter[str] = Counter()
    empty = []
    observed_images = 0
    with pymupdf.open(pdf) as doc:  # type: ignore[no-untyped-call]
        metadata = dict(doc.metadata)
        pages = len(doc)
        for page in doc:
            structure = page_structure(page)
            if not any(line["text"].strip() for line in structure["lines"]):
                empty.append(page.number + 1)
            elif not structure["rows"]:
                issues.append({"page": page.number + 1, "kind": "text_page_without_supported_rows"})
            for line in structure["lines"]:
                for s in line["spans"]:
                    fonts[json.dumps([s["font"], round(s["size"], 2), s["flags"], s["color"]])] += 1
            for im in page.get_image_info(xrefs=True):
                observed_images += 1
                xref = im["xref"]
                if xref not in cache:
                    extracted = doc.extract_image(xref) if xref else None
                    if not extracted:
                        issues.append(
                            {"page": page.number + 1, "kind": "unextractable_image", "xref": xref}
                        )
                        continue
                    body = extracted["image"]
                    sha = hashlib.sha256(body).hexdigest()
                    target = media / sha
                    if not target.exists():
                        target.write_bytes(body)
                    elif file_hash(target) != sha:
                        raise ValueError("Corrupt reference media object")
                    cache[xref] = {
                        "sha256": sha,
                        "extension": extracted["ext"],
                        "width": extracted["width"],
                        "height": extracted["height"],
                    }
                structure["images"].append(
                    {
                        **cache[xref],
                        "xref": xref,
                        "page": page.number + 1,
                        "bbox": list(im["bbox"]),
                        "has_mask": im["has-mask"],
                    }
                )
            consume_page(structure, records, headings, source, issues)
        if not records:
            raise ValueError("No question rows; unsupported layout or missing text layer")
        for q in records:
            finish_correct(q)
            if not q.question_text or len(q.answers) < 2 or any(not a.text for a in q.answers):
                q.issues.append("incomplete_content")
            if q.section_code and not q.code_normalized.startswith(q.section_code + "."):
                q.issues.append("code_heading_disagreement")
            if "\n" in q.code:
                q.issues.append("wrapped_code")
            for issue in q.issues:
                issues.append({"page": q.page, "code": q.code, "order": q.order, "kind": issue})
    for heading in headings:
        heading["heading_normalized"] = normalized(heading["heading"])
        heading["title_normalized"] = normalized(heading["title"])
    summary = {
        "schema_version": VERSION,
        "source": source,
        "pages": pages,
        "empty_pages": empty,
        "document_metadata": {
            "title": metadata.get("title") or None,
            "publisher": None,
            "publication_date": None,
            "revision_date": None,
            "version": None,
            "raw_pdf_metadata": metadata,
        },
        "question_positions": len(records),
        "unique_codes": len({q.code for q in records}),
        "duplicate_code_groups": sum(n > 1 for n in Counter(q.code for q in records).values()),
        "prefixes": dict(sorted(Counter(q.prefix for q in records).items())),
        "sections": len(
            {".".join(q.section_code.split(".")[:2]) for q in records if q.section_code}
        ),
        "topics": len({h["code"] for h in headings}),
        "heading_occurrences": len(headings),
        "with_images": sum(bool(q.illustration_refs) for q in records),
        "without_images": sum(not q.illustration_refs for q in records),
        "image_placements": sum(len(q.illustration_refs) for q in records),
        "observed_image_placements": observed_images,
        "unassociated_image_placements": observed_images
        - sum(len(q.illustration_refs) for q in records),
        "unique_image_objects": len({i["sha256"] for q in records for i in q.illustration_refs}),
        "with_vector_overlays": sum(bool(q.graphic_refs) for q in records),
        "answer_count_distribution": dict(sorted(Counter(len(q.answers) for q in records).items())),
        "correct_known": sum(q.correct_answer_indexes is not None for q in records),
        "correct_unknown": sum(q.correct_answer_indexes is None for q in records),
        "duplicate_exact_text": duplicates([q.question_text for q in records]),
        "duplicate_text_answers": duplicates([content_key(q, images=False) for q in records]),
        "duplicate_text_answers_images": duplicates([content_key(q) for q in records]),
        "issues_by_kind": dict(Counter(i["kind"] for i in issues)),
        "font_spans": dict(fonts),
    }
    write_jsonl(output / "questions.jsonl", [q.model_dump() for q in records])
    atomic_json(output / "sections.json", taxonomy(records, headings))
    atomic_json(output / "parse-issues.json", {"issues": issues})
    summary["artifacts"] = {
        name: file_hash(output / name)
        for name in ("questions.jsonl", "sections.json", "parse-issues.json")
    }
    atomic_json(output / "summary.json", summary)
    return summary
