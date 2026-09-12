from collections import Counter
from datetime import datetime
from typing import Any, Final, Literal, Self
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import fingerprint, match_fingerprint, normalize_text, revision_fingerprint

SIMULATOR_URL: Final = "https://digital.mchs.gov.ru/gims/simulator"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Resource(Model):
    # Keep the original URL string, including port, escaping and query ordering.
    url: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_type: str | None = None
    bytes: int | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "digital.mchs.gov.ru"
            or parsed.port != 85
            or parsed.username is not None
            or parsed.password is not None
            or not path.startswith("/testing_bucket/")
            or path == "/testing_bucket/"
            or "\\" in value
            or any(ord(c) < 32 for c in value)
            or parsed.fragment
            or any(part in {".", ".."} for part in path.split("/"))
            or "%" in path
        ):
            raise ValueError("resource origin/path outside official media allowlist")
        return value


class Source(Model):
    publisher: Literal["МЧС России"] = "МЧС России"
    simulator_url: Literal["https://digital.mchs.gov.ru/gims/simulator"] = SIMULATOR_URL
    retrieved_at: datetime


class Answer(Model):
    id: UUID
    text: str
    resources: list[Resource]
    correct: bool

    @field_validator("text")
    @classmethod
    def normalize(cls, value: str) -> str:
        return normalize_text(value)


class Counters(Model):
    questions_count: int = Field(gt=0)
    question_number: int = Field(gt=0)
    questions_passed: int = Field(ge=0)
    questions_remain: int = Field(ge=0)
    questions_statuses: list[Any] | dict[str, Any] | None = None


class Question(Model):
    schema_version: Literal["1.0"] = "1.0"
    stable_key: str = ""
    match_fingerprint: str = ""
    revision_fingerprint: str = ""
    official_id: UUID | None
    id_status: Literal["official", "unresolved_initial_html"]
    position: int = Field(gt=0)
    text: str
    type: Literal["single", "multiple"]
    multiple: bool
    is_additional: bool | None
    resources: list[Resource]
    answers: list[Answer] = Field(min_length=2)
    correct_answer_ids: list[UUID] = Field(min_length=1)
    counters: Counters
    content_fingerprint: str = ""
    source: Source

    @field_validator("text")
    @classmethod
    def normalize(cls, value: str) -> str:
        return normalize_text(value)

    @model_validator(mode="after")
    def validate_question(self) -> Self:
        if not self.text and not self.resources:
            raise ValueError("question has neither text nor media")
        if self.official_id is None:
            if self.position != 1 or self.id_status != "unresolved_initial_html":
                raise ValueError("only initial HTML may have an unresolved official ID")
        elif self.id_status != "official":
            raise ValueError("known UUID requires official id_status")
        ids = [a.id for a in self.answers]
        correct = self.correct_answer_ids
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate answer UUID")
        if len(correct) != len(set(correct)) or not set(correct).issubset(ids):
            raise ValueError("invalid or duplicate correct answer UUID")
        if any(a.correct != (a.id in correct) for a in self.answers):
            raise ValueError("correct flags disagree with official valid_answers")
        if self.multiple != (self.type == "multiple"):
            raise ValueError("type and multiple flag disagree")
        if (self.multiple and len(correct) < 2) or (not self.multiple and len(correct) != 1):
            raise ValueError("correct answer count disagrees with type/multiple")
        c = self.counters
        if (
            c.question_number != self.position
            or self.position > c.questions_count
            or c.questions_passed != self.position - 1
            or c.questions_remain != c.questions_count - c.questions_passed
        ):
            raise ValueError("inconsistent sequential marathon counters")
        expected = fingerprint(self)
        if self.content_fingerprint and self.content_fingerprint != expected:
            raise ValueError("content fingerprint mismatch")
        self.content_fingerprint = expected
        match = match_fingerprint(self)
        revision = revision_fingerprint(self)
        key = f"official:{self.official_id}" if self.official_id else f"fallback:{match}"
        for name, value in (
            ("stable_key", key),
            ("match_fingerprint", match),
            ("revision_fingerprint", revision),
        ):
            if getattr(self, name) and getattr(self, name) != value:
                raise ValueError(f"{name} mismatch")
            setattr(self, name, value)
        return self


def summarize(questions: list[Question], expected_total: int, *, complete: bool) -> dict[str, Any]:
    ids = Counter(str(q.official_id) for q in questions if q.official_id)
    prints = Counter(q.content_fingerprint for q in questions)
    errors = []
    if [q.position for q in questions] != list(range(1, len(questions) + 1)):
        errors.append("positions are not consecutive from 1")
    if complete and len(questions) != expected_total:
        errors.append("saved count differs from reported questions_count")
    if any(q.counters.questions_count != expected_total for q in questions):
        errors.append("questions_count changed during run")
    duplicate_ids = {key: n for key, n in ids.items() if n > 1}
    if duplicate_ids:
        errors.append("duplicate official question UUIDs")
    if len({q.stable_key for q in questions}) != len(questions):
        errors.append("duplicate stable keys")
    return {
        "positions": len(questions),
        "reported_questions_count": expected_total,
        "total_changed_from_1513": expected_total != 1513,
        "with_official_id": sum(ids.values()),
        "without_official_id": sum(q.official_id is None for q in questions),
        "unique_official_ids": len(ids),
        "single": sum(not q.multiple for q in questions),
        "multiple": sum(q.multiple for q in questions),
        "with_question_media": sum(bool(q.resources) for q in questions),
        "with_answer_media": sum(any(a.resources for a in q.answers) for q in questions),
        "duplicates_by_uuid": duplicate_ids,
        "duplicates_by_fingerprint": {key: n for key, n in prints.items() if n > 1},
        "complete": complete and not errors,
        "validation_errors": errors,
    }
