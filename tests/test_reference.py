"""Small structured fixtures and a generated two-page PDF; strictly offline."""

import json

import pymupdf
import pytest

from gims_open_data.__main__ import main
from gims_open_data.parse_html import parse_initial_html
from gims_open_data.reference_match import image_descriptor, match_questions, media_evidence
from gims_open_data.reference_pdf import (
    CODE,
    HEADING,
    PdfAnswer,
    PdfQuestion,
    answer_lines,
    consume_page,
    content_key,
    finish_correct,
    parse_pdf,
    safe_output,
)

from .test_diff import rebuild
from .test_parsers import NOW
from .test_sync import PNG, successful_sync


def line(text, x, y, *, bold=False):
    bbox = [x, y, x + len(text) * 4, y + 11]
    return {
        "text": text,
        "bbox": bbox,
        "spans": [
            {"text": text, "bbox": bbox, "font": "Test", "flags": 16 if bold else 0, "size": 11.04}
        ],
    }


def row(top=70, bottom=220):
    return {"top": top, "bottom": bottom, "columns": [50, 110, 320, 510, 790]}


def structure(page, lines, rows, images=None, highlights=None):
    return {
        "page": page,
        "lines": lines,
        "rows": rows,
        "images": images or [],
        "highlights": highlights or [],
    }


def reference(live, code="М.2.2.3", order=1, **changes):
    values = dict(
        code=code,
        code_normalized=code,
        page=order,
        order=order,
        prefix=code.split(".")[0],
        section_code="М.2.2",
        section_title="Published heading",
        question_text=live.text,
        answers=[PdfAnswer(order=i + 1, text=a.text) for i, a in enumerate(live.answers)],
        source={"filename": "test.pdf", "sha256_of_pdf": "a" * 64, "page": order, "code": code},
    )
    values.update(changes)
    return PdfQuestion(**values)


@pytest.mark.parametrize("code", ["ВВП.1.1.1", "ВВП.3.82", "М.423", "Г.3.1.", "П.2.1.79."])
def test_published_code_grammar(code):
    assert CODE.fullmatch(code)


@pytest.mark.parametrize(
    "text", ["Г.1.3 Уход за судовым двигателем.", "ВВП.3. Основы навигации и радиосвязи"]
)
def test_heading_grammar(text):
    assert HEADING.fullmatch(text)


def test_geometry_multiline_continuation_and_images():
    records, headings, issues = [], [], []
    source = {"filename": "test.pdf", "sha256_of_pdf": "a" * 64}
    page1 = structure(
        1,
        [
            line("М.1.1. Published heading", 60, 40),
            line("М.1.1.1", 60, 120),
            line("First question", 120, 100),
            line("•", 530, 110),
            line("Wrapped answer", 548, 112),
            line("continues here", 548, 125),
            line("•", 530, 150),
            line("Second answer", 548, 152),
            line("М.1.1.2", 60, 270),
            line("Second question", 120, 250),
            line("•", 530, 250),
            line("Answer starts", 548, 252),
        ],
        [row(), row(220, 350)],
        [
            {"sha256": "1" * 64, "bbox": [325, 90, 500, 200], "page": 1},
            {"sha256": "2" * 64, "bbox": [325, 225, 500, 345], "page": 1},
        ],
    )
    consume_page(page1, records, headings, source, issues)
    page2 = structure(
        2,
        [line("and continues", 548, 45), line("•", 530, 65), line("Other answer", 548, 67)],
        [row(35, 160)],
    )
    consume_page(page2, records, headings, source, issues)
    assert not issues
    assert len(records) == 2
    assert records[0].answers[0].text == "Wrapped answer continues here"
    assert records[1].answers[0].text == "Answer starts and continues"
    assert len(records[1].row_refs) == 2
    assert records[0].illustration_refs[0]["sha256"] == "1" * 64
    assert records[1].illustration_refs[0]["sha256"] == "2" * 64
    assert records[0].section_title == "Published heading"


@pytest.mark.parametrize("mode", ["bold", "highlight", "absent", "both"])
def test_correct_answer_requires_one_consistent_style(initial, mode):
    live = parse_initial_html(initial, NOW)
    lines = [
        line("•", 530, 100),
        line("Answer one", 548, 102, bold=mode in ("bold", "both")),
        line("•", 530, 130),
        line("Answer two", 548, 132, bold=mode == "both"),
    ]
    highlights = [[540, 99, 700, 118]] if mode == "highlight" else []
    q = reference(live, answers=answer_lines(lines, highlights, []))
    finish_correct(q)
    if mode in ("bold", "highlight"):
        assert q.correct_answer_indexes == [0]
        assert [a.correct_pdf for a in q.answers] == [True, False]
    else:
        assert q.correct_answer_indexes is None
        assert all(a.correct_pdf is None for a in q.answers)


def test_unbulleted_paragraph_spacing():
    lines = [
        line("Answer one", 515, 100),
        line("wrapped", 515, 114.4),
        line("Answer two", 515, 129.5),
        line("Answer three", 515, 144.6),
    ]
    answers = answer_lines(lines, [], [])
    assert [a.text for a in answers] == ["Answer one wrapped", "Answer two", "Answer three"]


def test_initial_many_codes_and_determinism(initial):
    live = parse_initial_html(initial, NOW)
    pdf = [reference(live), reference(live, "Г.2.2.4", 2)]
    result = match_questions([live], pdf)
    assert result == match_questions([live], pdf)
    assert result[0]["official_id"] is None
    assert result[0]["live_stable_key"] == live.stable_key
    assert result[0]["match_status"] == "exact"
    assert {m["pdf_code"] for m in result[0]["matches"]} == {"М.2.2.3", "Г.2.2.4"}


def test_duplicate_text_different_images_requires_evidence(initial):
    live = rebuild(parse_initial_html(initial, NOW), text="Что показано на рисунке?")
    pdf = [
        reference(live, illustration_refs=[{"sha256": "1" * 64}]),
        reference(live, "П.1.1.1", 2, illustration_refs=[{"sha256": "2" * 64}]),
    ]
    assert content_key(pdf[0]) != content_key(pdf[1])
    assert match_questions([live], pdf)[0]["match_status"] == "ambiguous"
    assert not match_questions([live], pdf)[0]["matches"]


def test_wording_and_answer_order_changed(initial):
    live = rebuild(parse_initial_html(initial, NOW), resources=[])
    q = reference(
        live,
        question_text=live.text.replace("перечисленных", "указанных"),
        answers=list(reversed(reference(live).answers)),
    )
    result = match_questions([live], [q])[0]
    assert result["match_status"] == "strong"
    diff = result["matches"][0]["differences"]
    assert diff["question_text_changed"]
    assert diff["answer_order_changed"]
    assert not diff["answers_text_changed"]


def test_ambiguous_near_and_unmatched(initial):
    live = rebuild(parse_initial_html(initial, NOW), resources=[])
    pdf = [
        reference(live, question_text=live.text + " Здесь."),
        reference(live, "П.1.1.1", 2, question_text=live.text + " Там."),
    ]
    assert match_questions([live], pdf)[0]["match_status"] == "ambiguous"
    unrelated = reference(
        live,
        question_text="Абсолютно иной предмет разговора",
        answers=[PdfAnswer(order=1, text="Снег"), PdfAnswer(order=2, text="Дождь")],
    )
    assert match_questions([live], [unrelated])[0]["match_status"] == "unmatched"


def test_changed_correct_is_reported_but_live_wins(initial):
    live = parse_initial_html(initial, NOW)
    different = next(i for i, a in enumerate(live.answers) if not a.correct)
    q = reference(live, correct_answer_indexes=[different])
    result = match_questions([live], [q])[0]
    assert result["matches"][0]["differences"]["correct_answer_changed"]
    assert live.answers[different].correct is False


def test_numeric_near_change_is_reviewed(initial):
    live = rebuild(
        parse_initial_html(initial, NOW),
        text="На расстоянии 100 метров что необходимо сделать?",
        resources=[],
    )
    q = reference(live, question_text=live.text.replace("100", "200"))
    result = match_questions([live], [q])[0]
    assert result["match_status"] == "probable"
    assert not result["classification_usable"]


def test_unknown_pdf_correct_stays_unknown(initial):
    live = parse_initial_html(initial, NOW)
    result = match_questions([live], [reference(live)])[0]
    assert result["matches"][0]["differences"]["correct_answer_changed"] is None


def test_empty_pdf_is_not_success(tmp_path):
    path = tmp_path / "empty.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    with pytest.raises(ValueError, match="No question rows"):
        parse_pdf(path, tmp_path / "parsed", tmp_path / "reference-media", root=tmp_path)


def test_image_descriptors_are_deterministic(tmp_path):
    path = tmp_path / "image"
    path.write_bytes(PNG)
    desc = image_descriptor(path)
    assert desc == image_descriptor(path)
    assert media_evidence(["a"], ["b"], {"a": desc, "b": desc})["kind"] == "pixels_equal"


def test_generated_pdf_full_parse_and_no_snapshot_writes(tmp_path):
    path = tmp_path / "small.pdf"
    with pymupdf.open() as doc:
        for number in range(2):
            page = doc.new_page(width=842, height=595)
            page.insert_text((60, 45), "T.1.1. Fixture heading", fontsize=11)
            for x in [50, 110, 320, 510, 790]:
                page.draw_rect(pymupdf.Rect(x, 70, x + 0.48, 220), color=None, fill=(0, 0, 0))
            for y in [70, 220]:
                for left, right in zip([50, 110, 320, 510], [110, 320, 510, 790], strict=True):
                    page.draw_rect(
                        pymupdf.Rect(left, y, right, y + 0.48), color=None, fill=(0, 0, 0)
                    )
            page.insert_text((60, 125), f"T.1.1.{number + 1}", fontsize=10)
            page.insert_text((120, 115), "Fixture question", fontsize=11)
            page.insert_text((520, 115), "First answer", fontsize=11, fontname="hebo")
            page.insert_text((520, 140), "Second answer", fontsize=11)
            page.insert_image(pymupdf.Rect(330, 85, 480, 205), stream=PNG)
        doc.save(path)
    out, media = tmp_path / "parsed", tmp_path / "reference-media"
    s = parse_pdf(path, out, media, root=tmp_path)
    assert s["question_positions"] == 2
    assert s["correct_known"] == 2
    assert s["image_placements"] == 2
    assert len(list(media.iterdir())) == 1
    before = (out / "questions.jsonl").read_bytes()
    assert parse_pdf(path, out, media, root=tmp_path) == s
    assert (out / "questions.jsonl").read_bytes() == before
    with pytest.raises(ValueError, match="protected"):
        safe_output(tmp_path / "snapshots/current/derived", tmp_path)


def test_cli_uses_current_and_preserves_snapshot(tmp_path, initial, next_raw, capsys):
    successful_sync(tmp_path, initial, next_raw, media=False)
    pointer = json.loads((tmp_path / "state/current.json").read_text())
    snapshot = tmp_path / pointer["path"]
    before = {p: p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}
    q = parse_initial_html(initial, NOW)
    parsed = tmp_path / "reference/parsed"
    parsed.mkdir(parents=True)
    (parsed / "questions.jsonl").write_text(reference(q).model_dump_json() + "\n", encoding="utf-8")
    (parsed / "summary.json").write_text(json.dumps({"source": reference(q).source}))
    (parsed / "sections.json").write_text("{}")
    assert (
        main(
            [
                "reference",
                "crosswalk",
                "--data-root",
                str(tmp_path),
                "--parsed",
                str(parsed),
                "--media",
                str(tmp_path / "reference/media"),
                "--json-summary",
            ]
        )
        == 0
    )
    s = json.loads(
        (
            tmp_path / "derived" / pointer["snapshot_id"] / "reference-crosswalk/summary.json"
        ).read_text(encoding="utf-8")
    )
    assert s["snapshot_id"] == pointer["snapshot_id"]
    assert all(p.read_bytes() == data for p, data in before.items())
