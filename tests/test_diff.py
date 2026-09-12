import json

import pytest

from gims_open_data.diff import compare
from gims_open_data.models import Question
from gims_open_data.parse_html import parse_initial_html
from gims_open_data.parse_json import parse_question_json

from .test_parsers import NOW


def rebuild(q, **changes):
    data = q.model_dump()
    data.update(changes)
    for key in ("stable_key", "content_fingerprint", "match_fingerprint", "revision_fingerprint"):
        data.pop(key)
    return Question.model_validate(data)


def changes(old, new):
    return compare(old, new, previous_snapshot="before", current_snapshot="after")


def test_added_removed(next_raw):
    q = parse_question_json(next_raw, NOW)
    assert len(changes([], [q])["added"]) == 1
    assert len(changes([q], [])["removed"]) == 1


@pytest.mark.parametrize("change,field", [("text", "text"), ("correct", "correct_answers")])
def test_changed_text_and_correct_revision(next_raw, change, field):
    q = parse_question_json(next_raw, NOW)
    if change == "text":
        new = rebuild(q, text=q.text + " New text")
    else:
        answers = [a.model_dump() for a in q.answers]
        for i, answer in enumerate(answers):
            answer["correct"] = i == 0
        new = rebuild(q, answers=answers, correct_answer_ids=[answers[0]["id"]])
        assert new.match_fingerprint == q.match_fingerprint
    diff = changes([q], [new])
    assert diff["changed"][0]["fields"] == [field]
    assert new.revision_fingerprint != q.revision_fingerprint
    assert new.stable_key == q.stable_key


def test_moved_is_not_content_changed(next_raw):
    q = parse_question_json(next_raw, NOW)
    counters = q.counters.model_dump()
    counters.update(question_number=3, questions_passed=2, questions_remain=1511)
    new = rebuild(q, position=3, counters=counters)
    diff = changes([q], [new])
    assert diff["moved"] == [{"stable_key": q.stable_key, "from": 2, "to": 3}]
    assert not diff["changed"] and new.revision_fingerprint == q.revision_fingerprint


def test_unresolved_stable_matching_and_ambiguity(initial):
    q = parse_initial_html(initial, NOW)
    new = rebuild(q)
    assert q.official_id is None and q.stable_key.startswith("fallback:")
    assert changes([q], [new])["unchanged_count"] == 1
    diff = changes([q, q], [new])
    assert diff["ambiguous_keys"] == [q.stable_key]
    assert diff["unchanged_count"] == 0 and len(diff["removed"]) == 2


def test_unknown_fields_accepted(next_raw):
    data = json.loads(next_raw)
    data["new_field"] = {"anything": 123}
    data["current_question"]["future_field"] = True
    assert parse_question_json(json.dumps(data), NOW) == parse_question_json(next_raw, NOW)


@pytest.mark.parametrize(
    "field",
    ["current_question", "current_answers", "valid_answers", "question_number", "questions_count"],
)
def test_missing_critical_field_rejected(next_raw, field):
    data = json.loads(next_raw)
    del data[field]
    with pytest.raises((KeyError, ValueError)):
        parse_question_json(json.dumps(data), NOW)
