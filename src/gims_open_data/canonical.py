"""Content identity, independent of official identifiers and retrieval metadata."""

import hashlib
import json
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Question


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def fingerprint(question: "Question") -> str:
    content = {
        "text": normalize_text(question.text),
        "resources": [r.url for r in question.resources],
        "answers": [
            {"text": normalize_text(a.text), "resources": [r.url for r in a.resources]}
            for a in question.answers
        ],
    }
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MATCH_ALGORITHM = "match-fingerprint-v1"
REVISION_ALGORITHM = "revision-fingerprint-v2"


def digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def match_fingerprint(question: "Question") -> str:
    return digest(
        {
            "algorithm": MATCH_ALGORITHM,
            "text": normalize_text(question.text),
            "resources": [r.url for r in question.resources],
            "answers": [
                {
                    "id": str(a.id),
                    "text": normalize_text(a.text),
                    "resources": [r.url for r in a.resources],
                }
                for a in question.answers
            ],
        }
    )


def revision_fingerprint(question: "Question") -> str:
    return digest(
        {
            "algorithm": REVISION_ALGORITHM,
            "match": match_fingerprint(question),
            "type": question.type,
            "multiple": question.multiple,
            "is_additional": question.is_additional,
            "correct_answer_ids": sorted(str(i) for i in question.correct_answer_ids),
            "correct_flags": [a.correct for a in question.answers],
        }
    )
