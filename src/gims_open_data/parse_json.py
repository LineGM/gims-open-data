import json
from datetime import datetime
from uuid import UUID

from .models import Answer, Counters, Question, Resource, Source


def valid_ids(value: str | list[str]) -> list[UUID]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not value:
        raise ValueError("valid_answers must be a nonempty UUID list or comma-separated string")
    return [UUID(item.strip()) for item in value]


def resources(paths: list[str]) -> list[Resource]:
    if not isinstance(paths, list):
        raise ValueError("resources_path must be a list")
    return [Resource(url=path) for path in paths]


def parse_question_json(raw: bytes | str, retrieved_at: datetime) -> Question:
    data = json.loads(raw)
    current = data["current_question"]
    if not current:
        raise ValueError("server ended marathon before expected question")
    correct = valid_ids(data["valid_answers"])
    answers = [
        Answer(
            id=a["id"],
            text=a["title"],
            resources=resources(a["resources_path"]),
            correct=UUID(a["id"]) in correct,
        )
        for a in data["current_answers"]
    ]
    return Question(
        official_id=current["id"],
        id_status="official",
        position=data["question_number"],
        text=current["content"],
        type=current["type"],
        multiple=current["type"] == "multiple",
        is_additional=current["is_additional"],
        resources=resources(current["resources_path"]),
        answers=answers,
        correct_answer_ids=correct,
        counters=Counters(
            questions_count=data["questions_count"],
            question_number=data["question_number"],
            questions_passed=data["questions_passed"],
            questions_remain=data["questions_count"] - data["questions_passed"],
            questions_statuses=data["questions_statuses"],
        ),
        source=Source(retrieved_at=retrieved_at),
    )
