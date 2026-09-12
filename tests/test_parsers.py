import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from gims_open_data.canonical import fingerprint, normalize_text
from gims_open_data.models import Question, summarize
from gims_open_data.parse_html import marathon_form, parse_initial_html
from gims_open_data.parse_json import parse_question_json, valid_ids

from .conftest import FIXTURES

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def test_initial_html_and_correct_mapping(initial):
    q = parse_initial_html(initial, NOW)
    assert (q.position, q.counters.questions_count) == (1, 1513)
    assert q.official_id is None and q.id_status == "unresolved_initial_html"
    assert q.is_additional is None
    assert q.text == "В каком из перечисленных случаев будет наилучшая управляемость судна?"
    assert len(q.answers) == 4
    assert not q.multiple and q.type == "single"
    assert str(q.correct_answer_ids[0]) == "95902c7d-14ea-4950-a64c-044dece028a4"
    assert [a.text for a in q.answers if a.correct] == [
        "При придании судну небольшого дифферента на корму"
    ]
    assert q.resources[0].url.encode() in initial
    assert ":85/testing_bucket/" in q.resources[0].url


@pytest.mark.parametrize(
    "filename", ["marathon-next-question.json", "marathon-third-question.json"]
)
def test_json_uuid_resources_and_correct_mapping(filename):
    raw = (FIXTURES / filename).read_bytes()
    data = json.loads(raw)
    q = parse_question_json(raw, NOW)
    assert str(q.official_id) == data["current_question"]["id"]
    assert [str(a.id) for a in q.answers] == [a["id"] for a in data["current_answers"]]
    assert [r.url for r in q.resources] == data["current_question"]["resources_path"]
    assert [str(a.id) for a in q.answers if a.correct] == data["valid_answers"].split(",")
    assert q.counters.questions_passed == q.position - 1
    assert q.source.publisher == "МЧС России"
    assert "INSTANCE_A" not in q.model_dump_json()
    assert q == Question.model_validate_json(q.model_dump_json())


@pytest.mark.parametrize("as_list", [False, True])
def test_synthetic_multiple_and_all_resources(next_raw, as_list):
    # Synthetic coverage, not an observed multiple fixture.
    data = json.loads(next_raw)
    ids = [a["id"] for a in data["current_answers"][:2]]
    data["valid_answers"] = ids if as_list else ", ".join(ids)
    data["current_question"]["type"] = "multiple"
    urls = [
        "https://digital.mchs.gov.ru:85/testing_bucket/a.png?b=2&a=1",
        "https://digital.mchs.gov.ru:85/testing_bucket/b.png",
    ]
    data["current_question"]["resources_path"] = urls
    data["current_answers"][0]["resources_path"] = urls
    q = parse_question_json(json.dumps(data), NOW)
    assert q.multiple
    assert [str(a.id) for a in q.answers if a.correct] == ids
    assert [r.url for r in q.resources] == urls
    assert [r.url for r in q.answers[0].resources] == urls


def test_initial_multiple_and_answer_media(initial):
    raw = initial.decode().replace('data-multiple-answers="false"', 'data-multiple-answers="true"')
    raw = raw.replace(
        'data-valid-answers="95902c7d-14ea-4950-a64c-044dece028a4"',
        'data-valid-answers="95902c7d-14ea-4950-a64c-044dece028a4,'
        'a72c5c50-9338-4e46-887c-bd5001ce2f14"',
    )
    raw = raw.replace('name="answer"', 'name="answer[]"').replace('type="radio"', 'type="checkbox"')
    raw = raw.replace(
        '<span class="answers-label">',
        '<img src="https://digital.mchs.gov.ru:85/testing_bucket/answer.png">'
        '<span class="answers-label">',
        1,
    )
    q = parse_initial_html(raw, NOW)
    assert q.multiple and len(q.correct_answer_ids) == 2
    assert len(q.answers[0].resources) == 1


def test_fingerprint_stability_normalization_and_order(next_raw):
    q = parse_question_json(next_raw, NOW)
    original = q.content_fingerprint
    assert normalize_text("  е\u0308\t\nА\u00a0 Б  ") == "ё А Б"
    q.text = " \n" + q.text.replace(" ", "\u00a0\t") + " "
    q.position = 99
    q.official_id = UUID("00000000-0000-0000-0000-000000000001")
    assert fingerprint(q) == original
    q.answers.reverse()
    assert fingerprint(q) != original


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_answer",
        "missing_correct",
        "empty_correct",
        "single_two_correct",
        "multiple_one_correct",
        "bad_counter",
        "one_answer",
        "bad_uuid",
        "unknown_type",
    ],
)
def test_invalid_json_rejected(next_raw, mutation):
    data = json.loads(next_raw)
    if mutation == "duplicate_answer":
        data["current_answers"].append(data["current_answers"][0])
    elif mutation == "missing_correct":
        data["valid_answers"] = "00000000-0000-0000-0000-000000000001"
    elif mutation == "empty_correct":
        data["valid_answers"] = []
    elif mutation == "single_two_correct":
        data["valid_answers"] = [a["id"] for a in data["current_answers"][:2]]
    elif mutation == "multiple_one_correct":
        data["current_question"]["type"] = "multiple"
    elif mutation == "bad_counter":
        data["questions_passed"] = 99
    elif mutation == "one_answer":
        data["current_answers"] = data["current_answers"][-1:]
    elif mutation == "bad_uuid":
        data["current_question"]["id"] = "invented"
    else:
        data["current_question"]["type"] = "unknown"
    with pytest.raises((ValueError, ValidationError)):
        parse_question_json(json.dumps(data), NOW)


def test_summary_completeness_and_duplicates(initial, next_raw):
    questions = [parse_initial_html(initial, NOW), parse_question_json(next_raw, NOW)]
    summary = summarize(questions, 1513, complete=False)
    assert not summary["validation_errors"]
    assert summary["without_official_id"] == 1
    assert summarize(questions, 1513, complete=True)["validation_errors"]
    duplicate = summarize(questions + [questions[1]], 1513, complete=False)
    assert duplicate["duplicates_by_uuid"] and duplicate["duplicates_by_fingerprint"]
    assert "positions are not consecutive from 1" in duplicate["validation_errors"]


def test_csrf_form_and_no_secrets_in_normalized(initial, next_raw):
    _, form = marathon_form((FIXTURES / "simulator-form.html").read_bytes())
    assert form["gims_simulator_form[type]"] == "marathon"
    assert len(form) == 3
    generated = parse_initial_html(initial, NOW).model_dump_json()
    generated += parse_question_json(next_raw, NOW).model_dump_json()
    for marker in ["INSTANCE_A", "PHPSESSID", "REDACTED_CSRF_TOKEN", "ACTIVATION_A"]:
        assert marker not in generated


def test_valid_answers_never_falls_back_to_singular(next_raw):
    data = json.loads(next_raw)
    del data["valid_answers"]
    with pytest.raises(KeyError):
        parse_question_json(json.dumps(data), NOW)
    with pytest.raises(ValueError):
        valid_ids(123)
