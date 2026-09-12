"""Comparison with legacy artifacts after independent categorized matching."""

import re
from pathlib import Path
from typing import Any

from .mchs_reference_match import answer_key, match_normalized
from .mchs_reference_pdf import read_records
from .media import file_hash
from .snapshot import read_json, snapshot_path


def order_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe category/topic runs around unresolved live positions without inference."""
    confident = [r for r in rows if r.get("match_status") in {"exact", "strong"}]
    unresolved = [r for r in rows if r.get("match_status") in {"probable", "unmatched"}]

    def signatures(row: dict[str, Any]) -> list[dict[str, str]]:
        values = []
        for topic in row.get("topics", []):
            values.append(
                {
                    "dimension": topic.get("dimension", ""),
                    "category": topic.get("category", ""),
                    "topic_code": topic.get("topic_code", ""),
                }
            )
        return values

    anchors = []
    for row in sorted(confident, key=lambda r: r["live_position"]):
        anchors.append({"position": row["live_position"], "signatures": signatures(row)})

    records = []
    for row in sorted(unresolved, key=lambda r: r["live_position"]):
        left = [a for a in anchors if a["position"] < row["live_position"]][-5:]
        right = [a for a in anchors if a["position"] > row["live_position"]][:5]
        left_topics = {
            (s["dimension"], s["category"], s["topic_code"]) for a in left for s in a["signatures"]
        }
        right_topics = {
            (s["dimension"], s["category"], s["topic_code"]) for a in right for s in a["signatures"]
        }
        left_categories = {(s["dimension"], s["category"]) for a in left for s in a["signatures"]}
        right_categories = {(s["dimension"], s["category"]) for a in right for s in a["signatures"]}
        records.append(
            {
                "live_position": row["live_position"],
                "live_stable_key": row["live_stable_key"],
                "match_status": row["match_status"],
                "left": left,
                "right": right,
                "left_topic_unanimous": len(left_topics) == 1 and len(left) == 5,
                "right_topic_unanimous": len(right_topics) == 1 and len(right) == 5,
                "left_category_unanimous": len(left_categories) == 1 and len(left) == 5,
                "right_category_unanimous": len(right_categories) == 1 and len(right) == 5,
                "same_topic_both_sides": len(left_topics) == 1
                and left_topics == right_topics
                and len(right) == 5,
                "same_category_both_sides": len(left_categories) == 1
                and left_categories == right_categories
                and len(right) == 5,
                "conflicting_anchors": bool(
                    left_topics and right_topics and left_topics != right_topics
                ),
                "transition_boundary": bool(
                    left_topics and right_topics and left_topics != right_topics
                ),
            }
        )
    return {
        "unresolved_total": len(records),
        "confident_anchor_total": len(anchors),
        "same_topic_both_sides": sum(r["same_topic_both_sides"] for r in records),
        "same_category_both_sides": sum(r["same_category_both_sides"] for r in records),
        "conflicting_anchors": sum(r["conflicting_anchors"] for r in records),
        "transition_boundary": sum(r["transition_boundary"] for r in records),
        "records": records,
        "inference_applied": False,
    }


def legacy_comparison(root: Path, snapshot_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    output = root / "derived" / snapshot_id
    result: dict[str, Any] = {
        "authority": "live > categorized > legacy",
        "influenced_new_matching": False,
    }
    for namespace in ("reference-crosswalk", "reference-classification"):
        path = output / namespace / "crosswalk.jsonl"
        if not path.exists():
            # Classification uses its own name in older revisions.
            path = output / namespace / "classification.jsonl"
        if not path.exists():
            result[namespace] = {"available": False}
            continue
        old = {r["live_stable_key"]: r for r in read_records(path)}
        legacy_summary = read_json(path.parent / "summary.json")
        if legacy_summary.get("snapshot_questions_sha256") != file_hash(
            snapshot_path(root, snapshot_id) / "questions.jsonl"
        ):
            raise ValueError("Legacy audit belongs to different live input bytes")
        old_usable = {k for k, v in old.items() if v.get("classification_usable")}
        new_usable = {r["live_stable_key"] for r in rows if r["classification_usable"]}
        all_keys = {r["live_stable_key"] for r in rows}
        result[namespace] = {
            "available": True,
            "artifact_sha256": file_hash(path),
            "legacy_usable": len(old_usable),
            "matched_by_both": len(new_usable & old_usable),
            "only_new": len(new_usable - old_usable),
            "only_legacy": len(old_usable - new_usable),
            "neither": len(all_keys - (old_usable | new_usable)),
            "only_new_positions": [
                r["live_position"] for r in rows if r["live_stable_key"] in new_usable - old_usable
            ],
        }
        title_differences = []
        for row in rows:
            key = row["live_stable_key"]
            if key not in new_usable & old_usable:
                continue
            new_titles = {title_key(t["topic_title"]) for t in row["topics"]}
            old_titles = {title_key(m.get("section_title") or "") for m in old[key]["matches"]}
            if new_titles != old_titles:
                title_differences.append(
                    {
                        "live_position": row["live_position"],
                        "live_stable_key": key,
                        "new_topic_titles": sorted(new_titles),
                        "legacy_topic_titles": sorted(old_titles),
                        "no_shared_title": not bool(new_titles & old_titles),
                    }
                )
        result[namespace]["topic_title_set_differences"] = len(title_differences)
        result[namespace]["no_shared_topic_title"] = sum(
            d["no_shared_title"] for d in title_differences
        )
        result[namespace]["taxonomy_review"] = title_differences
        result[namespace]["taxonomy_comparison_semantics"] = (
            "Literal published-title sets after typography and observed category suffix removal; "
            "differences are review evidence, not proven semantic classification conflicts"
        )
    result.update(legacy_content_audit(root, snapshot_id, rows))
    return result


def title_key(title: str) -> str:
    return match_normalized(
        re.sub(r"\s*\((?:ммс|мпс|гд|с|ввп|вп|мп)\+?\)\s*$", "", title, flags=re.I)
    )


def legacy_content_audit(
    root: Path, snapshot_id: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    legacy_path = root / "reference/parsed/questions.jsonl"
    if not legacy_path.exists():
        return {"legacy_question_text_audit": {"available": False}}
    old_pdf = read_records(legacy_path)
    legacy_summary = read_json(legacy_path.parent / "summary.json")
    if legacy_summary.get("artifacts", {}).get("questions.jsonl") != file_hash(legacy_path):
        raise ValueError("Legacy parsed checksum mismatch")
    old_texts = {match_normalized(q["question_text"]) for q in old_pdf}
    result = {
        "legacy_question_text_audit": {
            "artifact_sha256": file_hash(legacy_path),
            "new_matched_without_legacy_exact_text": sum(
                r["classification_usable"]
                and match_normalized(r["live_question_text"]) not in old_texts
                for r in rows
            ),
            "meaning": (
                "No exact normalized live question text in legacy; not proof of semantic absence"
            ),
            "legacy_positions": len(old_pdf),
            "legacy_known_correctness": sum(
                q["correct_answer_indexes"] is not None for q in old_pdf
            ),
        }
    }
    by_order = {q["order"]: q for q in old_pdf}
    strict_path = root / "derived" / snapshot_id / "reference-crosswalk/crosswalk.jsonl"
    old_matches = (
        {r["live_stable_key"]: r for r in read_records(strict_path)} if strict_path.exists() else {}
    )
    answer_pairs = []
    for row in rows:
        for old_match in old_matches.get(row["live_stable_key"], {}).get("matches", []):
            question = by_order[old_match["pdf_order"]]
            indexes = question["correct_answer_indexes"]
            if indexes is None or len(indexes) != 1:
                continue
            old_answer = question["answers"][indexes[0]]["text"]
            for new_match in row["source_matches"]:
                new_answer = new_match["answer_cross_check"]["published_correct_answer"]
                if new_answer is not None:
                    answer_pairs.append(
                        {
                            "live_position": row["live_position"],
                            "legacy_pdf_order": old_match["pdf_order"],
                            "new_reference_id": new_match["reference_id"],
                            "same_normalized_answer": answer_key(old_answer)
                            == answer_key(new_answer),
                        }
                    )
    result["new_legacy_answer_comparison"] = {
        "comparable_pairs": len(answer_pairs),
        "different_pairs": sum(not p["same_normalized_answer"] for p in answer_pairs),
        "pairs": answer_pairs,
        "unknown_without_labels": not bool(answer_pairs),
    }
    return result
