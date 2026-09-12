"""Deterministic, answer-aware many-to-many reference crosswalk. No network."""

import hashlib
import itertools
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .media import file_hash
from .models import Question
from .reference_pdf import (
    PdfQuestion,
    content_key,
    normalized,
    safe_output,
    write_jsonl,
)
from .snapshot import current_pointer, read_json, read_questions, snapshot_path
from .storage import atomic_json

MATCH_VERSION = "1.0"
USABLE = {"exact", "strong"}


def image_descriptor(path: Path) -> dict[str, Any]:
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        im = ImageOps.exif_transpose(opened).convert("RGB")
        small = im.resize((32, 32), Image.Resampling.LANCZOS)
        gray = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        values = list(gray.tobytes())
        bits = sum(
            (values[y * 9 + x] > values[y * 9 + x + 1]) << (y * 8 + x)
            for y in range(8)
            for x in range(8)
        )
        return {
            "pixel_sha256": hashlib.sha256(str(im.size).encode() + im.tobytes()).hexdigest(),
            "aspect": im.width / im.height,
            "thumbnail": list(small.tobytes()),
            "dhash": bits,
        }


def media_evidence(
    live: list[str], pdf: list[str], descriptors: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not live and not pdf:
        return {"kind": "both_absent", "identity": True, "visual_support": True}
    if live and pdf and sorted(live) == sorted(pdf):
        return {"kind": "bytes_equal", "identity": True, "visual_support": True}
    if len(live) != 1 or len(pdf) != 1 or not all(h in descriptors for h in live + pdf):
        return {
            "kind": "unavailable" if live and pdf else "presence_changed",
            "identity": False,
            "visual_support": False,
        }
    a, b = descriptors[live[0]], descriptors[pdf[0]]
    if a["pixel_sha256"] == b["pixel_sha256"]:
        return {"kind": "pixels_equal", "identity": True, "visual_support": True}
    error = sum(abs(x - y) for x, y in zip(a["thumbnail"], b["thumbnail"], strict=True)) / 3072
    distance = (a["dhash"] ^ b["dhash"]).bit_count()
    aspect = abs(a["aspect"] / b["aspect"] - 1)
    return {
        "kind": "visual_comparison",
        "identity": False,
        "visual_support": error <= 8 and distance <= 6,
        "strict_visual_support": error <= 2 and distance <= 2,
        "mean_rgb_error": round(error, 4),
        "dhash_distance": distance,
        "aspect_relative_difference": round(aspect, 6),
    }


def answer_similarity(a: list[str], b: list[str]) -> tuple[float, float, list[int]]:
    from rapidfuzz.fuzz import ratio

    order = sum(ratio(x, y) / 100 for x, y in zip(a, b, strict=False)) / max(len(a), len(b), 1)
    if len(a) == len(b) and len(a) <= 7:
        matrix = [[ratio(x, y) / 100 for y in b] for x in a]
        permutation = max(
            itertools.permutations(range(len(b))),
            key=lambda p: sum(matrix[i][j] for i, j in enumerate(p)),
        )
        score = sum(matrix[i][j] for i, j in enumerate(permutation)) / max(len(a), 1)
        return score, order, list(permutation)
    return (
        sum(ratio(x, y) / 100 for x, y in zip(sorted(a), sorted(b), strict=False))
        / max(len(a), len(b), 1),
        order,
        [],
    )


def critical_tokens(text: str) -> list[str]:
    return re.findall(
        r"\d+(?:[.,]\d+)?|\b(?:не|нет|нельзя|запрещается|разрешается|"
        r"лев\w*|прав\w*|верх\w*|ниж\w*|увелич\w*|уменьш\w*)\b",
        text,
    )


def image_dependent(text: str, answers: list[str]) -> bool:
    return bool(
        re.search(r"рисунк|иллюстрац|изображ|показан|на схем|обозначен|этих судов", text)
        or (answers and all(re.fullmatch(r"[а-яa-z0-9)()., /-]{1,12}", a) for a in answers))
    )


def differences(
    live: Question, pdf: PdfQuestion, mapping: list[int], media: dict[str, Any]
) -> dict[str, Any]:
    la = [normalized(a.text) for a in live.answers]
    pa = [normalized(a.text) for a in pdf.answers]
    comparable = (
        pdf.correct_answer_indexes is not None
        and len(mapping) == len(la)
        and len(la) == len(pa)
        and len(set(mapping)) == len(mapping)
    )
    correct_changed = (
        (
            set(mapping[i] for i, a in enumerate(live.answers) if a.correct)
            != set(pdf.correct_answer_indexes or [])
        )
        if comparable
        else None
    )
    image_changed: bool | None = None
    if media["identity"]:
        image_changed = False
    elif media["kind"] == "presence_changed":
        image_changed = True
    elif media["kind"] == "visual_comparison" and not media["visual_support"]:
        image_changed = True
    return {
        "question_text_changed": normalized(live.text) != normalized(pdf.question_text),
        "answers_text_changed": sorted(la) != sorted(pa),
        "answer_order_changed": (mapping != list(range(len(la))))
        if comparable or sorted(la) == sorted(pa)
        else None,
        "correct_answer_changed": correct_changed,
        "image_changed": image_changed,
        "pdf_only_answers": list((Counter(pa) - Counter(la)).elements()),
        "live_only_answers": list((Counter(la) - Counter(pa)).elements()),
        "live_question_text": live.text,
        "pdf_question_text": pdf.question_text,
        "answer_alignment_live_to_pdf": mapping,
        "pdf_correct_answer_indexes": pdf.correct_answer_indexes,
        "live_correct_answer_indexes": [i for i, a in enumerate(live.answers) if a.correct],
    }


def match_questions(
    live: list[Question],
    pdf: list[PdfQuestion],
    descriptors: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    from rapidfuzz import fuzz, process

    descriptors = descriptors or {}
    texts = [normalized(q.question_text) for q in pdf]
    answers = [[normalized(a.text) for a in q.answers] for q in pdf]
    exact_index: dict[tuple[str, tuple[str, ...]], list[int]] = defaultdict(list)
    text_index: dict[str, list[int]] = defaultdict(list)
    answer_index: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i in range(len(pdf)):
        exact_index[texts[i], tuple(answers[i])].append(i)
        text_index[texts[i]].append(i)
        answer_index[tuple(sorted(answers[i]))].append(i)
    results = []
    for live_q in live:
        lt = normalized(live_q.text)
        la = [normalized(a.text) for a in live_q.answers]
        needs_image = image_dependent(lt, la)
        exact = set(exact_index.get((lt, tuple(la)), []))
        candidates = set(exact)
        candidates.update(text_index.get(lt, []))
        candidates.update(
            i for i in answer_index.get(tuple(sorted(la)), []) if fuzz.ratio(lt, texts[i]) >= 80
        )
        if not exact:
            candidates.update(
                i
                for _, _, i in process.extract(
                    lt, texts, scorer=fuzz.ratio, limit=40, score_cutoff=55
                )
            )
        evaluated: list[dict[str, Any]] = []
        for i in sorted(candidates):
            candidate_needs_image = needs_image or image_dependent(texts[i], answers[i])
            qscore = fuzz.ratio(lt, texts[i]) / 100
            ascore, order, mapping = answer_similarity(la, answers[i])
            if qscore < 0.55 or (ascore < 0.45 and qscore < 0.9):
                continue
            media = media_evidence(
                [r.sha256 for r in live_q.resources if r.sha256],
                [im["sha256"] for im in pdf[i].illustration_refs],
                descriptors,
            )
            score = 0.6 * qscore + 0.3 * ascore + 0.1 * order
            method = "fuzzy_candidate"
            status = "probable"
            if i in exact:
                status = "exact"
                method = "exact_text_answers_media" if media["identity"] else "exact_text_answers"
            elif critical_tokens(lt) == critical_tokens(texts[i]) and (
                (qscore >= 0.92 and ascore >= 0.96)
                or (qscore >= 0.80 and ascore == 1 and sum(map(len, la)) >= 70)
            ):
                status = "strong"
                method = "answer_aware_near"
            if sorted(la) == sorted(answers[i]) and lt == texts[i] and i not in exact:
                status, method = "strong", "exact_text_answer_set"
            if "incomplete_content" in pdf[i].issues or any(a.resources for a in live_q.answers):
                status = "probable"
            if candidate_needs_image and pdf[i].graphic_refs:
                status = "probable"
            evaluated.append(
                {
                    "pdf_code": pdf[i].code,
                    "pdf_page": pdf[i].page,
                    "pdf_order": pdf[i].order,
                    "pdf_source": pdf[i].source,
                    "prefix": pdf[i].prefix,
                    "section_code": pdf[i].section_code,
                    "section_title": pdf[i].section_title,
                    "match_method": method,
                    "match_status": status,
                    "confidence": round(score, 6),
                    "similarity": {
                        "question_text": round(qscore, 6),
                        "answer_set": round(ascore, 6),
                        "answer_order": round(order, 6),
                    },
                    "media_evidence": media,
                    "image_required_for_disambiguation": candidate_needs_image,
                    "differences": differences(live_q, pdf[i], mapping, media),
                    "pdf_issues": pdf[i].issues,
                    "content_group": content_key(pdf[i]),
                    "text_answer_group": content_key(pdf[i], images=False),
                }
            )
        evaluated.sort(key=lambda e: (-e["confidence"], e["pdf_order"]))
        accepted = [e for e in evaluated if e["match_status"] in USABLE]
        review: list[dict[str, Any]] = []
        if accepted:
            # Images disambiguate identical text/options. Similarity is not image identity.
            by_text: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for e in accepted:
                by_text[e["text_answer_group"]].append(e)
            retained = []
            for group in by_text.values():
                variants = {e["content_group"] for e in group}
                if any(e["image_required_for_disambiguation"] for e in group):
                    resolved = [
                        e
                        for e in group
                        if e["media_evidence"]["identity"]
                        or e["media_evidence"].get("strict_visual_support")
                    ]
                    if len({e["content_group"] for e in resolved}) == 1:
                        retained.extend(resolved)
                        review.extend(e for e in group if e not in resolved)
                    else:
                        for e in group:
                            e["match_status"] = "ambiguous" if len(variants) > 1 else "probable"
                        review.extend(group)
                else:
                    retained.extend(group)
            accepted = retained
            # Distinct near candidates with similar scores require review.
            if accepted and not any(e["match_status"] == "exact" for e in accepted):
                best = accepted[0]["confidence"]
                competing = [e for e in evaluated if e["confidence"] >= best - 0.025]
                groups = {e["text_answer_group"] for e in competing}
                if len(groups) > 1:
                    for e in accepted:
                        e["match_status"] = "ambiguous"
                    review.extend(accepted)
                    accepted = []
        if accepted:
            status = "exact" if any(e["match_status"] == "exact" for e in accepted) else "strong"
        else:
            if not review:
                review = evaluated[:10]
            best = review[0]["confidence"] if review else 0
            groups = {e["content_group"] for e in review if e["confidence"] >= best - 0.025}
            status = (
                (
                    "ambiguous"
                    if len(groups) > 1 or any(e["match_status"] == "ambiguous" for e in review)
                    else "probable"
                )
                if best >= 0.70
                else "unmatched"
            )
        for e in review:
            e["classification_usable"] = False
            if e["match_status"] in USABLE:
                e["match_status"] = "probable"
        for e in accepted:
            e["classification_usable"] = True
        for e in evaluated:
            e.pop("content_group", None)
            e.pop("text_answer_group", None)
        results.append(
            {
                "live_stable_key": live_q.stable_key,
                "official_id": str(live_q.official_id) if live_q.official_id else None,
                "id_status": live_q.id_status,
                "live_position": live_q.position,
                "live_question_text": live_q.text,
                "match_status": status,
                "classification_usable": bool(accepted),
                "matches": accepted,
                "candidates": review,
            }
        )
    return results


def crosswalk(root: Path, parsed: Path, pdf_media: Path) -> dict[str, Any]:
    pointer = current_pointer(root)
    if pointer is None:
        raise ValueError("No current snapshot")
    snapshot = snapshot_path(root, pointer["snapshot_id"])
    manifest = read_json(snapshot / "manifest.json")
    live_path = snapshot / "questions.jsonl"
    if file_hash(live_path) != manifest["artifacts"]["questions.jsonl"]:
        raise ValueError("Current questions checksum mismatch")
    live = read_questions(live_path)
    pdf = [
        PdfQuestion.model_validate_json(line)
        for line in (parsed / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    parse_summary = read_json(parsed / "summary.json")
    for name, sha in parse_summary.get("artifacts", {}).items():
        if name not in ("questions.jsonl", "sections.json", "parse-issues.json"):
            raise ValueError("Unexpected parsed artifact")
        if file_hash(parsed / name) != sha:
            raise ValueError("Parsed artifact checksum mismatch")
    if any(q.source["sha256_of_pdf"] != parse_summary["source"]["sha256_of_pdf"] for q in pdf):
        raise ValueError("Mixed PDF provenance")
    output = root / "derived" / pointer["snapshot_id"] / "reference-crosswalk"
    safe_output(output, root)
    output.mkdir(parents=True, exist_ok=True)
    descriptors = {}
    media_issues = []
    paths = {
        r.sha256: root / "media/objects" / r.sha256 for q in live for r in q.resources if r.sha256
    }
    paths.update(
        {im["sha256"]: pdf_media / im["sha256"] for q in pdf for im in q.illustration_refs}
    )
    for sha, path in sorted(paths.items()):
        try:
            if file_hash(path) != sha:
                raise ValueError("Media checksum mismatch")
            descriptors[sha] = image_descriptor(path)
        except (OSError, ValueError) as exc:
            media_issues.append({"sha256": sha, "error": str(exc)})
    records = match_questions(live, pdf, descriptors)
    matched = {m["pdf_order"] for r in records for m in r["matches"]}
    unmatched_pdf = [q.model_dump() for q in pdf if q.order not in matched]
    changed = [
        r for r in records if any(m["differences"]["question_text_changed"] for m in r["matches"])
    ]
    review = [r for r in records if r["candidates"] or not r["classification_usable"]]
    summary = {
        "schema_version": MATCH_VERSION,
        "snapshot_id": pointer["snapshot_id"],
        "snapshot_questions_sha256": file_hash(live_path),
        "pdf_source": parse_summary["source"],
        "parsed_questions_sha256": file_hash(parsed / "questions.jsonl"),
        "live_total": len(live),
        "match_status_counts": {
            status: sum(r["match_status"] == status for r in records)
            for status in ("exact", "strong", "probable", "ambiguous", "unmatched")
        },
        "live_multiple_pdf_codes": sum(
            len({m["pdf_code"] for m in r["matches"]}) > 1 for r in records
        ),
        "matched_pdf_positions": len(matched),
        "pdf_without_accepted_live_counterpart": len(unmatched_pdf),
        "review_live": len(review),
        "changed_wording": len(changed),
        "differences": {
            key: sum(any(m["differences"][key] is True for m in r["matches"]) for r in records)
            for key in (
                "answers_text_changed",
                "answer_order_changed",
                "correct_answer_changed",
                "image_changed",
            )
        },
        "correct_answer_comparable_pairs": sum(
            m["differences"]["correct_answer_changed"] is not None
            for r in records
            for m in r["matches"]
        ),
        "media_comparison_issues": media_issues,
        "initial_unresolved": [r for r in records if r["official_id"] is None],
        "policy": {
            "usable_statuses": sorted(USABLE),
            "score": "0.6 question + 0.3 answer set + 0.1 answer order; not a probability",
            "authority": "Live is current truth; PDF is reference classification.",
            "absent_semantics": (
                "Without accepted counterpart, not proven absent; includes review candidates."
            ),
        },
    }
    write_jsonl(output / "crosswalk.jsonl", records)
    atomic_json(output / "taxonomy.json", read_json(parsed / "sections.json"))
    atomic_json(output / "summary.json", summary)
    for name, items in (
        ("unmatched-live", [r for r in records if not r["classification_usable"]]),
        ("unmatched-pdf", unmatched_pdf),
        ("ambiguous", [r for r in records if r["match_status"] == "ambiguous"]),
        ("changed-wording", changed),
        ("review", review),
    ):
        atomic_json(output / f"{name}.json", {"records": items})
    return summary
