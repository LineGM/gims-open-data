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
