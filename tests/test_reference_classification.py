from __future__ import annotations

from gims_open_data.models import Resource
from gims_open_data.parse_html import parse_initial_html
from gims_open_data.reference_classification import (
    build_canonical_taxonomy,
    classification_normalized,
    classify_questions,
    refine_classification,
)
from gims_open_data.reference_pdf import PdfAnswer

from .test_diff import rebuild
from .test_parsers import NOW
from .test_reference import reference


def test_exact_text_changed_answers_is_topic_usable(initial):
    live = parse_initial_html(initial, NOW)
    changed = [
        PdfAnswer(order=index + 1, text=f"Новая редакция {index}")
        for index, _ in enumerate(live.answers)
    ]
    result = classify_questions([live], [reference(live, answers=changed)])[0]
    assert result["classification_status"] == "exact_topic"
    assert result["classification_usable"]
    assert "exact question text despite answer drift" in result["recovery_reasons"]


def test_classification_normalization_handles_punctuation_and_short_homoglyphs():
    assert classification_normalized("Сигнал A?") == classification_normalized("Сигнал А")
    assert classification_normalized("Текст:  ") == "текст"


def test_exact_text_across_codes_same_topic_is_many_to_many(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [reference(live), reference(live, "Г.2.2.4", 2)]
    result = classify_questions([live], pdf)[0]
    assert result["classification_usable"]
    assert {match["pdf_code"] for match in result["matches"]} == {"М.2.2.3", "Г.2.2.4"}


def test_same_generic_text_with_different_images_is_ambiguous(initial):
    live = rebuild(parse_initial_html(initial, NOW), text="Что показано на рисунке?")
    pdf = [
        reference(live, illustration_refs=[{"sha256": "1" * 64}]),
        reference(live, "П.1.1.1", 2, illustration_refs=[{"sha256": "2" * 64}]),
    ]
    result = classify_questions([live], pdf)[0]
    assert result["classification_status"] == "ambiguous"
    assert not result["classification_usable"]


def test_vessel_type_change_is_not_auto_match(initial):
    live = rebuild(
        parse_initial_html(initial, NOW),
        text="Какого типа движитель установлен на судне особой конструкции?",
    )
    pdf = [reference(live, question_text="Какого типа движитель установлен на гидроцикле?")]
    result = classify_questions([live], pdf)[0]
    assert not result["classification_usable"]
    assert result["semantic_danger_rejections"]


def test_polarity_number_and_direction_changes_are_not_auto_match(initial):
    live = parse_initial_html(initial, NOW)
    for live_text, pdf_text in (
        ("Положительная остойчивость судна", "Отрицательная остойчивость судна"),
        ("На расстоянии 100 метров что делать?", "На расстоянии 200 метров что делать?"),
        ("Что находится слева от судна?", "Что находится справа от судна?"),
    ):
        current = rebuild(live, text=live_text)
        result = classify_questions([current], [reference(current, question_text=pdf_text)])[0]
        assert not result["classification_usable"], (live_text, result)


def test_strict_image_support_resolves_image_variant(initial):
    live = rebuild(
        parse_initial_html(initial, NOW),
        text="Что показано на рисунке?",
        resources=[
            Resource(
                url="https://digital.mchs.gov.ru:85/testing_bucket/live",
                sha256="3" * 64,
            )
        ],
    )
    first = reference(live, "М.2.2.3", 1, illustration_refs=[{"sha256": "1" * 64}])
    second = reference(live, "П.1.1.1", 2, illustration_refs=[{"sha256": "2" * 64}])
    descriptors = {
        "3" * 64: {"pixel_sha256": "same", "aspect": 1.0, "thumbnail": [0], "dhash": 0},
        "1" * 64: {"pixel_sha256": "same", "aspect": 1.0, "thumbnail": [0], "dhash": 0},
        "2" * 64: {"pixel_sha256": "different", "aspect": 1.0, "thumbnail": [255], "dhash": 255},
    }
    result = classify_questions([live], [first, second], descriptors)[0]
    assert result["classification_usable"], result
    assert [match["pdf_code"] for match in result["matches"]] == ["М.2.2.3"]


def test_classification_is_deterministic(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [reference(live), reference(live, "Г.2.2.4", 2)]
    assert classify_questions([live], pdf) == classify_questions([live], pdf)


def test_same_observed_title_across_prefixes_and_punctuation_is_one_topic(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [
        reference(
            live,
            code="М.1.1.1",
            section_code="М.1.1",
            section_title="Observed heading.",
        ),
        reference(
            live,
            code="П.1.1.1",
            order=2,
            prefix="П",
            section_code="П.1.1",
            section_title="Observed heading",
        ),
    ]
    taxonomy = build_canonical_taxonomy(pdf)
    assert len(taxonomy["topics"]) == 1
    assert len(taxonomy["topics"][0]["memberships"]) == 2
    assert {item["prefix"] for item in taxonomy["topics"][0]["memberships"]} == {"М", "П"}


def test_four_branch_consensus_preserves_singleton_source_conflict(initial):
    live = parse_initial_html(initial, NOW)
    main_title = "Вопросы административной ответственности за нарушение правил плавания"
    pdf = [
        reference(
            live,
            code=f"{prefix}.1.3.{index}",
            order=index,
            prefix=prefix,
            section_code=f"{prefix}.1.3",
            section_title=main_title,
        )
        for index, prefix in enumerate(("ВВП", "ВП", "МП", "МТ"), 1)
    ]
    pdf.append(
        reference(
            live,
            code="МП.1.1.150",
            order=5,
            prefix="МП",
            section_code="МП.1.1",
            section_title="Нормативные акты в области мореплавания",
        )
    )
    strict = [
        {
            "live_stable_key": live.stable_key,
            "match_status": "exact",
            "matches": [{"pdf_order": order} for order in range(1, 5)],
            "candidates": [],
        }
    ]
    baseline = classify_questions([live], pdf, strict_records=strict)
    refined = refine_classification(
        baseline,
        pdf,
        strict,
        build_canonical_taxonomy(pdf),
    )[0]
    assert refined["classification_usable"]
    assert refined["canonical_topic_status"] == "source_taxonomy_conflict"
    assert refined["source_taxonomy_conflict"]
    assert refined["primary_canonical_topic"]["display_title"] == main_title
    assert {item["pdf_code"] for item in refined["conflicting_memberships"]} == {"МП.1.1.150"}
    assert {match["pdf_code"] for match in refined["matches"]} == {
        f"{prefix}.1.3.{index}" for index, prefix in enumerate(("ВВП", "ВП", "МП", "МТ"), 1)
    } | {"МП.1.1.150"}


def test_equally_supported_exact_topics_remain_many_to_many(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [
        reference(
            live,
            code=f"{prefix}.1.1.{index}",
            order=index,
            prefix=prefix,
            section_code=f"{prefix}.1.1",
            section_title=title,
        )
        for index, (prefix, title) in enumerate(
            (
                ("М", "Topic one"),
                ("П", "Topic one"),
                ("Г", "Topic two"),
                ("А", "Topic two"),
            ),
            1,
        )
    ]
    strict = [
        {
            "live_stable_key": live.stable_key,
            "match_status": "exact",
            "matches": [{"pdf_order": order} for order in range(1, 5)],
            "candidates": [],
        }
    ]
    refined = refine_classification(
        classify_questions([live], pdf, strict_records=strict),
        pdf,
        strict,
        build_canonical_taxonomy(pdf),
    )[0]
    assert refined["classification_usable"]
    assert refined["canonical_topic_status"] == "multi_topic_confirmed"
    assert refined["primary_canonical_topic"] is None
    assert len(refined["canonical_topic_candidates"]) == 2


def test_fuzzy_candidates_do_not_use_consensus_shortcut(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [
        reference(
            live,
            question_text=f"{live.text} редакция",
            prefix="М",
            section_code="М.1.1",
            section_title="One topic",
        ),
        reference(
            live,
            code="П.1.1.2",
            order=2,
            question_text=f"{live.text} редакция",
            prefix="П",
            section_code="П.1.1",
            section_title="Two topic",
        ),
    ]
    strict = [
        {
            "live_stable_key": live.stable_key,
            "match_status": "exact",
            "matches": [{"pdf_order": 1}, {"pdf_order": 2}],
            "candidates": [],
        }
    ]
    refined = refine_classification(
        classify_questions([live], pdf, strict_records=strict),
        pdf,
        strict,
        build_canonical_taxonomy(pdf),
    )[0]
    assert not refined["refinement_applied"]
    assert "consensus_not_applied_to_fuzzy" in refined["refinement_reasons"]


def test_image_dependent_conflict_blocks_canonical_acceptance(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [
        reference(
            live,
            section_code="М.1.1",
            section_title="Image topic one",
            graphic_refs=[{"kind": "diagram"}],
        ),
        reference(
            live,
            code="П.1.1.2",
            order=2,
            prefix="П",
            section_code="П.1.1",
            section_title="Image topic two",
            graphic_refs=[{"kind": "diagram"}],
        ),
    ]
    refined = refine_classification(
        classify_questions([live], pdf),
        pdf,
        [],
        build_canonical_taxonomy(pdf),
    )[0]
    assert refined["canonical_topic_status"] == "image_version_ambiguous"
    assert not refined["classification_usable"]
