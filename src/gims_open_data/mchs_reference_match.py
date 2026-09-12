"""Conservative categorized crosswalk; the legacy result is read only after matching."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from .mchs_reference_group import build_reference_groups, safe_answer_normalized, serializable_group
from .mchs_reference_inventory import SOURCES, VERSION
from .mchs_reference_pdf import PublishedQuestion, read_records
from .media import file_hash
from .models import Question
from .reference_match import image_dependent, image_descriptor, media_evidence
from .reference_pdf import normalized, safe_output, write_jsonl
from .snapshot import current_pointer, read_json, read_questions, snapshot_path, verify_snapshot
from .storage import atomic_json


def match_normalized(text: str) -> str:
    return normalized(text).rstrip(" .?!;:…")


def text_variants(raw: str) -> list[str]:
    """Explicit line-end hyphen hypotheses; each acceptance needs corroboration."""
    return list(
        dict.fromkeys(
            match_normalized(s)
            for s in (
                raw,
                re.sub(r"(?<=[а-яёА-ЯЁ])-\s*\n(?=[а-яёА-ЯЁ])", "", raw),
                re.sub(r"(?<=[а-яёА-ЯЁ])-\s*\n(?=[а-яёА-ЯЁ])", "-", raw),
            )
        )
    )


def critical_tokens(text: str) -> tuple[str, ...]:
    value = match_normalized(text)
    tokens = re.findall(
        r"\d+(?:[.,]\d+)?|\b(?:не|ни|нет|нельзя|без|запре\w*|разреш\w*|допус\w*|"
        r"лев\w*|прав\w*|влев\w*|вправ\w*|слева|справа|верх\w*|ниж\w*|вверх\w*|вниз\w*|"
        r"увелич\w*|уменьш\w*|положит\w*|отрицат\w*|"
        r"гидроцикл\w*|парусн\w*|моторн\w*|швертбот\w*|катер\w*|яхт\w*|"
        r"особ\w*|конструкц\w*|воздушн\w*|подушк\w*|"
        r"ввп|вп|мп|морск\w*|территориальн\w*|внутренн\w*|речн\w*|порт\w*|"
        r"фарватер\w*|шлюз\w*|судоходн\w*|"
        r"км|м|см|мм|кг|квт|л\.с|узл\w*|метр\w*|секунд\w*|минут\w*|час\w*)\b|[%°]",
        value,
    )
    return tuple(tokens)


def answer_key(text: str) -> str:
    value = match_normalized(text)
    if len(value) > 1 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def requires_image(text: str, answers: list[str]) -> bool:
    # Short ordinary answer words (e.g. "Гладкой") are not diagram labels.
    labels = bool(answers) and all(
        a not in {"да", "нет", "все"}
        and re.fullmatch(r"[а-яa-z0-9]{1,3}(?:[)()., /-]+[а-яa-z0-9]{1,3})*[)()., /-]*", a)
        for a in answers
    )
    return image_dependent(text, []) or labels


def answer_check(live: Question, pdf: PublishedQuestion) -> dict[str, Any]:
    current = [a.text for a in live.answers if a.correct]
    published = pdf.published_correct_answer
    status = "unknown_pdf_correctness"
    if pdf.correctness_known:
        if len(current) != 1 or published is None or any(a.resources for a in live.answers):
            status = "not_comparable"
        elif current[0] == published:
            status = "same"
        else:
            index = (pdf.published_correct_answer_indexes or [0])[0]
            variants = [answer_key(s) for s in text_variants(pdf.raw_published_answers[index])]
            status = (
                "wording_changed_same_semantics"
                if answer_key(current[0]) in variants
                else "different"
            )
    return {
        "status": status,
        "live_correct_answers": current,
        "published_correct_answer": published,
        "pdf_correctness_evidence": pdf.correctness_evidence,
    }


def group_answer_check(live: Question, group: dict[str, Any]) -> dict[str, Any]:
    """Cross-check one live answer against the complete source-group answer set.

    The live answer is never used to form ``group_id`` or select a source row.
    """
    current = [a.text for a in live.answers if a.correct]
    published = list(group["published_answers"])
    answer_keys = [safe_answer_normalized(a) for a in published]
    unique_keys = list(dict.fromkeys(answer_keys))
    status = "not_comparable"
    matched: list[str] = []
    if len(current) == 1 and published:
        current_text = current[0]
        current_key = safe_answer_normalized(current_text)
        exact_matches = [
            a for a, key in zip(published, answer_keys, strict=True) if a == current_text
        ]
        normalized_matches = [
            a for a, key in zip(published, answer_keys, strict=True) if key == current_key
        ]
        matched = exact_matches or normalized_matches
        if exact_matches:
            status = "supported_exact"
        elif current_key in unique_keys:
            status = "supported_format_normalized"
        else:
            status = "unsupported"
    return {
        "status": status,
        "live_correct_answers": current,
        "published_answers": published,
        "supported_published_answers": matched,
        "source_multiple_answers": len(unique_keys) > 1,
        "published_answer_keys": unique_keys,
        "source_row_ids": list(group["source_row_ids"]),
    }


def memberships_for(
    matches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    categories: dict[tuple[str, str], dict[str, Any]] = {}
    topics: dict[str, dict[str, Any]] = {}
    for match in matches:
        for membership in match["memberships"]:
            key = (membership["dimension"], membership["category"])
            group = categories.setdefault(
                key, {"dimension": key[0], "category": key[1], "evidence": []}
            )
            group["evidence"].append(
                {
                    "reference_id": match["reference_id"],
                    "membership_id": membership["membership_id"],
                }
            )
            topic = topics.setdefault(
                membership["membership_id"],
                {
                    k: membership[k]
                    for k in (
                        "membership_id",
                        "dimension",
                        "category",
                        "topic_code",
                        "topic_title",
                        "pdf_url",
                        "landing_page_url",
                    )
                },
            )
            topic.setdefault("source_documents", [])
            if match["document_id"] not in topic["source_documents"]:
                topic["source_documents"].append(match["document_id"])
    return (
        [v for (d, c), v in sorted(categories.items()) if d == "ship_type"],
        [v for (d, c), v in sorted(categories.items()) if d == "sailing_area"],
        [v for k, v in sorted(topics.items())],
    )


def match_questions(
    live: list[Question],
    pdf: list[PublishedQuestion],
    descriptors: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    from rapidfuzz import fuzz, process

    descriptors = descriptors or {}

    @lru_cache(maxsize=100000)
    def compare_images(live_hashes: tuple[str, ...], pdf_hashes: tuple[str, ...]) -> dict[str, Any]:
        return media_evidence(list(live_hashes), list(pdf_hashes), descriptors)

    groups = build_reference_groups(pdf)
    variants = [text_variants(g["source_rows"][0].raw_question_text) for g in groups]
    variant_index: dict[str, list[int]] = defaultdict(list)
    for i, values in enumerate(variants):
        for value in values:
            variant_index[value].append(i)
    choices = sorted(variant_index)
    results = []
    for live_q in live:
        lt = match_normalized(live_q.text)
        candidates = set(variant_index.get(lt, []))
        # Near candidates are generated only when exact text has no source hit.
        # This keeps the full 1513-question run bounded while retaining deterministic review.
        if not candidates:
            for text, _, _ in process.extract(
                lt, choices, scorer=fuzz.ratio, limit=40, score_cutoff=85
            ):
                candidates.update(variant_index[text])
        evaluated: list[dict[str, Any]] = []
        for i in sorted(candidates):
            group = groups[i]
            q = group["source_rows"][0]
            best_text = max(
                variants[i], key=lambda text: (fuzz.ratio(lt, text), text == variants[i][0])
            )
            score = fuzz.ratio(lt, best_text) / 100
            exact = lt == variants[i][0]
            group_images = [im for row in group["source_rows"] for im in row.illustration_refs]
            image = compare_images(
                tuple(r.sha256 for r in live_q.resources if r.sha256),
                tuple(im["sha256"] for im in group_images if im.get("sha256")),
            )
            strict_image = bool(
                (image["identity"] or image.get("strict_visual_support"))
                and live_q.resources
                and group_images
            )
            needs_image = (
                requires_image(lt, [normalized(a.text) for a in live_q.answers])
                or group["image_dependent"]
                or requires_image(best_text, [])
            )
            answer = group_answer_check(live_q, group)
            safe_answer = answer["status"].startswith("supported_")
            critical_equal = critical_tokens(lt) == critical_tokens(best_text)
            blockers = []
            if not critical_equal:
                blockers.append("critical_tokens_changed")
            if needs_image and not strict_image:
                blockers.append("image_dependent_without_strict_support")
            if needs_image and (
                any(row.graphic_refs for row in group["source_rows"])
                or any(im.get("smask") for im in group_images)
            ):
                blockers.append("uncompared_graphic_overlay_or_mask")
            if "incomplete_content" in q.issues or "text_layer_replacement_character" in q.issues:
                blockers.append("incomplete_source")
            if any(a.resources for a in live_q.answers):
                blockers.append("live_answer_media")
            corroboration = strict_image or (
                safe_answer and len(answer_key(" ".join(group["published_answers"]))) >= 20
            )
            # Exact line-wrap hypothesis still needs answer or real image support.
            strong = (score == 1 and (safe_answer or strict_image)) or (
                score >= 0.97 and corroboration
            )
            accepted_pair = not blockers and (exact or strong)
            status = ("exact" if exact else "strong") if accepted_pair else "probable"
            image_changed: bool | None = None
            if image["identity"]:
                image_changed = False
            elif image["kind"] == "presence_changed" or (
                image["kind"] == "visual_comparison" and not image["visual_support"]
            ):
                image_changed = True
            evaluated.append(
                {
                    "reference_id": q.reference_id,
                    "reference_question_group": {
                        "group_id": group["group_id"],
                        "dimension": group["dimension"],
                        "category": group["category"],
                        "topic_code": group["topic_code"],
                        "topic_title": group["topic_title"],
                        "normalized_question_text": group["normalized_question_text"],
                        "source_rows": group["source_row_ids"],
                        "published_answers": group["published_answers"],
                        "answer_multiplicity": group["answer_multiplicity"],
                        "source_multiple_answers": group["source_multiple_answers"],
                    },
                    "source_row_ids": group["source_row_ids"],
                    "document_id": q.document_id,
                    "pdf_page": q.page,
                    "question_number": q.question_number,
                    "memberships": group["memberships"],
                    "question_text": group["question_text"],
                    "source_derived_memberships": q.source_derived_memberships,
                    "match_status": status,
                    "classification_usable": accepted_pair,
                    "question_similarity": round(score, 6),
                    "method": "exact_normalized_text"
                    if exact
                    else ("line_wrap_corroborated" if score == 1 else "very_near_corroborated"),
                    "matched_text_variant": best_text,
                    "critical_tokens_equal": critical_equal,
                    "image_required": needs_image,
                    "media_evidence": image,
                    "image_changed": image_changed,
                    "wording_changed": lt != best_text,
                    "answer_cross_check": answer,
                    "blockers": blockers,
                }
            )
        evaluated.sort(key=lambda e: (-e["question_similarity"], e["reference_id"]))
        accepted = [e for e in evaluated if e["classification_usable"]]
        # Distinct very-near wording at a competing score cannot be selected by arbitrary order.
        if accepted and not any(
            e["match_status"] == "exact" or e["question_similarity"] == 1 for e in accepted
        ):
            best = accepted[0]["question_similarity"]
            competing = [
                e
                for e in evaluated
                if e["critical_tokens_equal"] and e["question_similarity"] >= best - 0.01
            ]
            if len({e["matched_text_variant"] for e in competing}) > 1:
                for e in accepted:
                    e["classification_usable"] = False
                    e["match_status"] = "ambiguous"
                    e["blockers"].append("competing_near_wording")
                accepted = []
        review = [e for e in evaluated if not e["classification_usable"]]
        if accepted:
            status = "exact" if any(e["match_status"] == "exact" for e in accepted) else "strong"
        elif not review:
            status = "unmatched"
        elif (
            any(e["match_status"] == "ambiguous" for e in review)
            or len(
                {
                    e["matched_text_variant"]
                    for e in review
                    if e["question_similarity"] >= review[0]["question_similarity"] - 0.01
                }
            )
            > 1
        ):
            status = "ambiguous"
        else:
            status = "probable"
        ships, areas, topics = memberships_for(accepted)
        results.append(
            {
                "live_stable_key": live_q.stable_key,
                "official_id": str(live_q.official_id) if live_q.official_id else None,
                "id_status": live_q.id_status,
                "live_position": live_q.position,
                "live_question_text": live_q.text,
                "classification_usable": bool(accepted),
                "match_status": status,
                "ship_type_memberships": ships,
                "sailing_area_memberships": areas,
                "topics": topics,
                "source_matches": accepted,
                "candidates": review,
            }
        )
    return results


def crosswalk_metrics(rows: list[dict[str, Any]], pdf: list[PublishedQuestion]) -> dict[str, Any]:
    matched = {
        row_id
        for r in rows
        for m in r["source_matches"]
        for row_id in m.get("source_row_ids", [m["reference_id"]])
    }
    groups = build_reference_groups(pdf)
    answers = Counter(m["answer_cross_check"]["status"] for r in rows for m in r["source_matches"])
    covered = sum(r["classification_usable"] for r in rows)
    categories = {}
    for _dimension, category, _ in SOURCES:
        subset = [q for q in pdf if any(m["category"] == category for m in q.memberships)]
        categories[category] = {
            "pdf_docs": len({q.document_id for q in subset}),
            "pdf_question_positions": len(subset),
            "unique_pdf_questions": len({q.normalized_question_text for q in subset}),
            "matched_live_questions": sum(
                any(t["category"] == category for t in r["topics"]) for r in rows
            ),
        }
    return {
        "live_total": len(rows),
        "confident_matched": covered,
        "match_percentage": round(100 * covered / max(1, len(rows)), 4),
        "match_status_counts": {
            status: sum(r["match_status"] == status for r in rows)
            for status in ("exact", "strong", "probable", "ambiguous", "unmatched")
        },
        "live_without_confident_match": len(rows) - covered,
        "matched_reference_positions": len(matched),
        "reference_question_groups": len(groups),
        "matched_reference_groups": len(
            {m["reference_question_group"]["group_id"] for r in rows for m in r["source_matches"]}
        ),
        "reference_groups_with_one_source_row": sum(len(g["source_rows"]) == 1 for g in groups),
        "reference_groups_with_multiple_source_rows": sum(
            len(g["source_rows"]) > 1 for g in groups
        ),
        "reference_groups_with_multiple_published_answers": sum(
            g["source_multiple_answers"] for g in groups
        ),
        "reference_group_max_source_rows": max((len(g["source_rows"]) for g in groups), default=0),
        "unmatched_reference_positions": len(pdf) - len(matched),
        "ship_type_coverage": sum(bool(r["ship_type_memberships"]) for r in rows),
        "sailing_area_coverage": sum(bool(r["sailing_area_memberships"]) for r in rows),
        "topic_coverage": sum(bool(r["topics"]) for r in rows),
        "both_ship_and_area": sum(
            bool(r["ship_type_memberships"] and r["sailing_area_memberships"]) for r in rows
        ),
        "multiple_categories": sum(
            len(r["ship_type_memberships"]) + len(r["sailing_area_memberships"]) > 1 for r in rows
        ),
        "multiple_ship_types": sum(len(r["ship_type_memberships"]) > 1 for r in rows),
        "multiple_sailing_areas": sum(len(r["sailing_area_memberships"]) > 1 for r in rows),
        "multiple_topics": sum(len(r["topics"]) > 1 for r in rows),
        "multiple_source_positions": sum(len(r["source_matches"]) > 1 for r in rows),
        "categories": categories,
        "answer_cross_check": {
            status: answers[status]
            for status in (
                "supported_exact",
                "supported_format_normalized",
                "supported_semantic_safe",
                "unsupported",
                "not_comparable",
                "image_ambiguous",
            )
        },
        "answer_comparable_groups": sum(
            answers[s]
            for s in (
                "supported_exact",
                "supported_format_normalized",
                "supported_semantic_safe",
                "unsupported",
            )
        ),
        "answer_comparable_pairs": sum(
            answers[s]
            for s in (
                "supported_exact",
                "supported_format_normalized",
                "supported_semantic_safe",
                "unsupported",
            )
        ),
        "answer_cross_check_live": {
            "with_comparable_source_groups": sum(bool(r["source_matches"]) for r in rows),
            "all_groups_support_live": sum(
                bool(r["source_matches"])
                and all(
                    m["answer_cross_check"]["status"].startswith("supported_")
                    for m in r["source_matches"]
                )
                for r in rows
            ),
            "some_groups_support_some_disagree": sum(
                any(
                    m["answer_cross_check"]["status"].startswith("supported_")
                    for m in r["source_matches"]
                )
                and any(
                    m["answer_cross_check"]["status"] == "unsupported" for m in r["source_matches"]
                )
                for r in rows
            ),
            "no_groups_support_live": sum(
                bool(r["source_matches"])
                and all(
                    m["answer_cross_check"]["status"] == "unsupported" for m in r["source_matches"]
                )
                for r in rows
            ),
            "source_internal_multiple_answer_groups": sum(
                any(
                    m["answer_cross_check"].get("source_multiple_answers")
                    for m in r["source_matches"]
                )
                for r in rows
            ),
            "official_cross_category_disagreement": sum(
                len(
                    {
                        (m["memberships"][0]["dimension"], m["memberships"][0]["category"])
                        for m in r["source_matches"]
                    }
                )
                > 1
                and any(
                    m["answer_cross_check"]["status"] == "unsupported" for m in r["source_matches"]
                )
                and any(
                    m["answer_cross_check"]["status"].startswith("supported_")
                    for m in r["source_matches"]
                )
                for r in rows
            ),
            "formatting_only_differences": sum(
                any(
                    m["answer_cross_check"]["status"] == "supported_format_normalized"
                    for m in r["source_matches"]
                )
                for r in rows
            ),
            "genuine_potential_drift": sum(
                bool(r["source_matches"])
                and all(
                    m["answer_cross_check"]["status"] == "unsupported" for m in r["source_matches"]
                )
                for r in rows
            ),
        },
        "answer_drift_live_questions": sum(
            bool(r["source_matches"])
            and all(m["answer_cross_check"]["status"] == "unsupported" for m in r["source_matches"])
            for r in rows
        ),
        "image_drift_pairs": sum(
            m["image_changed"] is True for r in rows for m in r["source_matches"]
        ),
        "image_drift_live_questions": sum(
            any(m["image_changed"] is True for m in r["source_matches"]) for r in rows
        ),
        "wording_drift_pairs": sum(m["wording_changed"] for r in rows for m in r["source_matches"]),
        "wording_drift_live_questions": sum(
            any(m["wording_changed"] for m in r["source_matches"]) for r in rows
        ),
    }


def crosswalk(root: Path, snapshot_id: str | None = None) -> dict[str, Any]:
    from .mchs_reference_audit import legacy_comparison, order_audit
    from .mchs_reference_verify import verify_inventory, verify_parsed

    verify_inventory(root)
    verify_parsed(root)
    pointer = current_pointer(root)
    selected = snapshot_id or (pointer["snapshot_id"] if pointer else None)
    if selected is None:
        raise ValueError("No selected snapshot")
    snapshot = snapshot_path(root, selected)
    verify_snapshot(root, snapshot)
    corpus = root / "reference/mchs-categorized"
    parsed = corpus / "parsed"
    live = read_questions(snapshot / "questions.jsonl")
    pdf = [PublishedQuestion.model_validate(r) for r in read_records(parsed / "questions.jsonl")]
    paths = {
        r.sha256: root / "media/objects" / r.sha256 for q in live for r in q.resources if r.sha256
    }
    paths.update(
        {
            im["sha256"]: parsed / "media/objects" / im["sha256"]
            for q in pdf
            for im in q.illustration_refs
        }
    )
    descriptors = {}
    media_issues = []
    for sha, path in sorted(paths.items()):
        if file_hash(path) != sha:
            raise ValueError("Input media checksum mismatch")
        try:
            descriptors[sha] = image_descriptor(path)
        except (ValueError, OSError):
            media_issues.append({"sha256": sha, "issue": "unsupported_image_format"})
    rows = match_questions(live, pdf, descriptors)
    groups = build_reference_groups(pdf)
    # No legacy artifact is read until the independent result is complete.
    legacy = legacy_comparison(root, selected, rows)
    ordering = order_audit(rows)
    output = root / "derived" / selected / "mchs-categorized-crosswalk"
    safe_output(output, root)
    output.mkdir(parents=True, exist_ok=True)
    matched = {
        row_id
        for r in rows
        for m in r["source_matches"]
        for row_id in m.get("source_row_ids", [m["reference_id"]])
    }
    write_jsonl(output / "crosswalk.jsonl", rows)
    write_jsonl(output / "reference-groups.jsonl", [serializable_group(g) for g in groups])
    atomic_json(output / "taxonomy.json", read_json(parsed / "topics.json"))
    atomic_json(output / "legacy-comparison.json", legacy)
    atomic_json(output / "order-audit.json", ordering)
    for name, records in (
        ("unmatched-live", [r for r in rows if not r["classification_usable"]]),
        ("unmatched-reference", [q.model_dump() for q in pdf if q.reference_id not in matched]),
        ("ambiguous", [r for r in rows if r["match_status"] == "ambiguous"]),
        (
            "answer-drift",
            [
                {
                    "live_stable_key": r["live_stable_key"],
                    "live_position": r["live_position"],
                    "live_question_text": r["live_question_text"],
                    "match": m,
                }
                for r in rows
                for m in r["source_matches"]
                if m["answer_cross_check"]["status"] == "unsupported"
            ],
        ),
        ("review", [r for r in rows if r["candidates"] or not r["classification_usable"]]),
    ):
        atomic_json(output / f"{name}.json", {"records": records})
    summary = {
        "schema_version": VERSION,
        "snapshot_id": selected,
        "snapshot_questions_sha256": file_hash(snapshot / "questions.jsonl"),
        "parsed_questions_sha256": file_hash(parsed / "questions.jsonl"),
        **crosswalk_metrics(rows, pdf),
        "legacy_comparison": legacy,
        "order_audit": {k: v for k, v in ordering.items() if k != "records"},
        "media_comparison_issues": media_issues,
        "initial_unresolved": [
            {k: r[k] for k in ("live_stable_key", "official_id", "id_status", "match_status")}
            for r in rows
            if r["official_id"] is None
        ],
        "policy": {
            "authority": "live > categorized > legacy",
            "automatic_answer_replacement": False,
            "near_threshold": 0.97,
            "image_mean_rgb_error_max": 2,
            "image_dhash_distance_max": 2,
            "answer_difference_semantics": (
                "different means different under safe normalization, not a proven factual error"
            ),
        },
    }
    summary["artifacts"] = {
        p.name: file_hash(p)
        for p in sorted(output.iterdir())
        if p.name != "summary.json" and p.suffix in (".json", ".jsonl")
    }
    atomic_json(output / "summary.json", summary)
    return summary
