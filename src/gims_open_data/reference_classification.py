"""Deterministic offline topic classification against the GIMS reference PDF.

This module deliberately has separate semantics from ``reference_match``.  It
classifies a live question into published PDF sections; it does not claim that
the live question is the same content/version as a PDF row.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from .media import file_hash
from .models import Question
from .reference_match import image_dependent, media_evidence
from .reference_pdf import PdfQuestion, safe_output, write_jsonl
from .snapshot import current_pointer, read_json, read_questions, snapshot_path
from .storage import atomic_json

CLASSIFICATION_VERSION = "1.0"
USABLE_STATUSES = {"exact_topic", "strong_topic"}
OLD_NONUSABLE = {"probable", "ambiguous", "unmatched"}

_HOMOGLYPH_MAP: dict[str, str | int | None] = {
    "a": "а",
    "c": "с",
    "e": "е",
    "o": "о",
    "p": "р",
    "x": "х",
    "y": "у",
    "k": "к",
    "m": "м",
    "t": "т",
    "b": "в",
    "h": "н",
}
_HOMOGLYPHS = str.maketrans(_HOMOGLYPH_MAP)
_TYPO_MAP: dict[str, str | int | None] = {
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "„": '"',
    "–": "-",
    "—": "-",
    "‑": "-",
    "\u00ad": "",
}
_SHORT_TOKEN = re.compile(r"(?<![\wА-Яа-яЁё])([a-zа-яё]{1,3})(?![\wА-Яа-яЁё])", re.I)
_TERMINAL_PUNCTUATION = re.compile(r"[?!.…;:]+(?=\s*$)")
_NUMBER = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?:\s*[А-Яа-яA-Za-z%°/-]+)?", re.I)
_CRITICAL = re.compile(
    r"\b(?:не|нет|нельзя|запрещается|разрешается|"
    r"лев\w*|прав\w*|слева|справа|верх\w*|ниж\w*|увелич\w*|уменьш\w*|"
    r"положит\w*|отрицат\w*|гидроцикл\w*|парусн\w*|моторн\w*|"
    r"швертбот\w*|катер\w*|яхт\w*|"
    r"особ\w*\s+конструкц\w*|воздушн\w*\s+подушк\w*)\b",
    re.I,
)


def classification_normalized(text: str) -> str:
    """Apply classification-only technical normalization.

    The strict PDF normalization remains untouched.  This function preserves
    words, numbers, negation, direction and units; it only handles typography,
    terminal punctuation and short label-like homoglyph noise.
    """

    value = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    value = value.translate(str.maketrans(_TYPO_MAP))
    value = _TERMINAL_PUNCTUATION.sub("", value.strip())

    def replace_short(match: re.Match[str]) -> str:
        token = match.group(1)
        if any("a" <= char <= "z" for char in token):
            return token.translate(_HOMOGLYPHS)
        return token

    value = _SHORT_TOKEN.sub(replace_short, value)
    return " ".join(value.split())


def critical_signature(text: str) -> tuple[str, ...]:
    """Return semantic guard tokens whose changes block auto-classification."""

    value = classification_normalized(text)
    found: list[tuple[int, str]] = []
    for match in _NUMBER.finditer(value):
        found.append((match.start(), f"number:{match.group(0)}"))
    for match in _CRITICAL.finditer(value):
        found.append((match.start(), f"critical:{match.group(0)}"))
    return tuple(item for _, item in sorted(found))


def _answer_similarity(a: list[str], b: list[str]) -> tuple[float, float]:
    from rapidfuzz.fuzz import ratio

    if not a and not b:
        return 1.0, 1.0
    if len(a) == len(b) and len(a) <= 8:
        matrix = [[ratio(x, y) / 100 for y in b] for x in a]
        unused = set(range(len(b)))
        total = 0.0
        for row in matrix:
            index = max(unused, key=lambda item: row[item])
            total += row[index]
            unused.remove(index)
        score = total / max(len(a), 1)
    else:
        score = sum(ratio(x, y) / 100 for x, y in zip(sorted(a), sorted(b), strict=False)) / max(
            len(a), len(b), 1
        )
    order = sum(ratio(x, y) / 100 for x, y in zip(a, b, strict=False)) / max(len(a), len(b), 1)
    return score, order


def _topic_title_key(question: PdfQuestion) -> str:
    title = question.section_title or ""
    return classification_normalized(title)


def _section_key(question: PdfQuestion) -> tuple[str, str]:
    return question.section_code or "", _topic_title_key(question)


def _canonical_title_key(title: str | None, section_code: str | None = None) -> str:
    normalized = classification_normalized(title or "")
    if normalized:
        return normalized
    return f"[untitled section] {classification_normalized(section_code or '')}"


def _canonical_topic_id(normalized_title: str) -> str:
    digest = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:16]
    return f"topic_{digest}"


def build_canonical_taxonomy(pdf: list[PdfQuestion]) -> dict[str, Any]:
    """Build a stable taxonomy from observed PDF headings only.

    A canonical topic is keyed by the classification-only normalized heading.
    Section codes and prefixes remain source memberships and are never used as
    semantic topic identifiers.
    """

    grouped: dict[str, list[PdfQuestion]] = {}
    for question in pdf:
        key = _canonical_title_key(question.section_title, question.section_code)
        grouped.setdefault(key, []).append(question)

    topics: list[dict[str, Any]] = []
    for normalized_title, questions in sorted(grouped.items()):
        title_counts = Counter(question.section_title or "" for question in questions)
        display_title = sorted(title_counts, key=lambda title: (-title_counts[title], title))[0]
        memberships = sorted(
            {
                (
                    question.prefix,
                    question.section_code or "",
                    question.section_title or "",
                )
                for question in questions
            }
        )
        topics.append(
            {
                "canonical_topic_id": _canonical_topic_id(normalized_title),
                "slug": _canonical_topic_id(normalized_title),
                "display_title": display_title,
                "normalized_title": normalized_title,
                "memberships": [
                    {
                        "prefix": prefix,
                        "section_code": section_code,
                        "original_section_title": original_title,
                    }
                    for prefix, section_code, original_title in memberships
                ],
            }
        )
    return {
        "schema_version": "1.0",
        "topic_identity": "classification_normalized_observed_section_title",
        "topics": topics,
        "raw_section_titles": sorted({question.section_title or "" for question in pdf}),
        "raw_section_codes": sorted({question.section_code or "" for question in pdf}),
    }


def _taxonomy_index(taxonomy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(topic["normalized_title"]): topic for topic in taxonomy.get("topics", [])}


def _annotate_candidate(
    candidate: dict[str, Any], taxonomy_by_title: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    result = dict(candidate)
    normalized_title = _canonical_title_key(
        str(candidate.get("section_title") or ""),
        str(candidate.get("section_code") or ""),
    )
    topic = taxonomy_by_title.get(normalized_title)
    if topic is None:
        raise ValueError(f"Missing canonical topic for {normalized_title!r}")
    result["canonical_topic"] = {
        "canonical_topic_id": topic["canonical_topic_id"],
        "slug": topic["slug"],
        "display_title": topic["display_title"],
        "normalized_title": topic["normalized_title"],
    }
    result["source_membership"] = {
        "prefix": candidate.get("prefix") or "",
        "section_code": candidate.get("section_code") or "",
        "original_section_title": candidate.get("section_title") or "",
        "pdf_code": candidate.get("pdf_code") or "",
        "pdf_page": candidate.get("pdf_page"),
        "pdf_order": candidate.get("pdf_order"),
    }
    return result


def _media_hashes_live(question: Question) -> list[str]:
    return [resource.sha256 for resource in question.resources if resource.sha256]


def _media_hashes_pdf(question: PdfQuestion) -> list[str]:
    return [str(item["sha256"]) for item in question.illustration_refs if item.get("sha256")]


def _candidate_record(
    live: Question,
    pdf: PdfQuestion,
    descriptors: dict[str, dict[str, Any]],
    *,
    strict_source: str | None = None,
) -> dict[str, Any]:
    live_text = classification_normalized(live.text)
    pdf_text = classification_normalized(pdf.question_text)
    from rapidfuzz.fuzz import ratio

    question_score = ratio(live_text, pdf_text) / 100
    live_answers = [classification_normalized(answer.text) for answer in live.answers]
    pdf_answers = [classification_normalized(answer.text) for answer in pdf.answers]
    answer_score, answer_order = _answer_similarity(live_answers, pdf_answers)
    media = media_evidence(_media_hashes_live(live), _media_hashes_pdf(pdf), descriptors)
    image_needed = (
        image_dependent(live_text, live_answers)
        or image_dependent(pdf_text, pdf_answers)
        or bool(pdf.graphic_refs)
    )
    exact_text = live_text == pdf_text
    critical_equal = critical_signature(live.text) == critical_signature(pdf.question_text)
    safe_answers_equal = sorted(live_answers) == sorted(pdf_answers)
    answers_exact = live_answers == pdf_answers
    strict_image = bool(media.get("identity") or media.get("strict_visual_support"))
    reasons: list[str] = []
    if exact_text:
        reasons.append("exact_question_text")
    else:
        reasons.append("near_question_text")
    if not critical_equal:
        reasons.append("critical_tokens_changed")
    if not safe_answers_equal:
        reasons.append("answers_changed")
    elif live.answers != []:
        reasons.append("answers_safe_normalized_equal")
    if strict_image:
        reasons.append("strict_visual_support")
    if image_needed and not strict_image:
        reasons.append("image_support_missing_or_mismatch")
    if strict_source:
        reasons.append(f"strict_candidate:{strict_source}")
    return {
        "pdf_code": pdf.code,
        "pdf_page": pdf.page,
        "pdf_order": pdf.order,
        "pdf_source": pdf.source,
        "prefix": pdf.prefix,
        "section_code": pdf.section_code,
        "section_title": pdf.section_title,
        "confidence": round(0.7 * question_score + 0.3 * answer_score, 6),
        "similarity": {
            "question_text": round(question_score, 6),
            "answer_set": round(answer_score, 6),
            "answer_order": round(answer_order, 6),
        },
        "media_evidence": media,
        "image_dependent": image_needed,
        "image_artifact_present": bool(
            _media_hashes_live(live) or _media_hashes_pdf(pdf) or pdf.graphic_refs
        ),
        "strict_visual_support": strict_image,
        "critical_tokens_equal": critical_equal,
        "safe_answers_equal": safe_answers_equal,
        "answers_exact": answers_exact,
        "exact_question_text": exact_text,
        "evidence_reasons": reasons,
        "pdf_issues": pdf.issues,
    }


def _topic_keys(candidates: list[dict[str, Any]]) -> set[str]:
    return {
        classification_normalized(str(candidate.get("section_title") or ""))
        or f"section:{candidate.get('section_code') or ''}"
        for candidate in candidates
    }


def _eligible_image_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    supported = [candidate for candidate in candidates if candidate["strict_visual_support"]]
    if supported and len(_topic_keys(supported)) == 1:
        return supported
    return []


def _near_usable(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    candidates = sorted(
        candidates,
        key=lambda candidate: (
            -candidate["similarity"]["question_text"],
            -candidate["similarity"]["answer_set"],
            candidate["pdf_order"],
        ),
    )
    best_question = candidates[0]["similarity"]["question_text"]
    best = [
        candidate
        for candidate in candidates
        if candidate["similarity"]["question_text"] >= best_question - 0.025
    ]
    strong = [
        candidate
        for candidate in best
        if candidate["critical_tokens_equal"]
        and candidate["similarity"]["question_text"] >= 0.86
        and (
            candidate["similarity"]["answer_set"] >= 0.88
            or candidate["safe_answers_equal"]
            or candidate["strict_visual_support"]
        )
        and not (candidate["image_dependent"] and not candidate["strict_visual_support"])
    ]
    if not strong or len(_topic_keys(strong)) != 1:
        return []
    return strong


def _classify_one(
    live: Question,
    pdf: list[PdfQuestion],
    descriptors: dict[str, dict[str, Any]],
    strict_orders: dict[int, str],
) -> dict[str, Any]:
    from rapidfuzz import process
    from rapidfuzz.fuzz import ratio

    texts = [classification_normalized(question.question_text) for question in pdf]
    live_text = classification_normalized(live.text)
    exact_indexes = {index for index, text in enumerate(texts) if text == live_text}
    near_indexes = {
        index
        for _, score, index in process.extract(
            live_text, texts, scorer=ratio, limit=80, score_cutoff=55
        )
        if score >= 55
    }
    strict_indexes = {
        index for index, question in enumerate(pdf) if question.order in strict_orders
    }
    indexes = sorted(exact_indexes | near_indexes | strict_indexes)
    evaluated = [
        _candidate_record(
            live,
            pdf[index],
            descriptors,
            strict_source=strict_orders.get(pdf[index].order),
        )
        for index in indexes
    ]
    evaluated = [
        candidate for candidate in evaluated if candidate["similarity"]["question_text"] >= 0.55
    ]
    evaluated.sort(
        key=lambda candidate: (
            -candidate["similarity"]["question_text"],
            -candidate["similarity"]["answer_set"],
            candidate["pdf_order"],
        )
    )
    exact = [candidate for candidate in evaluated if candidate["exact_question_text"]]
    exact_safe = [candidate for candidate in exact if candidate["critical_tokens_equal"]]
    supported_exact = _eligible_image_candidates(exact_safe)
    accepted: list[dict[str, Any]] = []
    status = "unmatched"
    reasons: list[str] = []
    if supported_exact:
        accepted = supported_exact
        status = "exact_topic"
        reasons.extend(["exact_question_text", "strict_visual_support"])
    elif exact_safe:
        topic_keys = _topic_keys(exact_safe)
        image_blocked = any(
            candidate["image_dependent"] and not candidate["strict_visual_support"]
            for candidate in exact_safe
        )
        if len(topic_keys) == 1 and not image_blocked:
            accepted = exact_safe
            status = "exact_topic"
            reasons.append("exact_question_text")
        elif len(topic_keys) > 1:
            reasons.append("competing_section_topics")
        if image_blocked:
            reasons.append("image_dependent_variant_not_supported")

    exact_image_conflict = (
        not supported_exact
        and bool(exact_safe)
        and any(
            candidate["image_dependent"] and not candidate["strict_visual_support"]
            for candidate in exact_safe
        )
    )
    if exact_image_conflict:
        status = "ambiguous"
        reasons.append("image_dependent_variant_not_supported")
    if not accepted and not exact_image_conflict:
        near = [
            candidate
            for candidate in evaluated
            if candidate["critical_tokens_equal"]
            and candidate["similarity"]["question_text"] >= 0.70
        ]
        accepted = _near_usable(near)
        if accepted:
            status = "strong_topic"
            reasons.extend(["near_question_text", "strong_corroboration"])
        elif near:
            near_topics = _topic_keys(
                [
                    candidate
                    for candidate in near
                    if candidate["similarity"]["question_text"]
                    >= near[0]["similarity"]["question_text"] - 0.025
                ]
            )
            status = "ambiguous" if len(near_topics) > 1 else "probable"
            reasons.append(
                "competing_section_topics" if len(near_topics) > 1 else "insufficient_corroboration"
            )
        elif evaluated:
            status = "probable"
            reasons.append("critical_tokens_changed_or_low_similarity")
    for candidate in evaluated:
        candidate["accepted"] = candidate in accepted
        candidate["rejection_reasons"] = []
        if not candidate["accepted"]:
            if not candidate["critical_tokens_equal"]:
                candidate["rejection_reasons"].append("critical_tokens_changed")
            if candidate["image_dependent"] and not candidate["strict_visual_support"]:
                candidate["rejection_reasons"].append("image_dependent_image_mismatch")
            if not candidate["rejection_reasons"]:
                candidate["rejection_reasons"].append("not_in_conservative_best_set")
    accepted_codes = [candidate["pdf_code"] for candidate in accepted]
    exact_answer_drift = any(
        candidate["exact_question_text"] and not candidate["safe_answers_equal"]
        for candidate in accepted
    )
    strict_image = any(candidate["strict_visual_support"] for candidate in accepted)
    safe_answer_differences = any(
        candidate["similarity"]["question_text"] < 1 and candidate["safe_answers_equal"]
        for candidate in accepted
    )
    recovery_reasons: list[str] = []
    if exact_answer_drift:
        recovery_reasons.append("exact question text despite answer drift")
    if strict_image:
        recovery_reasons.append("exact text + strict image")
    if safe_answer_differences:
        recovery_reasons.append("safe-normalized answer differences")
    if accepted and not recovery_reasons and status == "strong_topic":
        recovery_reasons.append("near wording + strong corroboration")
    if accepted and not recovery_reasons:
        recovery_reasons.append("other")
    return {
        "live_stable_key": live.stable_key,
        "official_id": str(live.official_id) if live.official_id else None,
        "id_status": live.id_status,
        "live_position": live.position,
        "live_question_text": live.text,
        "original_content_status": strict_orders.get(-1, "unknown"),
        "classification_status": status,
        "classification_usable": status in USABLE_STATUSES,
        "evidence_reasons": sorted(set(reasons)),
        "recovery_reasons": recovery_reasons,
        "accepted_pdf_codes": accepted_codes,
        "matches": accepted,
        "candidate_alternatives": [
            candidate for candidate in evaluated if not candidate["accepted"]
        ],
        "semantic_danger_rejections": [
            candidate
            for candidate in evaluated
            if "critical_tokens_changed" in candidate["rejection_reasons"]
        ],
    }


def classify_questions(
    live: list[Question],
    pdf: list[PdfQuestion],
    descriptors: dict[str, dict[str, Any]] | None = None,
    strict_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Classify every live question without changing strict matcher output."""

    descriptors = descriptors or {}
    strict_by_key = {
        str(record.get("live_stable_key")): record for record in (strict_records or [])
    }
    strict_orders_by_live: dict[str, dict[int, str]] = {}
    for record in strict_by_key.values():
        orders: dict[int, str] = {}
        for candidate in record.get("matches", []) + record.get("candidates", []):
            order = candidate.get("pdf_order")
            if isinstance(order, int):
                orders[order] = str(record.get("match_status", "unknown"))
        strict_orders_by_live[str(record.get("live_stable_key"))] = orders
    results: list[dict[str, Any]] = []
    for question in live:
        record = _classify_one(
            question,
            pdf,
            descriptors,
            strict_orders_by_live.get(question.stable_key, {}),
        )
        strict = strict_by_key.get(question.stable_key, {})
        record["original_content_status"] = str(strict.get("match_status", "unknown"))
        record["original_content_usable"] = str(strict.get("match_status")) in {"exact", "strong"}
        results.append(record)
    return results


def _candidate_identity(candidate: dict[str, Any]) -> tuple[int, str]:
    return int(candidate.get("pdf_order", 0)), str(candidate.get("pdf_code", ""))


def _all_record_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[int, str], dict[str, Any]] = {}
    for candidate in record.get("matches", []) + record.get("candidate_alternatives", []):
        by_identity[_candidate_identity(candidate)] = candidate
    return [by_identity[key] for key in sorted(by_identity)]


def _topic_group_summary(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        topic = candidate["canonical_topic"]
        groups.setdefault(str(topic["canonical_topic_id"]), []).append(candidate)
    summaries: list[dict[str, Any]] = []
    for _topic_id, members in groups.items():
        topic = members[0]["canonical_topic"]
        summaries.append(
            {
                "canonical_topic": topic,
                "support": {
                    "pdf_positions": sorted({candidate["pdf_order"] for candidate in members}),
                    "pdf_codes": sorted({candidate["pdf_code"] for candidate in members}),
                    "prefixes": sorted({candidate["prefix"] for candidate in members}),
                    "section_codes": sorted({candidate["section_code"] for candidate in members}),
                },
                "source_memberships": [
                    candidate["source_membership"]
                    for candidate in sorted(members, key=_candidate_identity)
                ],
            }
        )
    return sorted(
        summaries,
        key=lambda summary: (
            -len(summary["support"]["prefixes"]),
            -len(summary["support"]["pdf_positions"]),
            summary["canonical_topic"]["normalized_title"],
        ),
    )


def _image_version_blocked(candidates: list[dict[str, Any]], topic_count: int) -> bool:
    unsupported = [
        candidate
        for candidate in candidates
        if candidate["image_dependent"]
        and candidate.get("image_artifact_present", False)
        and (
            not candidate["strict_visual_support"]
            or candidate["media_evidence"].get("kind") == "both_absent"
        )
    ]
    if not unsupported:
        return False
    # A single text topic can still be classified thematically when the image
    # is merely a version detail.  Competing image-bearing topics, or no strict
    # visual support at all, remain blocked.
    return topic_count > 1 or not any(
        candidate["strict_visual_support"]
        and candidate.get("image_artifact_present", False)
        and candidate["media_evidence"].get("kind") != "both_absent"
        for candidate in candidates
    )


def refine_classification(
    records: list[dict[str, Any]],
    pdf: list[PdfQuestion],
    strict_records: list[dict[str, Any]],
    taxonomy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply exact-only canonical topic consensus over baseline results.

    The baseline classifier remains the source of near/fuzzy decisions.  This
    refinement can change a row only when the question text is exact and its
    critical signature is unchanged.  It treats repeated published branches as
    source evidence, while retaining every source membership in the output.
    """

    del pdf  # The taxonomy and candidate records already contain all needed rows.
    taxonomy_by_title = _taxonomy_index(taxonomy)
    strict_by_key = {str(record.get("live_stable_key")): record for record in strict_records}
    refined: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        record["pre_refinement_classification_status"] = record["classification_status"]
        record["pre_refinement_classification_usable"] = record["classification_usable"]
        all_candidates = [
            _annotate_candidate(candidate, taxonomy_by_title)
            for candidate in _all_record_candidates(record)
        ]
        by_identity = {_candidate_identity(candidate): candidate for candidate in all_candidates}
        record["matches"] = [
            by_identity[_candidate_identity(candidate)]
            for candidate in record.get("matches", [])
            if _candidate_identity(candidate) in by_identity
        ]
        record["candidate_alternatives"] = [
            by_identity[_candidate_identity(candidate)]
            for candidate in record.get("candidate_alternatives", [])
            if _candidate_identity(candidate) in by_identity
        ]
        exact_candidates = [
            candidate
            for candidate in all_candidates
            if candidate["exact_question_text"] and candidate["critical_tokens_equal"]
        ]
        exact_groups = _topic_group_summary(exact_candidates)
        strict = strict_by_key.get(record["live_stable_key"], {})
        strict_usable = str(strict.get("match_status")) in {"exact", "strong"}
        record["source_taxonomy_conflict"] = False
        record["primary_canonical_topic"] = None
        record["canonical_topic_candidates"] = _topic_group_summary(all_candidates)
        record["conflicting_memberships"] = []
        record["refinement_applied"] = False
        record["refinement_reasons"] = []

        if exact_candidates:
            record["canonical_topic_candidates"] = exact_groups
            image_blocked = _image_version_blocked(exact_candidates, len(exact_groups))
            baseline_visual_safe = bool(
                record["pre_refinement_classification_usable"]
                and any(candidate["strict_visual_support"] for candidate in record["matches"])
            )
            if image_blocked and baseline_visual_safe:
                accepted_groups = _topic_group_summary(record["matches"])
                record["canonical_topic_status"] = (
                    "single_topic" if len(accepted_groups) == 1 else "multi_topic_confirmed"
                )
                if accepted_groups:
                    record["primary_canonical_topic"] = accepted_groups[0]["canonical_topic"]
                record["refinement_reasons"].append("preserved_baseline_strict_visual_support")
                refined.append(record)
                continue
            if image_blocked:
                record["canonical_topic_status"] = "image_version_ambiguous"
                record["classification_status"] = "ambiguous"
                record["classification_usable"] = False
                record["refinement_reasons"].append("image_dependent_variant_not_supported")
            elif len(exact_groups) == 1:
                record["canonical_topic_status"] = "single_topic"
                record["primary_canonical_topic"] = exact_groups[0]["canonical_topic"]
                record["classification_status"] = "exact_topic"
                record["classification_usable"] = True
                record["refinement_applied"] = True
                record["refinement_reasons"].append("exact_question_canonical_topic")
            else:
                supported = [
                    group for group in exact_groups if len(group["support"]["prefixes"]) >= 2
                ]
                singleton_competitors = all(
                    len(group["support"]["prefixes"]) == 1
                    and len(group["support"]["pdf_positions"]) == 1
                    for group in exact_groups
                    if group not in supported
                )
                if len(supported) == 1 and singleton_competitors:
                    record["canonical_topic_status"] = "source_taxonomy_conflict"
                    record["primary_canonical_topic"] = supported[0]["canonical_topic"]
                    record["source_taxonomy_conflict"] = True
                    record["conflicting_memberships"] = [
                        membership
                        for group in exact_groups
                        if group is not supported[0]
                        for membership in group["source_memberships"]
                    ]
                    record["refinement_reasons"].append("published_branch_consensus")
                else:
                    record["canonical_topic_status"] = "multi_topic_confirmed"
                    if len(supported) == 1:
                        record["primary_canonical_topic"] = supported[0]["canonical_topic"]
                    record["refinement_reasons"].append("exact_many_to_many_topics")
                record["classification_status"] = "exact_topic"
                record["classification_usable"] = True
                record["refinement_applied"] = True

            if record["classification_usable"]:
                for candidate in all_candidates:
                    candidate["accepted"] = candidate in exact_candidates
                record["matches"] = exact_candidates
                record["candidate_alternatives"] = [
                    candidate for candidate in all_candidates if candidate not in exact_candidates
                ]
                record["accepted_pdf_codes"] = [
                    candidate["pdf_code"]
                    for candidate in sorted(exact_candidates, key=_candidate_identity)
                ]
                if strict_usable:
                    record["refinement_reasons"].append("strict_content_usable_support")
        else:
            accepted_topics = _topic_group_summary(record["matches"])
            if record["classification_usable"]:
                if len(accepted_topics) <= 1:
                    record["canonical_topic_status"] = "single_topic"
                    if accepted_topics:
                        record["primary_canonical_topic"] = accepted_topics[0]["canonical_topic"]
                else:
                    record["canonical_topic_status"] = "multi_topic_confirmed"
            elif record["classification_status"] == "ambiguous":
                has_image_reason = any(
                    reason
                    in {
                        "image_dependent_variant_not_supported",
                        "image_support_missing_or_mismatch",
                    }
                    for reason in record["evidence_reasons"]
                )
                record["canonical_topic_status"] = (
                    "image_version_ambiguous" if has_image_reason else "semantic_ambiguous"
                )
            else:
                record["canonical_topic_status"] = record["classification_status"]
            record["refinement_reasons"].append("consensus_not_applied_to_fuzzy")
        refined.append(record)
    return refined


def _load_pdf(parsed: Path) -> tuple[list[PdfQuestion], dict[str, Any]]:
    questions = [
        PdfQuestion.model_validate_json(line)
        for line in (parsed / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = read_json(parsed / "summary.json")
    for name, sha in summary.get("artifacts", {}).items():
        if name not in {"questions.jsonl", "sections.json", "parse-issues.json"}:
            raise ValueError("Unexpected parsed artifact")
        if file_hash(parsed / name) != sha:
            raise ValueError("Parsed artifact checksum mismatch")
    source_hashes = {question.source.get("sha256_of_pdf") for question in questions}
    if len(source_hashes) > 1:
        raise ValueError("Mixed PDF provenance")
    return questions, summary


def _load_descriptors(
    root: Path, live: list[Question], pdf: list[PdfQuestion], pdf_media: Path
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    from .reference_match import image_descriptor

    paths = {
        resource.sha256: root / "media/objects" / resource.sha256
        for question in live
        for resource in question.resources
        if resource.sha256
    }
    paths.update(
        {
            str(item["sha256"]): pdf_media / str(item["sha256"])
            for question in pdf
            for item in question.illustration_refs
            if item.get("sha256")
        }
    )
    descriptors: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, str]] = []
    for sha, path in sorted(paths.items()):
        try:
            if file_hash(path) != sha:
                raise ValueError("Media checksum mismatch")
            descriptors[sha] = image_descriptor(path)
        except (OSError, ValueError) as exc:
            issues.append({"sha256": sha, "error": str(exc)})
    return descriptors, issues


def _compact_sample(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "live_position": record["live_position"],
        "original_content_status": record["original_content_status"],
        "classification_status": record["classification_status"],
        "classification_usable": record["classification_usable"],
        "canonical_topic_status": record.get("canonical_topic_status"),
        "primary_canonical_topic": record.get("primary_canonical_topic"),
        "source_taxonomy_conflict": record.get("source_taxonomy_conflict", False),
        "live_question_text": record["live_question_text"],
        "evidence_reasons": record["evidence_reasons"],
        "accepted_pdf_codes": record["accepted_pdf_codes"],
        "section_codes": sorted({match["section_code"] for match in record["matches"]}),
        "candidate_alternatives": [
            {
                "pdf_code": candidate["pdf_code"],
                "section_code": candidate["section_code"],
                "section_title": candidate["section_title"],
                "canonical_topic": candidate.get("canonical_topic"),
                "rejection_reasons": candidate.get("rejection_reasons", []),
            }
            for candidate in record["candidate_alternatives"][:3]
        ],
    }


def classify(root: Path, parsed: Path, pdf_media: Path) -> dict[str, Any]:
    """Run the offline classifier and write only ignored derived artifacts."""

    pointer = current_pointer(root)
    if pointer is None:
        raise ValueError("No current snapshot")
    snapshot = snapshot_path(root, pointer["snapshot_id"])
    manifest = read_json(snapshot / "manifest.json")
    live_path = snapshot / "questions.jsonl"
    if file_hash(live_path) != manifest["artifacts"]["questions.jsonl"]:
        raise ValueError("Current questions checksum mismatch")
    live = read_questions(live_path)
    pdf, parse_summary = _load_pdf(parsed)
    strict_output = root / "derived" / pointer["snapshot_id"] / "reference-crosswalk"
    strict_path = strict_output / "crosswalk.jsonl"
    if not strict_path.exists():
        raise ValueError("Existing strict crosswalk is required")
    strict_records = [
        json.loads(line)
        for line in strict_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    descriptors, media_issues = _load_descriptors(root, live, pdf, pdf_media)
    taxonomy = build_canonical_taxonomy(pdf)
    baseline_records = classify_questions(live, pdf, descriptors, strict_records)
    records = refine_classification(baseline_records, pdf, strict_records, taxonomy)
    output = root / "derived" / pointer["snapshot_id"] / "reference-classification"
    safe_output(output, root)
    output.mkdir(parents=True, exist_ok=True)

    counts = Counter(record["classification_status"] for record in records)
    baseline_counts = Counter(record["pre_refinement_classification_status"] for record in records)
    usable = [record for record in records if record["classification_usable"]]
    strict_status_counts = {
        status: sum(record["original_content_status"] == status for record in records)
        for status in ("exact", "strong", "probable", "ambiguous", "unmatched")
    }
    strict_usable = strict_status_counts["exact"] + strict_status_counts["strong"]
    recovered = [record for record in usable if record["original_content_status"] in OLD_NONUSABLE]
    pre_refinement_recovered = [
        record
        for record in records
        if record["pre_refinement_classification_usable"]
        and record["original_content_status"] in OLD_NONUSABLE
    ]
    downgrade_rows = [
        record
        for record in records
        if record["original_content_usable"] and not record["pre_refinement_classification_usable"]
    ]
    recovered_downgrades = [record for record in downgrade_rows if record["classification_usable"]]
    recovery_by_old = {
        status: sum(record["original_content_status"] == status for record in recovered)
        for status in sorted(OLD_NONUSABLE)
    }
    reason_names = [
        "exact question text despite answer drift",
        "exact text + strict image",
        "safe-normalized answer differences",
        "near wording + strong corroboration",
        "other",
    ]
    recovery_by_reason = {
        name: sum(name in record["recovery_reasons"] for record in recovered)
        for name in reason_names
    }
    usable_codes = [code for record in usable for code in record["accepted_pdf_codes"]]
    usable_code_sets = [set(record["accepted_pdf_codes"]) for record in usable]
    usable_sections = [
        {
            (match["section_code"], classification_normalized(str(match["section_title"] or "")))
            for match in record["matches"]
        }
        for record in usable
    ]
    one_code = sum(len(codes) == 1 for codes in usable_code_sets)
    multiple_code = sum(len(codes) > 1 for codes in usable_code_sets)
    multi_section = sum(len(sections) > 1 for sections in usable_sections)
    multiple_codes_one_section = sum(
        len(codes) > 1 and len(sections) == 1
        for codes, sections in zip(usable_code_sets, usable_sections, strict=True)
    )
    exact_text_answer_drift = sum(
        "exact question text despite answer drift" in record["recovery_reasons"]
        for record in records
    )
    semantic_danger = [
        record
        for record in records
        if not record["classification_usable"] and record["semantic_danger_rejections"]
    ]
    semantic_danger_candidates = sum(
        len(record["semantic_danger_rejections"]) for record in semantic_danger
    )
    canonical_status_counts = Counter(record["canonical_topic_status"] for record in records)
    canonical_usable_counts = Counter(
        record["canonical_topic_status"] for record in records if record["classification_usable"]
    )
    summary = {
        "schema_version": CLASSIFICATION_VERSION,
        "refinement_version": "1.0",
        "snapshot_id": pointer["snapshot_id"],
        "snapshot_questions_sha256": file_hash(live_path),
        "pdf_source": parse_summary["source"],
        "parsed_questions_sha256": file_hash(parsed / "questions.jsonl"),
        "live_total": len(live),
        "existing_strict_usable": strict_usable,
        "existing_strict_status_counts": strict_status_counts,
        "classification_status_counts": {
            status: counts.get(status, 0)
            for status in (*USABLE_STATUSES, "probable", "ambiguous", "unmatched")
        },
        "pre_refinement_classification_status_counts": {
            status: baseline_counts.get(status, 0)
            for status in (*USABLE_STATUSES, "probable", "ambiguous", "unmatched")
        },
        "classification_usable_before_refinement": sum(
            record["pre_refinement_classification_usable"] for record in records
        ),
        "classification_usable": len(usable),
        "classification_usable_percentage": round(100 * len(usable) / max(len(records), 1), 2),
        "recovered_from_old_nonusable": len(recovered),
        "pre_refinement_recovered_from_old_nonusable": len(pre_refinement_recovered),
        "old_nonusable_remaining_after_refinement": sum(
            record["original_content_status"] in OLD_NONUSABLE
            and not record["classification_usable"]
            for record in records
        ),
        "recovered_from_old_status": recovery_by_old,
        "recovery_by_reason": recovery_by_reason,
        "exact_question_text_despite_answer_drift": exact_text_answer_drift,
        "old_usable_downgrade_audit": {
            "downgrade_rows": len(downgrade_rows),
            "recovered_by_canonical_refinement": len(recovered_downgrades),
            "recovered_statuses": Counter(
                record["canonical_topic_status"] for record in recovered_downgrades
            ),
            "still_nonusable_statuses": Counter(
                record["canonical_topic_status"]
                for record in downgrade_rows
                if not record["classification_usable"]
            ),
            "image_version_blocked": sum(
                record["canonical_topic_status"] == "image_version_ambiguous"
                for record in downgrade_rows
                if not record["classification_usable"]
            ),
        },
        "reconciliation": {
            "old_usable_final_usable": sum(
                record["original_content_usable"] and record["classification_usable"]
                for record in records
            ),
            "old_usable_final_nonusable": sum(
                record["original_content_usable"] and not record["classification_usable"]
                for record in records
            ),
            "old_nonusable_final_usable": len(recovered),
            "old_nonusable_final_nonusable": sum(
                record["original_content_status"] in OLD_NONUSABLE
                and not record["classification_usable"]
                for record in records
            ),
            "formula": (
                "old usable final usable + old usable final nonusable + "
                "old nonusable final usable + old nonusable final nonusable = live total"
            ),
        },
        "canonical_topic_metrics": {
            "total_canonical_topics": len(taxonomy["topics"]),
            "raw_section_titles": len(taxonomy["raw_section_titles"]),
            "raw_section_codes": len(taxonomy["raw_section_codes"]),
            "single_topic_usable": canonical_usable_counts.get("single_topic", 0),
            "multi_topic_confirmed_usable": canonical_usable_counts.get("multi_topic_confirmed", 0),
            "source_taxonomy_conflict_usable": canonical_usable_counts.get(
                "source_taxonomy_conflict", 0
            ),
            "semantic_ambiguous": canonical_status_counts.get("semantic_ambiguous", 0),
            "image_version_ambiguous": canonical_status_counts.get("image_version_ambiguous", 0),
            "probable": canonical_status_counts.get("probable", 0),
            "unmatched": canonical_status_counts.get("unmatched", 0),
            "canonical_topic_status_counts": dict(sorted(canonical_status_counts.items())),
        },
        "pdf_mapping_metrics": {
            "usable_rows_with_one_pdf_code": one_code,
            "usable_rows_with_multiple_pdf_codes": multiple_code,
            "usable_rows_with_one_section": sum(len(sections) == 1 for sections in usable_sections),
            "distinct_section_code": len(
                {match["section_code"] for record in usable for match in record["matches"]}
            ),
            "multiple_pdf_codes_but_one_section": multiple_codes_one_section,
            "real_multi_section_classifications": multi_section,
            "distinct_accepted_pdf_codes": len(set(usable_codes)),
        },
        "semantic_danger_nonaccepted_rows": len(semantic_danger),
        "semantic_danger_candidate_rejections": semantic_danger_candidates,
        "media_comparison_issues": media_issues,
        "policy": {
            "usable_statuses": sorted(USABLE_STATUSES),
            "strict_matcher_unchanged": True,
            "correctness_from_pdf_used": False,
            "pdf_code_is_unique_topic_id": False,
            "critical_changes_block_auto": [
                "numbers",
                "negation",
                "direction",
                "increase/decrease",
                "vessel type",
                "polarity",
            ],
            "image_policy": (
                "strict image support can disambiguate; ordinary visual support is auxiliary; "
                "image-dependent mismatch blocks auto-accept"
            ),
            "score": "0.7 question text + 0.3 answer set; not a probability",
            "canonical_topic_identity": "classification-normalized observed section title",
            "consensus_shortcut": (
                "exact question text only; repeated prefixes may resolve singleton source taxonomy "
                "conflicts; fuzzy candidates never use this shortcut"
            ),
        },
        "samples": {
            "recovered": [_compact_sample(record) for record in recovered[:10]],
            "ambiguous": [
                _compact_sample(record)
                for record in records
                if record["classification_status"] == "ambiguous"
            ][:10],
            "unmatched": [
                _compact_sample(record)
                for record in records
                if record["classification_status"] == "unmatched"
            ][:10],
            "semantic_danger": [_compact_sample(record) for record in semantic_danger[:10]],
        },
    }
    write_jsonl(output / "classification.jsonl", records)
    atomic_json(output / "canonical-topics.json", taxonomy)
    atomic_json(output / "summary.json", summary)
    atomic_json(
        output / "review.json",
        {"records": [record for record in records if not record["classification_usable"]]},
    )
    atomic_json(
        output / "ambiguous.json",
        {
            "records": [
                record for record in records if record["classification_status"] == "ambiguous"
            ]
        },
    )
    atomic_json(
        output / "unmatched.json",
        {
            "records": [
                record for record in records if record["classification_status"] == "unmatched"
            ]
        },
    )
    return summary
