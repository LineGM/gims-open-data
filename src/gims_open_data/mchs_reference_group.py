"""Semantic identity for physical rows in the categorized MChS corpus."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any

from .mchs_reference_pdf import PublishedQuestion


def safe_answer_normalized(value: str) -> str:
    """Normalize typography and PDF line wrapping without semantic rewriting."""
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"(?<=\w)-\s*(?:\n\s*)?(?=\w)", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    # Punctuation is only a list/typography separator here. Token order remains.
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def safe_question_normalized(value: str) -> str:
    """Canonical question text with only physical PDF line-wrap repairs."""
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"(?<=\w)-\s*(?:\n\s*)?(?=\w)", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value.rstrip(" .?!;:…")


def _image_dependent(question: PublishedQuestion) -> bool:
    from .reference_match import image_dependent

    if image_dependent(question.question_text, []):
        return True
    answers = [safe_answer_normalized(a) for a in question.published_answers]
    return bool(answers) and all(
        a not in {"да", "нет", "все"}
        and re.fullmatch(r"[а-яa-z0-9]{1,3}(?:[)()., /-]+[а-яa-z0-9]{1,3})*[)()., /-]*", a)
        for a in answers
    )


def _scope(question: PublishedQuestion) -> tuple[str, str, str, str, str]:
    membership = question.memberships[0]
    return (
        question.document_id,
        membership["dimension"],
        membership["category"],
        membership.get("topic_code") or "",
        safe_question_normalized(question.question_text),
    )


def _image_scope(question: PublishedQuestion) -> tuple[str, ...]:
    if not _image_dependent(question):
        return ()
    # Exact content identity is the strictest reproducible visual grouping.
    hashes = tuple(sorted(i["sha256"] for i in question.illustration_refs if i.get("sha256")))
    return hashes or ("no-image",)


def _group_id(key: tuple[Any, ...]) -> str:
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"mchs-ref-group:{digest}"


def build_reference_groups(
    questions: list[PublishedQuestion],
) -> list[dict[str, Any]]:
    """Collapse repeated physical rows while retaining every row as provenance."""
    buckets: dict[tuple[Any, ...], list[PublishedQuestion]] = defaultdict(list)
    for question in questions:
        buckets[_scope(question) + (_image_scope(question),)].append(question)

    groups: list[dict[str, Any]] = []
    for key, rows in sorted(buckets.items(), key=lambda item: item[0]):
        first = rows[0]
        membership_keys = set()
        memberships: list[dict[str, Any]] = []
        for row in rows:
            for membership in row.memberships:
                mkey = membership.get("membership_id") or json.dumps(
                    membership, ensure_ascii=False, sort_keys=True
                )
                if mkey not in membership_keys:
                    membership_keys.add(mkey)
                    memberships.append(membership)
        answers: list[str] = []
        answer_keys: set[str] = set()
        for row in rows:
            if row.correctness_known and row.published_correct_answer is not None:
                row_answers = [row.published_correct_answer]
            elif row.answer_structure == "published_answer_only":
                row_answers = row.published_answers
            else:
                # A full option set is not a set of independently published correct answers.
                row_answers = []
            for answer in row_answers:
                normalized_answer = safe_answer_normalized(answer)
                if normalized_answer not in answer_keys:
                    answer_keys.add(normalized_answer)
                    answers.append(answer)
        group_key = key
        groups.append(
            {
                "group_id": _group_id(group_key),
                "dimension": first.memberships[0]["dimension"],
                "category": first.memberships[0]["category"],
                "topic_code": first.memberships[0].get("topic_code"),
                "topic_title": first.memberships[0].get("topic_title"),
                "document_id": first.document_id,
                "normalized_question_text": safe_question_normalized(first.question_text),
                "question_text": first.question_text,
                "image_dependent": bool(_image_scope(first)),
                "image_refs": [image for row in rows for image in row.illustration_refs],
                "source_rows": [row for row in rows],
                "source_row_ids": [row.reference_id for row in rows],
                "memberships": memberships,
                "published_answers": answers,
                "published_answer_keys": sorted(answer_keys),
                "answer_multiplicity": len(answer_keys),
                "source_multiple_answers": len(answer_keys) > 1,
            }
        )
    return groups


def serializable_group(group: dict[str, Any]) -> dict[str, Any]:
    """Return the audit-facing group record without embedding model objects."""
    return {key: value for key, value in group.items() if key != "source_rows"} | {
        "source_rows": [row.model_dump() for row in group["source_rows"]],
    }
