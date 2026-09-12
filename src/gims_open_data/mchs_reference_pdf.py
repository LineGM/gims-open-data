"""Geometry parser for categorized MChS tables; independent of the legacy bank."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .mchs_reference_inventory import VERSION, sha256
from .media import file_hash
from .models import Model
from .reference_pdf import normalized, safe_output, write_jsonl
from .storage import atomic_json

BULLETS = "\uf0b7•●"
LOG = logging.getLogger(__name__)


class PublishedQuestion(Model):
    reference_id: str
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str
    memberships: list[dict[str, Any]] = Field(min_length=1)
    source_derived_memberships: list[dict[str, Any]] = Field(default_factory=list)
    page: int = Field(ge=1)
    order: int = Field(ge=1)
    question_number: str
    raw_question_text: str
    question_text: str
    normalized_question_text: str
    published_answers: list[str]
    raw_published_answers: list[str]
    published_correct_answer: str | None = None
    published_correct_answer_indexes: list[int] | None = None
    correctness_known: bool = False
    answer_structure: Literal["full_options", "published_answer_only", "unknown"] = "unknown"
    correctness_evidence: dict[str, Any]
    illustration_refs: list[dict[str, Any]]
    graphic_refs: list[dict[str, Any]]
    row_refs: list[dict[str, Any]]
    issues: list[str]

    @model_validator(mode="after")
    def consistency(self) -> Self:
        if self.document_id != self.source_pdf_sha256:
            raise ValueError("Document identity mismatch")
        if self.reference_id != f"{self.document_id}:{self.order}":
            raise ValueError("Reference identity mismatch")
        if self.normalized_question_text != normalized(self.question_text):
            raise ValueError("Question normalization mismatch")
        indexes = self.published_correct_answer_indexes
        if self.correctness_known != (indexes is not None):
            raise ValueError("Correctness evidence mismatch")
        if indexes is not None and (
            not indexes
            or len(set(indexes)) != len(indexes)
            or any(i < 0 or i >= len(self.published_answers) for i in indexes)
        ):
            raise ValueError("Invalid correct answer indexes")
        expected = self.published_answers[indexes[0]] if indexes and len(indexes) == 1 else None
        if self.published_correct_answer != expected:
            raise ValueError("Published answer mismatch")
        return self


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def lines_from_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for span in sorted(
        spans, key=lambda s: (s.get("page", 0), round(s["bbox"][1], 1), s["bbox"][0])
    ):
        if (
            groups
            and span.get("page", 0) == groups[-1][0].get("page", 0)
            and abs(span["bbox"][1] - groups[-1][0]["bbox"][1]) <= 2
        ):
            groups[-1].append(span)
        else:
            groups.append([span])
    return [
        {
            "text": "".join(s["text"] for s in sorted(g, key=lambda s: s["bbox"][0])).strip(),
            "spans": g,
            "y": g[0]["bbox"][1],
        }
        for g in groups
    ]


def raw_text(spans: list[dict[str, Any]]) -> str:
    return "\n".join(line["text"] for line in lines_from_spans(spans) if line["text"])


def flowing_text(raw: str) -> str:
    # Keep printed hyphens: joining them would silently change compound vessel terms.
    # Separate line-wrap alternatives, guarded and corroborated, belong to matching.
    return " ".join(raw.split())


def structure(page: Any) -> dict[str, Any]:
    import pymupdf

    spans = [
        s
        for b in page.get_text(
            "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        )["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        if s["text"].strip()
    ]
    for span in spans:
        span["page"] = page.number + 1
    drawings = page.get_drawings()
    rects = [d["rect"] for d in drawings]
    horizontal = sorted(
        {
            round(r.y0, 2)
            for r in rects
            if r.height <= 1 and 20 < r.width < 70 and 20 < r.x0 < 80 and 65 < r.x1 < 125
        }
    )
    rows = []
    for top, bottom in zip(horizontal, horizontal[1:], strict=False):
        if bottom - top < 5:
            continue
        middle = (top + bottom) / 2
        vertical = [r for r in rects if r.width <= 1 and r.y0 <= middle <= r.y1]
        columns = []
        for low, high in ((20, 65), (65, 125), (260, 360), (480, 650), (700, 825)):
            choices = [r for r in vertical if low < r.x0 < high]
            if choices:
                columns.append(float(max(choices, key=lambda r: r.height).x0))
        if len(columns) != 5:
            continue
        cells = [
            [
                s
                for s in spans
                if top - 1 <= (s["bbox"][1] + s["bbox"][3]) / 2 < bottom
                and left <= s["bbox"][0] < right
            ]
            for left, right in zip(columns, columns[1:], strict=False)
        ]
        rows.append({"top": top, "bottom": bottom, "columns": columns, "cells": cells})
    return {"page": page.number + 1, "rows": rows, "spans": spans, "drawings": drawings}


def published_answers(spans: list[dict[str, Any]], legend: bool) -> dict[str, Any]:
    paragraphs: list[list[dict[str, Any]]] = []
    bullet_count = 0
    for line in lines_from_spans(spans):
        is_bullet = any(c in line["text"] for c in BULLETS)
        if is_bullet:
            bullet_count += 1
        if not paragraphs or is_bullet:
            paragraphs.append([])
        paragraphs[-1].extend(line["spans"])
    texts = []
    raws = []
    styles = []
    for paragraph in paragraphs:
        raw = raw_text(paragraph).translate(str.maketrans("", "", BULLETS)).strip()
        text = flowing_text(raw)
        if not text:
            continue
        substantive = [s for s in paragraph if re.search(r"[\w]", s["text"])]
        texts.append(text)
        raws.append(raw)
        styles.append(bool(substantive) and all(s["flags"] & 16 for s in substantive))
    indexes = [i for i, bold in enumerate(styles) if bold]
    known = legend and len(indexes) == 1
    # The observed exceptions have four separately bulleted options, exactly one bold.
    layout = (
        "published_answer_only"
        if len(texts) == 1
        else ("full_options" if len(texts) == 4 and bullet_count == 4 and known else "unknown")
    )
    return {
        "published_answers": texts,
        "raw_published_answers": raws,
        "published_correct_answer": texts[indexes[0]] if known else None,
        "published_correct_answer_indexes": indexes if known else None,
        "correctness_known": known,
        "answer_structure": layout,
        "correctness_evidence": {
            "legend_correct_highlighted": legend,
            "all_substantive_spans_bold": styles,
            "bullet_count": bullet_count,
            "method": "explicit_legend_and_single_bold_answer",
        },
    }


def consume_rows(
    st: dict[str, Any],
    records: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    document_id: str,
    memberships: list[dict[str, Any]],
    legend: bool,
) -> None:
    for row_index, row in enumerate(st["rows"]):
        cells = row["cells"]
        number = raw_text(cells[0]).strip()
        if number in {"No", "№", "Nо", "Nº"} or raw_text(cells[1]).strip() == "Вопрос":
            continue
        numbered = bool(re.fullmatch(r"\d+\.?", number))
        has_content = any(cells) or row.get("images")
        if not numbered:
            if not has_content:
                continue
            previous = records[-1] if records else None
            # Only an unnumbered first data row on the next physical page can continue.
            if (
                not number
                and previous
                and previous["row_refs"][-1]["page"] == st["page"] - 1
                and not any(
                    re.fullmatch(r"\d+\.?", raw_text(r["cells"][0]).strip())
                    for r in st["rows"][:row_index]
                )
            ):
                record = previous
                record["issues"].append("page_continuation")
            else:
                issues.append(
                    {
                        "document_id": document_id,
                        "page": st["page"],
                        "issue": "unrecognized_row",
                        "number": number,
                        "cells": [raw_text(c) for c in cells],
                    }
                )
                continue
        else:
            record = {
                "reference_id": f"{document_id}:{len(records) + 1}",
                "source_pdf_sha256": document_id,
                "document_id": document_id,
                "memberships": memberships,
                "page": st["page"],
                "order": len(records) + 1,
                "question_number": number,
                "raw_question_text": "",
                "illustration_refs": [],
                "graphic_refs": [],
                "row_refs": [],
                "issues": [],
                "_answers": [],
            }
            records.append(record)
        record["raw_question_text"] += ("\n" if record["raw_question_text"] else "") + raw_text(
            cells[1]
        )
        record["_answers"].extend(cells[3])
        record["illustration_refs"].extend(row.get("images", []))
        record["graphic_refs"].extend(row.get("graphics", []))
        record["row_refs"].append(
            {
                "page": st["page"],
                "top": row["top"],
                "bottom": row["bottom"],
                "columns": row["columns"],
            }
        )
        record["_legend"] = legend


def parse_document(
    path: Path, memberships: list[dict[str, Any]], media: Path
) -> tuple[list[PublishedQuestion], dict[str, Any]]:
    import pymupdf

    document_id = file_hash(path)
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    document_images = []
    page_audit = []
    with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
        first = str(pdf[0].get_text())
        heading = re.split(r"\n(?:No|№)\s*\n", first, maxsplit=1)[0].strip()
        legend = "правильный" in first and "выделен" in first.replace("-\n", "").replace("- \n", "")
        extra_memberships = []
        if re.search(r"парусно-моторным судном", heading):
            extra_memberships.append(
                {
                    "label": "парусно-моторным судном",
                    "source": "visible_document_heading",
                    "page": 1,
                    "text": heading,
                    "source_pdf_sha256": document_id,
                }
            )
        for page in pdf:
            st = structure(page)
            assigned_images = 0
            for placement in page.get_image_info(xrefs=True):
                bbox = placement["bbox"]
                xref = placement["xref"]
                if not xref:
                    blocks = [
                        b
                        for b in page.get_text("dict")["blocks"]
                        if b.get("type") == 1
                        and all(
                            abs(x - y) < 0.1
                            for x, y in zip(b["transform"], placement["transform"], strict=True)
                        )
                    ]
                    if len(blocks) != 1:
                        issues.append(
                            {
                                "document_id": document_id,
                                "page": page.number + 1,
                                "issue": "inline_image_unavailable",
                            }
                        )
                        continue
                    extracted = blocks[0]
                else:
                    extracted = pdf.extract_image(xref)
                body = extracted["image"]
                digest = sha256(body)
                (media / digest).write_bytes(body)
                im = {
                    "source_pdf_sha256": document_id,
                    "page": page.number + 1,
                    "xref": xref,
                    "bbox": list(bbox),
                    "sha256": digest,
                    "bytes": len(body),
                    "format": extracted["ext"],
                    "mime": "image/" + ("jpeg" if extracted["ext"] == "jpg" else extracted["ext"]),
                    "width": extracted["width"],
                    "height": extracted["height"],
                    "smask": extracted.get("smask", 0),
                }
                candidates = [
                    r
                    for r in st["rows"]
                    if r["top"] <= (bbox[1] + bbox[3]) / 2 < r["bottom"]
                    and r["columns"][2] <= (bbox[0] + bbox[2]) / 2 < r["columns"][3]
                ]
                if len(candidates) == 1:
                    candidates[0].setdefault("images", []).append(im)
                    assigned_images += 1
                else:
                    document_images.append(im)
            for row in st["rows"]:
                for d in st["drawings"]:
                    rect = d["rect"]
                    if (
                        row["columns"][2] + 1 < rect.x0 <= rect.x1 < row["columns"][3] - 1
                        and row["top"] + 1 < rect.y0 <= rect.y1 < row["bottom"] - 1
                    ):
                        row.setdefault("graphics", []).append(
                            {
                                "page": page.number + 1,
                                "bbox": list(rect),
                                "path": json.loads(json.dumps(d, default=list)),
                            }
                        )
            consume_rows(
                st, records, issues, document_id=document_id, memberships=memberships, legend=legend
            )
            unassigned = [
                s
                for s in st["spans"]
                if re.fullmatch(r"\d+\.", s["text"].strip())
                and s["bbox"][0] < 100
                and s["bbox"][1] > min((r["top"] for r in st["rows"]), default=0)
                and not any(
                    r["top"] - 1 <= (s["bbox"][1] + s["bbox"][3]) / 2 < r["bottom"]
                    and r["columns"][0] <= s["bbox"][0] < r["columns"][1]
                    for r in st["rows"]
                )
            ]
            if unassigned:
                issues.append(
                    {
                        "document_id": document_id,
                        "page": page.number + 1,
                        "issue": "unassigned_number_spans",
                        "spans": unassigned,
                    }
                )
            page_audit.append(
                {
                    "page": page.number + 1,
                    "table_rows": len(st["rows"]),
                    "image_placements": len(page.get_image_info()),
                    "assigned_images": assigned_images,
                }
            )
    for record in records:
        record["source_derived_memberships"] = extra_memberships
        record.update(published_answers(record.pop("_answers"), record.pop("_legend")))
        record["question_text"] = flowing_text(record["raw_question_text"])
        record["normalized_question_text"] = normalized(record["question_text"])
        if not record["question_text"] or not record["published_answers"]:
            record["issues"].append("incomplete_content")
        if not record["correctness_known"]:
            record["issues"].append("unknown_correctness")
        if "\ufffd" in record["question_text"]:
            record["issues"].append("text_layer_replacement_character")
    numbers = [int(r["question_number"].rstrip(".")) for r in records]
    if numbers != list(range(1, len(numbers) + 1)):
        issues.append(
            {
                "document_id": document_id,
                "issue": "nonsequential_source_numbers",
                "numbers": numbers,
            }
        )
    return [PublishedQuestion.model_validate(r) for r in records], {
        "document_id": document_id,
        "memberships": memberships,
        "internal_heading": heading,
        "source_derived_memberships": extra_memberships,
        "document_images": document_images,
        "question_count": len(records),
        "issues": issues,
        "pages": page_audit,
    }


def parse(root: Path) -> dict[str, Any]:
    from .mchs_reference_verify import verify_inventory

    corpus = root / "reference/mchs-categorized"
    verify_inventory(root)
    output = corpus / "parsed"
    safe_output(output, root)
    media = output / "media/objects"
    media.mkdir(parents=True, exist_ok=True)
    documents = read_records(corpus / "documents.jsonl")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in documents:
        groups[d["document_id"]].append(d)
    questions = []
    topics = []
    for i, (sha, memberships) in enumerate(sorted(groups.items()), 1):
        LOG.info("Parse PDF %s/%s", i, len(groups))
        qs, topic = parse_document(corpus / f"pdf/objects/{sha}.pdf", memberships, media)
        questions.extend(qs)
        topics.append(topic)
    summary = parse_metrics(questions, topics)
    summary.update(
        {
            "schema_version": VERSION,
            "inventory_manifest_sha256": file_hash(corpus / "manifest.json"),
        }
    )
    write_jsonl(output / "questions.jsonl", [q.model_dump() for q in questions])
    atomic_json(output / "topics.json", {"documents": topics})
    atomic_json(
        output / "parse-issues.json",
        {
            "issues": [e for t in topics for e in t["issues"]]
            + [{"reference_id": q.reference_id, "issues": q.issues} for q in questions if q.issues]
        },
    )
    summary["artifacts"] = {
        n: file_hash(output / n) for n in ("questions.jsonl", "topics.json", "parse-issues.json")
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_metrics(
    questions: list[PublishedQuestion], topics: list[dict[str, Any]]
) -> dict[str, Any]:
    counts = Counter(q.normalized_question_text for q in questions)
    text_groups: dict[str, list[PublishedQuestion]] = defaultdict(list)
    for question in questions:
        text_groups[question.normalized_question_text].append(question)
    content_counts = Counter(
        (
            q.normalized_question_text,
            tuple(q.published_answers),
            tuple(im["sha256"] for im in q.illustration_refs),
        )
        for q in questions
    )
    return {
        "parsed_pdf_count": len(topics),
        "question_positions": len(questions),
        "unique_normalized_question_texts": len(counts),
        "unique_source_question_texts": len({q.raw_question_text for q in questions}),
        "repeated_normalized_text_groups": sum(n > 1 for n in counts.values()),
        "duplicate_text_excess_positions": sum(n - 1 for n in counts.values()),
        "duplicate_text_groups_across_categories": sum(
            len({m["category"] for q in group for m in q.memberships}) > 1
            for group in text_groups.values()
        ),
        "duplicate_text_groups_across_topics": sum(
            len(
                {
                    (m["dimension"], m["category"], m["topic_code"])
                    for q in group
                    for m in q.memberships
                }
            )
            > 1
            for group in text_groups.values()
        ),
        "exact_text_answer_image_duplicate_groups": sum(n > 1 for n in content_counts.values()),
        "exact_text_answer_image_duplicate_excess": sum(n - 1 for n in content_counts.values()),
        "questions_with_images": sum(bool(q.illustration_refs) for q in questions),
        "unique_question_image_objects": len(
            {im["sha256"] for q in questions for im in q.illustration_refs}
        ),
        "unique_image_objects": len(
            {im["sha256"] for q in questions for im in q.illustration_refs}
            | {im["sha256"] for t in topics for im in t["document_images"]}
        ),
        "answer_structure": dict(Counter(q.answer_structure for q in questions)),
        "known_published_correct_answer": sum(q.correctness_known for q in questions),
        "question_issue_counts": dict(Counter(i for q in questions for i in q.issues)),
        "document_issue_counts": dict(Counter(i["issue"] for t in topics for i in t["issues"])),
        "questions_with_graphics": sum(bool(q.graphic_refs) for q in questions),
    }
