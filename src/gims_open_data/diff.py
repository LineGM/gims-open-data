"""Deterministic field-level comparison. Ambiguous keys are never paired."""

from collections import defaultdict
from typing import Any

from .media import MediaRecord
from .models import Question


def fields(q: Question) -> dict[str, Any]:
    return {
        "text": q.text,
        "answers": [(str(a.id), a.text) for a in q.answers],
        "correct_answers": sorted(str(i) for i in q.correct_answer_ids),
        "question_resources": [r.url for r in q.resources],
        "answer_resources": [(str(a.id), [r.url for r in a.resources]) for a in q.answers],
        "type": q.type,
        "is_additional": q.is_additional,
    }


def compare(
    previous: list[Question],
    current: list[Question],
    *,
    previous_snapshot: str | None,
    current_snapshot: str,
    old_media: dict[str, MediaRecord] | None = None,
    new_media: dict[str, MediaRecord] | None = None,
) -> dict[str, Any]:
    old: dict[str, list[Question]] = defaultdict(list)
    new: dict[str, list[Question]] = defaultdict(list)
    for q in previous:
        old[q.stable_key].append(q)
    for q in current:
        new[q.stable_key].append(q)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed, moved, ambiguous = [], [], []
    unchanged = 0
    for key in sorted(old.keys() | new.keys()):
        before, after = old[key], new[key]
        if len(before) > 1 or len(after) > 1:
            ambiguous.append(key)
            removed.extend({"stable_key": key, "position": q.position} for q in before)
            added.extend({"stable_key": key, "position": q.position} for q in after)
        elif not before:
            added.append({"stable_key": key, "position": after[0].position})
        elif not after:
            removed.append({"stable_key": key, "position": before[0].position})
        else:
            a, b = before[0], after[0]
            fa, fb = fields(a), fields(b)
            changed_fields = [name for name in fa if fa[name] != fb[name]]
            if changed_fields:
                changed.append(
                    {
                        "stable_key": key,
                        "fields": changed_fields,
                        "previous_revision": a.revision_fingerprint,
                        "current_revision": b.revision_fingerprint,
                    }
                )
            elif a.position == b.position:
                unchanged += 1
            if a.position != b.position:
                moved.append({"stable_key": key, "from": a.position, "to": b.position})
    om, nm = old_media or {}, new_media or {}
    return {
        "schema_version": "1.0",
        "previous_snapshot": previous_snapshot,
        "current_snapshot": current_snapshot,
        "previous_total": len(previous),
        "current_total": len(current),
        "delta_total": len(current) - len(previous),
        "added": added,
        "removed": removed,
        "changed": changed,
        "moved": moved,
        "unchanged_count": unchanged,
        "ambiguous_keys": ambiguous,
        "media_added": sorted(nm.keys() - om.keys()),
        "media_changed": [
            {"url": url, "previous_sha256": om[url].sha256, "current_sha256": nm[url].sha256}
            for url in sorted(om.keys() & nm.keys())
            if om[url].sha256 != nm[url].sha256
        ],
        "media_removed_or_unreferenced": sorted(om.keys() - nm.keys()),
    }
