"""Offline integrity and referential checks, including immutable live inputs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .mchs_reference_inventory import SOURCES, VERSION, allowed_url, discover, pdf_metadata
from .media import file_hash
from .reference_pdf import safe_output
from .snapshot import current_pointer, read_json, read_questions, snapshot_path, verify_snapshot


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def local_path(root: Path, name: str) -> Path:
    require(
        not Path(name).is_absolute() and "\\" not in name and ":" not in name,
        "Invalid relative path",
    )
    path = root / name
    require(path.resolve().is_relative_to(root.resolve()), "Path escapes artifact root")
    return path


def production_guard(root: Path) -> dict[str, Any]:
    pointer = current_pointer(root)
    if pointer is None:
        return {}
    snapshot = snapshot_path(root, pointer["snapshot_id"])
    return {
        "snapshot_id": pointer["snapshot_id"],
        "current_pointer_sha256": file_hash(root / "state/current.json"),
        "manifest_sha256": file_hash(snapshot / "manifest.json"),
        "artifacts": read_json(snapshot / "manifest.json")["artifacts"],
    }


def verify_guard(root: Path, guard: dict[str, Any]) -> None:
    if not guard:
        return
    snapshot = snapshot_path(root, guard["snapshot_id"])
    require(
        file_hash(root / "state/current.json") == guard["current_pointer_sha256"],
        "Current pointer changed",
    )
    require(
        file_hash(snapshot / "manifest.json") == guard["manifest_sha256"],
        "Production manifest changed",
    )
    for name, sha in guard["artifacts"].items():
        require(file_hash(local_path(snapshot, name)) == sha, "Production snapshot changed")


def check_artifacts(path: Path, metadata: dict[str, Any], expected: set[str]) -> None:
    require(set(metadata.get("artifacts", {})) == expected, "Artifact schema mismatch")
    for name, sha in metadata["artifacts"].items():
        require(file_hash(local_path(path, name)) == sha, f"Artifact checksum mismatch: {name}")


def privacy_check(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    require(
        not re.search(
            r"[A-Za-z]:[\\/](?:Users|Windows|data)|/testing/(?:activate|instance|test)/", text, re.I
        ),
        "Local path or transient test identity persisted",
    )
    require(
        not re.search(
            r'(?:PHPSESSID|csrf[_-]?token|authorization|access_token)[\s"\x27:=]+(?!REDACTED)[a-zA-Z0-9_-]{12,}',
            text,
            re.I,
        ),
        "Potential secret persisted",
    )


def verify_inventory(root: Path) -> dict[str, Any]:
    from .mchs_reference_pdf import read_records

    corpus = root / "reference/mchs-categorized"
    safe_output(corpus, root)
    manifest = read_json(corpus / "manifest.json")
    require(manifest.get("schema_version") == VERSION, "Unsupported inventory schema")
    check_artifacts(corpus, manifest, {"documents.jsonl", "pdf/index.json", "summary.json"})
    verify_guard(root, manifest.get("production_guard", {}))
    summary = read_json(corpus / "summary.json")
    require(summary.get("coherent") and not summary["failures"], "Inventory incomplete")
    pages = manifest["sources"]
    require(
        [(p["dimension"], p["category"], p["url"]) for p in pages] == SOURCES,
        "Expected exactly seven source pages",
    )
    discovered = []
    for page in pages:
        path = local_path(corpus, page["html_path"])
        require(file_hash(path) == page["sha256"], "HTML checksum mismatch")
        privacy_check(path)
        found = discover(path.read_bytes(), page["dimension"], page["category"], page["url"])
        discovered.extend(found["documents"])
    docs = read_records(corpus / "documents.jsonl")
    require(len(docs) == len(discovered), "Discovery/document count mismatch")
    require(len({d["membership_id"] for d in docs}) == len(docs), "Duplicate membership identity")
    for actual, expected in zip(docs, discovered, strict=True):
        require(all(actual.get(k) == v for k, v in expected.items()), "Source membership mismatch")
    index = read_json(corpus / "pdf/index.json")
    require(index.get("schema_version") == VERSION, "Unsupported PDF index")
    require(set(index["urls"]) == {d["pdf_url"] for d in docs}, "Document/index URL mismatch")
    checked = set()
    for url, obj in index["urls"].items():
        allowed_url(url)
        allowed_url(obj["final_url"])
        for redirect in obj["redirect_chain"]:
            allowed_url(redirect["url"])
            allowed_url(redirect["location"])
        require(obj["path"] == f"pdf/objects/{obj['sha256']}.pdf", "PDF object path mismatch")
        path = local_path(corpus, obj["path"])
        if obj["sha256"] not in checked:
            require(file_hash(path) == obj["sha256"], "PDF checksum mismatch")
            meta = pdf_metadata(path.read_bytes(), obj["headers"].get("content-type", ""))
            require(
                meta["page_count"] == obj["page_count"]
                and path.stat().st_size == obj["actual_bytes"],
                "PDF dimensions mismatch",
            )
            checked.add(obj["sha256"])
    for doc in docs:
        require(
            doc["document_id"]
            == doc["source_pdf_sha256"]
            == index["urls"][doc["pdf_url"]]["sha256"],
            "Document SHA mismatch",
        )
    require(
        summary["landing_pages"] == len(pages)
        and summary["pdf_memberships"] == len(docs)
        and summary["unique_urls"] == len(index["urls"])
        and summary["unique_pdf_sha256"] == len(checked),
        "Inventory counts mismatch",
    )
    require(sum(d["category"] == "motor" for d in docs) == 14, "Motor stop check failed")
    for name in ("manifest.json", "documents.jsonl", "pdf/index.json", "summary.json"):
        privacy_check(corpus / name)
    return {"inventory_valid": True, "pdf_objects": len(checked)}


def verify_parsed(root: Path) -> dict[str, Any]:
    from .mchs_reference_pdf import PublishedQuestion, parse_metrics, read_records

    corpus = root / "reference/mchs-categorized"
    parsed = corpus / "parsed"
    summary = read_json(parsed / "summary.json")
    require(summary.get("schema_version") == VERSION, "Unsupported parsed schema")
    require(
        summary["inventory_manifest_sha256"] == file_hash(corpus / "manifest.json"),
        "Stale parse inventory",
    )
    check_artifacts(parsed, summary, {"questions.jsonl", "topics.json", "parse-issues.json"})
    qs = [PublishedQuestion.model_validate(r) for r in read_records(parsed / "questions.jsonl")]
    topics = read_json(parsed / "topics.json")["documents"]
    docs = read_records(corpus / "documents.jsonl")
    require(len({q.reference_id for q in qs}) == len(qs), "Duplicate reference position")
    require(
        {t["document_id"] for t in topics} == {d["document_id"] for d in docs},
        "Parsed document mismatch",
    )
    checked = set()
    for topic in topics:
        expected = [d for d in docs if d["document_id"] == topic["document_id"]]
        require(topic["memberships"] == expected, "Topic membership mismatch")
        positions = [q for q in qs if q.document_id == topic["document_id"]]
        require(
            len(positions) == topic["question_count"]
            and [q.order for q in positions] == list(range(1, len(positions) + 1)),
            "Parsed position count mismatch",
        )
        for q in positions:
            require(q.memberships == expected, "Missing source membership")
        images = [im for q in positions for im in q.illustration_refs] + topic["document_images"]
        for im in images:
            require(im["source_pdf_sha256"] == topic["document_id"], "Media source mismatch")
            if im["sha256"] not in checked:
                path = local_path(parsed, "media/objects/" + im["sha256"])
                require(
                    file_hash(path) == im["sha256"] and path.stat().st_size == im["bytes"],
                    "Media checksum mismatch",
                )
                checked.add(im["sha256"])
    for k, v in parse_metrics(qs, topics).items():
        require(summary[k] == v, f"Parse metric mismatch: {k}")
    for name in ("questions.jsonl", "topics.json", "parse-issues.json", "summary.json"):
        privacy_check(parsed / name)
    return {"parsed_valid": True, "question_positions": len(qs), "media_objects": len(checked)}


def verify(root: Path, snapshot_id: str | None = None) -> dict[str, Any]:
    result = verify_inventory(root)
    corpus = root / "reference/mchs-categorized"
    if (corpus / "parsed/summary.json").exists():
        result.update(verify_parsed(root))
    pointer = current_pointer(root)
    selected = snapshot_id or (pointer["snapshot_id"] if pointer else None)
    if selected:
        verify_snapshot(root, snapshot_path(root, selected))
        result["production_snapshot_valid"] = True
        output = root / "derived" / selected / "mchs-categorized-crosswalk"
        if (output / "summary.json").exists():
            result.update(verify_crosswalk(root, selected))
    return result


def verify_crosswalk(root: Path, snapshot_id: str) -> dict[str, Any]:
    from .mchs_reference_audit import order_audit
    from .mchs_reference_group import build_reference_groups, serializable_group
    from .mchs_reference_match import crosswalk_metrics, group_answer_check, memberships_for
    from .mchs_reference_pdf import PublishedQuestion, read_records

    output = root / "derived" / snapshot_id / "mchs-categorized-crosswalk"
    summary = read_json(output / "summary.json")
    require(summary.get("schema_version") == VERSION, "Unsupported crosswalk schema")
    expected = {
        "crosswalk.jsonl",
        "reference-groups.jsonl",
        "taxonomy.json",
        "unmatched-live.json",
        "unmatched-reference.json",
        "ambiguous.json",
        "answer-drift.json",
        "review.json",
        "legacy-comparison.json",
        "order-audit.json",
    }
    check_artifacts(output, summary, expected)
    rows = read_records(output / "crosswalk.jsonl")
    live_path = snapshot_path(root, snapshot_id) / "questions.jsonl"
    require(
        summary["snapshot_questions_sha256"] == file_hash(live_path), "Stale crosswalk live inputs"
    )
    require(
        summary["parsed_questions_sha256"]
        == file_hash(root / "reference/mchs-categorized/parsed/questions.jsonl"),
        "Stale crosswalk reference inputs",
    )
    live = read_questions(live_path)
    require(len({r["live_stable_key"] for r in rows}) == len(rows), "Duplicate live stable key")
    require(
        [r["live_stable_key"] for r in rows] == [q.stable_key for q in live],
        "Live questions must appear exactly once",
    )
    require(summary["live_total"] == len(rows), "Live count mismatch")
    pdf = [
        PublishedQuestion.model_validate(r)
        for r in read_records(root / "reference/mchs-categorized/parsed/questions.jsonl")
    ]
    by_id = {q.reference_id: q for q in pdf}
    groups = build_reference_groups(pdf)
    by_group = {g["group_id"]: g for g in groups}
    serialized_groups = [
        json.loads(line)
        for line in (output / "reference-groups.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    require(
        serialized_groups == [serializable_group(g) for g in groups],
        "Reference group artifact mismatch",
    )
    for row, question in zip(rows, live, strict=True):
        require(
            row["live_position"] == question.position
            and row["official_id"] == (str(question.official_id) if question.official_id else None)
            and row["id_status"] == question.id_status
            and row["live_question_text"] == question.text,
            "Crosswalk live provenance mismatch",
        )
        matches = row["source_matches"]
        require(len({m["reference_id"] for m in matches}) == len(matches), "Duplicate source match")
        require(row["classification_usable"] == bool(matches), "Usability mismatch")
        for match in matches + row["candidates"]:
            require(match["reference_id"] in by_id, "Missing matched source reference")
            source = by_id[match["reference_id"]]
            group_ref = match.get("reference_question_group")
            require(group_ref and group_ref["group_id"] in by_group, "Missing source group")
            group = by_group[group_ref["group_id"]]
            require(
                match["memberships"] == group["memberships"]
                and match["document_id"] == source.document_id
                and match["pdf_page"] == source.page
                and match["question_number"] == source.question_number
                and match["source_derived_memberships"] == source.source_derived_memberships,
                "Matched source provenance mismatch",
            )
            require(
                match["source_row_ids"] == group["source_row_ids"]
                and group_ref["source_rows"] == group["source_row_ids"]
                and group_ref["published_answers"] == group["published_answers"],
                "Source group provenance mismatch",
            )
            require(
                match["answer_cross_check"] == group_answer_check(question, group),
                "Answer cross-check mismatch",
            )
        for match in matches:
            require(
                match["classification_usable"]
                and match["match_status"] in {"exact", "strong"}
                and not match["blockers"],
                "Unusable source match",
            )
        require(
            all(not c["classification_usable"] for c in row["candidates"]),
            "Review candidate accepted",
        )
        ships, areas, topics = memberships_for(matches)
        require(
            row["ship_type_memberships"] == ships
            and row["sailing_area_memberships"] == areas
            and row["topics"] == topics,
            "Missing crosswalk membership",
        )
    for key, value in crosswalk_metrics(rows, pdf).items():
        require(summary[key] == value, f"Crosswalk metric mismatch: {key}")
    matched = {
        row_id
        for r in rows
        for m in r["source_matches"]
        for row_id in m.get("source_row_ids", [m["reference_id"]])
    }
    queues = {
        "unmatched-live": [r for r in rows if not r["classification_usable"]],
        "unmatched-reference": [q.model_dump() for q in pdf if q.reference_id not in matched],
        "ambiguous": [r for r in rows if r["match_status"] == "ambiguous"],
        "review": [r for r in rows if r["candidates"] or not r["classification_usable"]],
        "answer-drift": [
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
    }
    for name, records in queues.items():
        require(read_json(output / f"{name}.json")["records"] == records, f"{name} queue mismatch")
    require(
        read_json(output / "taxonomy.json")
        == read_json(root / "reference/mchs-categorized/parsed/topics.json"),
        "Crosswalk taxonomy mismatch",
    )
    require(
        read_json(output / "legacy-comparison.json") == summary["legacy_comparison"],
        "Legacy summary mismatch",
    )
    require(
        read_json(output / "order-audit.json") == order_audit(rows),
        "Order audit mismatch",
    )
    for name in expected | {"summary.json"}:
        privacy_check(output / name)
    return {"crosswalk_valid": True, "live_questions": len(rows)}
