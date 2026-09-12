from datetime import datetime
from urllib.parse import urljoin
from uuid import UUID

from bs4 import BeautifulSoup, Tag

from .models import SIMULATOR_URL, Answer, Counters, Question, Resource, Source
from .parse_json import valid_ids


def required(root: BeautifulSoup | Tag, selector: str) -> Tag:
    tag = root.select_one(selector)
    if tag is None:
        raise ValueError(f"missing required HTML element: {selector}")
    return tag


def marathon_form(raw: bytes | str) -> tuple[str, dict[str, str]]:
    soup = BeautifulSoup(
        raw, "html.parser", from_encoding="utf-8" if isinstance(raw, bytes) else None
    )
    form = required(soup, 'form[name="gims_simulator_form"]')
    token = required(form, 'input[name="gims_simulator_form[_token]"]')["value"]
    if not token:
        raise ValueError("empty simulator form token")
    return str(form.get("action", "")), {
        "gims_simulator_form[type]": "marathon",
        "gims_simulator_form[send]": "",
        "gims_simulator_form[_token]": str(token),
    }


def html_resources(root: Tag) -> list[Resource]:
    return [
        Resource(url=urljoin(SIMULATOR_URL, str(tag["src"])))
        for tag in root.select("img[src], video[src], audio[src], source[src]")
    ]


def parse_initial_html(raw: bytes | str, retrieved_at: datetime) -> Question:
    soup = BeautifulSoup(
        raw, "html.parser", from_encoding="utf-8" if isinstance(raw, bytes) else None
    )
    if required(soup, "#instance-id").get("data-mode") != "marathon":
        raise ValueError("initial page is not a marathon")
    form = required(soup, "#step-form")
    correct = valid_ids(str(form["data-valid-answers"]))
    flag = form["data-multiple-answers"]
    if flag not in {"true", "false"}:
        raise ValueError("unknown multiple flag")
    position, total = map(int, required(soup, '[data-name="question-number"]').text.split("/"))
    if position != 1:
        raise ValueError("HTML parser is only valid for the initial question")
    answers = []
    for input_tag in form.select('input[name="answer"], input[name="answer[]"]'):
        answer_id = UUID(str(input_tag["value"]))
        label = required(form, f'label[for="{input_tag["id"]}"]')
        title = label.select_one(".answers-label")
        answers.append(
            Answer(
                id=answer_id,
                text=title.get_text(" ") if title else label.get_text(" "),
                resources=html_resources(label),
                correct=answer_id in correct,
            )
        )
    return Question(
        official_id=None,
        id_status="unresolved_initial_html",
        position=position,
        text=required(soup, '[data-name="current-question-content"]').get_text(" "),
        type="multiple" if flag == "true" else "single",
        multiple=flag == "true",
        is_additional=None,
        resources=html_resources(required(soup, '[data-name="question-container"]')),
        answers=answers,
        correct_answer_ids=correct,
        counters=Counters(
            questions_count=total,
            question_number=position,
            questions_passed=int(required(soup, '[data-name="questions-passed"]').text),
            questions_remain=int(required(soup, '[data-name="questions-remain"]').text),
        ),
        source=Source(retrieved_at=retrieved_at),
    )
