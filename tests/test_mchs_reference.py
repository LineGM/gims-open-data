"""Offline categorized fixtures are synthetic; no published PDFs enter Git."""

import copy

import httpx
import pymupdf
import pytest

from gims_open_data.client import ImportStopped
from gims_open_data.mchs_reference_group import build_reference_groups, safe_answer_normalized
from gims_open_data.mchs_reference_inventory import (
    SOURCES,
    PublicClient,
    allowed_url,
    discover,
    inventory,
    pdf_metadata,
)
from gims_open_data.mchs_reference_match import (
    answer_check,
    critical_tokens,
    crosswalk,
    group_answer_check,
    match_questions,
    requires_image,
    text_variants,
)
from gims_open_data.mchs_reference_pdf import (
    PublishedQuestion,
    consume_rows,
    parse,
    parse_document,
    published_answers,
    read_records,
)
from gims_open_data.mchs_reference_verify import (
    privacy_check,
    verify,
    verify_crosswalk,
)
from gims_open_data.media import file_hash
from gims_open_data.parse_html import parse_initial_html
from gims_open_data.reference_pdf import normalized, write_jsonl
from gims_open_data.snapshot import current_pointer, read_json
from gims_open_data.storage import atomic_json

from .test_diff import rebuild
from .test_parsers import NOW
from .test_sync import PNG, successful_sync


def landing(count=1, suffix=""):
    cards = "".join(
        f'<div class="doc-item"><a class="doc-item__title">1.{i}. Topic {i}</a>'
        f'<a href="/uploads/document/2024-02-09/{i}{suffix}.pdf"> Download </a></div>'
        for i in range(1, count + 1)
    )
    return (
        '<html><h1>Category</h1><link rel="canonical" href="'
        + SOURCES[0][2]
        + '">'
        + cards
        + '<a href="/uploads/document/ignored.docx">Other</a></html>'
    ).encode()


def span(text, x=580, y=120, bold=True, page=1):
    return {"text": text, "bbox": [x, y, x + 100, y + 11], "flags": 16 if bold else 0, "page": page}


def synthetic_pdf():
    pdf = pymupdf.open()
    for p in range(2):
        page = pdf.new_page(width=842, height=595)
        if p == 0:
            page.insert_text((55, 40), "1.1. Test topic")
        for y in (60, 90, 250, 400):
            for left, right in zip((51, 99, 319, 560), (99, 319, 560, 780), strict=True):
                page.draw_rect(pymupdf.Rect(left, y, right, y + 0.48), color=None, fill=(0, 0, 0))
        for x in (51, 99, 319, 560, 780):
            page.draw_rect(pymupdf.Rect(x, 60, x + 0.48, 400), color=None, fill=(0, 0, 0))
        page.insert_text((55, 77), "No")
        for number, y in ((p * 2 + 1, 150), (p * 2 + 2, 300)):
            page.insert_text((60, y), f"{number}.")
            page.insert_text((105, y), f"Test question {number}?")
            page.insert_text((570, y), "Published answer", fontname="hebo")
            page.insert_image(pymupdf.Rect(330, y - 40, 420, y + 30), stream=PNG)
    body = pdf.tobytes()
    pdf.close()
    return body


def member(category="motor", code="1.1"):
    dimension = (
        "ship_type"
        if category in {"motor", "sailing", "hydrocycle", "special_construction"}
        else "sailing_area"
    )
    url = next(u for d, c, u in SOURCES if c == category)
    return {
        "membership_id": category + code,
        "dimension": dimension,
        "category": category,
        "topic_code": code,
        "topic_title": "Test topic",
        "pdf_url": "https://mchs.gov.ru/test.pdf",
        "landing_page_url": url,
    }


def reference(live, *, text=None, answer=None, category="motor", order=1, images=None, known=True):
    text = live.text if text is None else text
    answer = next(a.text for a in live.answers if a.correct) if answer is None else answer
    return PublishedQuestion(
        reference_id=f"{'a' * 64}:{order}",
        source_pdf_sha256="a" * 64,
        document_id="a" * 64,
        memberships=[member(category)],
        page=1,
        order=order,
        question_number=f"{order}.",
        raw_question_text=text,
        question_text=" ".join(text.split()),
        normalized_question_text=normalized(text),
        published_answers=[answer],
        raw_published_answers=[answer],
        published_correct_answer=answer if known else None,
        published_correct_answer_indexes=[0] if known else None,
        correctness_known=known,
        answer_structure="published_answer_only",
        correctness_evidence={},
        illustration_refs=images or [],
        graphic_refs=[],
        row_refs=[],
        issues=[],
    )


@pytest.fixture
def live(initial):
    q = parse_initial_html(initial, NOW)
    return rebuild(
        q, text="Как называется способность судна сохранять заданную скорость?", resources=[]
    )


def test_discovery_title_card_taxonomy_duplicate_link_and_ignored():
    body = landing().replace(b"</html>", landing() + b"</html>")
    result = discover(body, *SOURCES[0])
    assert len(result["documents"]) == 1
    doc = result["documents"][0]
    assert doc["link_occurrences"] == 2
    assert doc["category"] == "motor" and doc["topic_code"] == "1.1"
    assert doc["topic_title"] == "Topic 1" and doc["link_text"] == " Download "
    assert doc["storage_path_date"] == "/2024-02-09/"
    assert result["ignored_links"][0]["reason"] == "non_pdf"


@pytest.mark.parametrize(
    "url",
    [
        "http://mchs.gov.ru/a.pdf",
        "https://other.ru/a.pdf",
        "https://mchs.gov.ru.evil/a.pdf",
        "https://u:p@mchs.gov.ru/a.pdf",
        "https://mchs.gov.ru:85/a.pdf",
        "https://mchs.gov.ru/%2e%2e/a.pdf",
        "https://mchs.gov.ru/a.pdf?token=secret",
    ],
)
def test_url_guard(url):
    with pytest.raises(ValueError):
        allowed_url(url)


def test_redirect_allowlist_and_no_cookie_reuse(monkeypatch):
    monkeypatch.setattr("gims_open_data.mchs_reference_inventory.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request)
        assert "cookie" not in request.headers
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "/b.pdf", "set-cookie": "secret=value"})
        return httpx.Response(
            200, content=b"%PDF-test", headers={"content-type": "application/pdf"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        body, meta = PublicClient(http, 0).get("https://mchs.gov.ru/a.pdf", pdf=True)
    assert body.startswith(b"%PDF-") and len(meta["redirect_chain"]) == 1
    assert "set-cookie" not in meta["headers"]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "https://evil.test/a.pdf"})
        )
    ) as http:
        with pytest.raises(ValueError):
            PublicClient(http, 0).get("https://mchs.gov.ru/a.pdf", pdf=True)


@pytest.mark.parametrize("status,expected", [(429, 3), (503, 3), (403, 1), (404, 1)])
def test_get_retries_only_transient(monkeypatch, status, expected):
    monkeypatch.setattr("gims_open_data.mchs_reference_inventory.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError):
            PublicClient(http, 0).get("https://mchs.gov.ru/a.pdf", pdf=True)
    assert len(calls) == expected


@pytest.mark.parametrize(
    "body,ctype",
    [(b"<html/>", "application/pdf"), (b"%PDF-x", "text/html"), (b"%PDF-x", "application/pdf")],
)
def test_invalid_pdf(body, ctype):
    with pytest.raises((ValueError, RuntimeError)):
        pdf_metadata(body, ctype)


def test_pdf_size_limit(monkeypatch):
    monkeypatch.setattr("gims_open_data.mchs_reference_inventory.MAX_PDF", 4)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"12345"))
    ) as http:
        with pytest.raises(ValueError, match="byte limit"):
            PublicClient(http, 0).get("https://mchs.gov.ru/a.pdf", pdf=True)


def test_parser_real_geometry_heading_images_and_unknown_correctness(tmp_path):
    path = tmp_path / "fixture.pdf"
    path.write_bytes(synthetic_pdf())
    media = tmp_path / "media"
    media.mkdir()
    qs, topic = parse_document(path, [member()], media)
    assert len(qs) == 4 and topic["internal_heading"] == "1.1. Test topic"
    assert qs[0].question_text == "Test question 1?"
    assert all(len(q.illustration_refs) == 1 for q in qs)
    assert all(not q.correctness_known for q in qs)  # no published correctness legend
    assert len(list(media.iterdir())) == 1


def test_answer_only_full_options_and_unknown():
    one = published_answers([span("•"), span("Correct answer", x=600)], True)
    assert one["answer_structure"] == "published_answer_only" and one["correctness_known"]
    assert not published_answers([span("Answer", bold=False)], True)["correctness_known"]
    spans = [span(f"• Option {i}", y=120 + i * 30, bold=i == 2) for i in range(4)]
    full = published_answers(spans, True)
    assert full["answer_structure"] == "full_options" and full[
        "published_correct_answer_indexes"
    ] == [2]
    assert (
        published_answers([span("• Both"), span("• Bold", y=150)], True)[
            "published_correct_answer_indexes"
        ]
        is None
    )


def test_page_continuation_keeps_answer_reading_order():
    answer = published_answers(
        [span("• First part", y=500), span("continues here", y=70, page=2)], True
    )
    assert answer["published_answers"] == ["First part continues here"]


def test_multiline_malformed_and_repeated_header_do_not_corrupt_next():
    def row(number, question, y):
        return {
            "top": y,
            "bottom": y + 80,
            "columns": [51, 99, 319, 560, 780],
            "cells": [
                [span(number, 60, y + 20)],
                [span(question, 105, y + 20)],
                [],
                [span("• Answer", 570, y + 20)],
            ],
        }

    records, issues = [], []
    st = {
        "page": 1,
        "rows": [
            row("No", "Вопрос", 60),
            row("1.", "First question", 150),
            row("bad", "Malformed", 250),
            row("2.", "Second question", 350),
        ],
    }
    st["rows"][1]["cells"][1].append(span("wrapped", 105, 190))
    consume_rows(st, records, issues, document_id="a" * 64, memberships=[member()], legend=True)
    assert len(records) == 2 and records[0]["raw_question_text"] == "First question\nwrapped"
    assert records[1]["raw_question_text"] == "Second question"
    assert issues[0]["issue"] == "unrecognized_row"


def test_exact_multi_memberships_and_unresolved_initial(live):
    qs = [reference(live), reference(live, category="vvp", order=2)]
    result = match_questions([live], qs)[0]
    assert result["classification_usable"] and len(result["source_matches"]) == 2
    assert result["official_id"] is None and result["live_stable_key"].startswith("fallback:")
    assert len(result["ship_type_memberships"]) == len(result["sailing_area_memberships"]) == 1


def test_exact_question_answer_drift_does_not_overwrite(live):
    before = live.model_dump()
    result = match_questions([live], [reference(live, answer="Different published answer")])[0]
    assert result["match_status"] == "exact"
    assert result["source_matches"][0]["answer_cross_check"]["status"] == "unsupported"
    assert live.model_dump() == before


def test_near_wording_requires_safe_corroboration(live):
    q = rebuild(live, text="Как называется способность судна сохранять заданную скорость движения?")
    ref = reference(q, text=q.text.replace("движения", "движении"))
    result = match_questions([q], [ref])[0]
    assert result["match_status"] == "strong"
    assert not match_questions([q], [reference(q, text=ref.question_text, answer="different")])[0][
        "classification_usable"
    ]


@pytest.mark.parametrize(
    "left,right",
    [
        ("10 метров", "11 метров"),
        ("не разрешается", "разрешается"),
        ("влево", "вправо"),
        ("левый", "правый"),
        ("вверх", "вниз"),
        ("положительная", "отрицательная"),
        ("гидроцикл", "судно особой конструкции"),
        ("моторное", "парусное"),
        ("ВВП", "МП"),
        ("10 м", "10 км"),
    ],
)
def test_critical_changes_block(live, left, right):
    assert critical_tokens(left) != critical_tokens(right)
    prefix = "При управлении маломерным судном в указанных условиях следует учитывать "
    q = rebuild(live, text=prefix + left)
    assert not match_questions([q], [reference(q, text=prefix + right)])[0]["classification_usable"]


def test_image_dependent_mismatch_blocks_strict_identity_supports(live):
    q = rebuild(
        live,
        text="Как называется судно, изображенное на рисунке?",
        resources=[
            {"url": "https://digital.mchs.gov.ru:85/testing_bucket/test.png", "sha256": "b" * 64}
        ],
    )
    assert not match_questions([q], [reference(q, images=[{"sha256": "c" * 64}])])[0][
        "classification_usable"
    ]
    assert (
        match_questions([q], [reference(q, images=[{"sha256": "b" * 64}])])[0]["match_status"]
        == "exact"
    )


def test_line_wrap_requires_corroboration(live):
    q = rebuild(
        live, text="Как называется способность маломерного судна сохранять заданную скорость?"
    )
    text = q.text.replace("маломерного", "маломер-\nного")
    assert match_questions([q], [reference(q, text=text)])[0]["match_status"] == "strong"
    assert not match_questions([q], [reference(q, text=text, answer="different")])[0][
        "classification_usable"
    ]
    assert text_variants("парусно-\nмоторное")[2] == "парусно-моторное"


def test_answer_unknown_and_safe_typography(live):
    q = reference(live, known=False)
    assert answer_check(live, q)["status"] == "unknown_pdf_correctness"
    answer = next(a.text for a in live.answers if a.correct)
    assert (
        answer_check(live, reference(live, answer="«" + answer + "»"))["status"]
        == "wording_changed_same_semantics"
    )


def test_repeated_stem_groups_rows_and_answers_without_live_selection(live):
    refs = [reference(live, order=i, answer=f"Published {i}") for i in (46, 47, 48)]
    groups = build_reference_groups(refs)
    assert len(groups) == 1
    assert groups[0]["source_row_ids"] == [f"{'a' * 64}:{i}" for i in (46, 47, 48)]
    assert groups[0]["answer_multiplicity"] == 3
    result = match_questions([live], refs)[0]
    assert len(result["source_matches"]) == 1
    assert result["source_matches"][0]["answer_cross_check"]["status"] == "unsupported"
    assert result["source_matches"][0]["reference_question_group"]["source_rows"] == [
        f"{'a' * 64}:{i}" for i in (46, 47, 48)
    ]


def test_group_answer_supported_among_multiple_answers(live):
    refs = [
        reference(live, order=i, answer=next(a.text for a in live.answers if a.correct))
        for i in (1, 2, 3)
    ]
    group = build_reference_groups(refs)[0]
    check = group_answer_check(live, group)
    assert check["status"] == "supported_exact"
    assert check["source_multiple_answers"] is False


def test_same_stem_different_images_and_categories_are_separate(live):
    image_text = "Что обозначает изображенный знак?"
    first = reference(live, text=image_text, order=1, images=[{"sha256": "b" * 64}])
    second = reference(live, text=image_text, order=2, images=[{"sha256": "c" * 64}])
    third = reference(live, text=image_text, category="vvp", order=3, images=[{"sha256": "b" * 64}])
    groups = build_reference_groups([first, second, third])
    assert len(groups) == 3
    assert {g["category"] for g in groups} == {"motor", "vvp"}
    assert {tuple(g["source_row_ids"]) for g in groups} == {
        (f"{'a' * 64}:1",),
        (f"{'a' * 64}:2",),
        (f"{'a' * 64}:3",),
    }


def test_safe_answer_normalization_only_changes_formatting():
    assert safe_answer_normalized("Скорость; Управляемость; Ходкость") == safe_answer_normalized(
        "Скорость, управляемость, ход-\nкость"
    )


@pytest.fixture
def corpus(tmp_path, monkeypatch, initial, next_raw):
    successful_sync(tmp_path, initial, next_raw)
    body = synthetic_pdf()
    calls = []
    original = httpx.Client

    def handler(request):
        calls.append(str(request.url))
        for _dimension, category, url in SOURCES:
            if str(request.url) == url:
                return httpx.Response(
                    200,
                    content=landing(14 if category == "motor" else 1),
                    headers={"content-type": "text/html"},
                )
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})

    monkeypatch.setattr(
        "gims_open_data.mchs_reference_inventory.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("gims_open_data.mchs_reference_inventory.time.sleep", lambda _: None)
    summary = inventory(tmp_path)
    assert summary["unique_urls"] == 14 and summary["unique_pdf_sha256"] == 1
    assert summary["pdf_memberships"] == 20 and summary["duplicate_url_memberships"] == 6
    assert len(calls) == 21
    parse(tmp_path)
    return tmp_path


def test_inventory_duplicate_sha_preserves_all_memberships_and_offline_resume(corpus, monkeypatch):
    summary = verify(corpus)
    assert summary["inventory_valid"] and summary["parsed_valid"]
    questions = read_records(corpus / "reference/mchs-categorized/parsed/questions.jsonl")
    assert len(questions) == 4 and len(questions[0]["memberships"]) == 20
    monkeypatch.setattr(
        PublicClient, "get", lambda *a, **kw: pytest.fail("cached inventory must be offline")
    )
    assert inventory(corpus)["coherent"]


@pytest.mark.parametrize("target", ["pdf", "html", "parsed", "membership", "snapshot", "summary"])
def test_corrupted_artifact_fails(corpus, target):
    base = corpus / "reference/mchs-categorized"
    if target == "pdf":
        path = next((base / "pdf/objects").glob("*.pdf"))
        path.write_bytes(b"broken")
    elif target == "html":
        path = next((base / "sources/landing-pages").glob("*.html"))
        path.write_bytes(b"broken")
    elif target in ("parsed", "membership"):
        path = base / "parsed/questions.jsonl"
        if target == "parsed":
            path.write_text("{}\n")
        else:
            records = read_records(path)
            records[0]["memberships"].pop()
            write_jsonl(path, records)
            summary = read_json(base / "parsed/summary.json")
            summary["artifacts"]["questions.jsonl"] = file_hash(path)
            atomic_json(base / "parsed/summary.json", summary)
    elif target == "snapshot":
        snapshot = current_pointer(corpus)["snapshot_id"]
        (corpus / "snapshots" / snapshot / "questions.jsonl").write_bytes(b"broken")
    else:
        summary = read_json(base / "parsed/summary.json")
        summary["question_positions"] += 1
        atomic_json(base / "parsed/summary.json", summary)
    with pytest.raises((ValueError, ImportStopped)):
        verify(corpus)


def test_short_answer_words_are_not_diagram_labels():
    assert not requires_image("Какой должна быть поверхность?", ["гладкой", "шероховатой"])
    assert requires_image("Выберите обозначение", ["а", "б", "в", "г"])
    assert requires_image("Какой объект показан на рисунке?", ["судно", "гидроцикл"])


@pytest.mark.parametrize(
    "payload",
    [
        "C:\\Users\\test\\data",
        "https://mchs.gov.ru/testing/instance/123",
        "PHPSESSID=abc0123456789secret",
        "csrf_token: abc0123456789secret",
    ],
)
def test_privacy_checks(tmp_path, payload):
    path = tmp_path / "artifact.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        privacy_check(path)


def test_stop_check_does_not_download_pdfs(tmp_path, monkeypatch):
    calls = []

    def get(self, url, *, pdf):
        assert not pdf
        calls.append(url)
        return landing(13), {
            "url": url,
            "status": 200,
            "retrieved_at": "2026-09-12T00:00:00Z",
            "headers": {"content-type": "text/html"},
            "redirect_chain": [],
        }

    monkeypatch.setattr(PublicClient, "get", get)
    with pytest.raises(ValueError, match="Inventory incomplete"):
        inventory(tmp_path)
    summary = read_json(tmp_path / "reference/mchs-categorized/summary.json")
    assert len(calls) == 7 and summary["failures"] and summary["downloaded_urls"] == 0


def test_source_card_without_explicit_topic_code_is_preserved():
    page = landing().replace(b"1.1. Topic 1", b"Published topic")
    found = discover(page, *SOURCES[0])["documents"][0]
    assert found["topic_code"] is None and found["topic_title"] == "Published topic"


def test_network_retry_then_success(monkeypatch):
    monkeypatch.setattr("gims_open_data.mchs_reference_inventory.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("transient")
        return httpx.Response(200, content=b"%PDF-test")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        body, meta = PublicClient(http, 0).get("https://mchs.gov.ru/a.pdf", pdf=True)
    assert len(calls) == 3 and len(meta["attempts"]) == 3 and body == b"%PDF-test"


def test_crosswalk_stable_keys_and_snapshot_immutability(corpus):
    pointer = (corpus / "state/current.json").read_bytes()
    snapshot = current_pointer(corpus)["snapshot_id"]
    path = corpus / "snapshots" / snapshot
    before = {p.name: file_hash(p) for p in path.iterdir() if p.is_file()}
    first = crosswalk(corpus)
    assert first["live_total"] == 2 and verify_crosswalk(corpus, snapshot)["crosswalk_valid"]
    out = corpus / "derived" / snapshot / "mchs-categorized-crosswalk"
    digest = file_hash(out / "crosswalk.jsonl")
    crosswalk(corpus)
    assert digest == file_hash(out / "crosswalk.jsonl")
    assert before == {p.name: file_hash(p) for p in path.iterdir() if p.is_file()}
    assert pointer == (corpus / "state/current.json").read_bytes()
    records = read_records(out / "crosswalk.jsonl")
    records[1] = copy.deepcopy(records[0])
    write_jsonl(out / "crosswalk.jsonl", records)
    summary = read_json(out / "summary.json")
    summary["artifacts"]["crosswalk.jsonl"] = file_hash(out / "crosswalk.jsonl")
    atomic_json(out / "summary.json", summary)
    with pytest.raises(ValueError, match="Duplicate live stable key"):
        verify_crosswalk(corpus, snapshot)
