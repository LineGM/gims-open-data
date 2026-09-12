"""Build and verify self-contained question snapshots with shared media objects."""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .canonical import MATCH_ALGORITHM, REVISION_ALGORITHM
from .client import ImportStopped
from .media import MediaRecord, all_resources, file_hash, read_index, valid_media
from .models import Question, summarize
from .parse_html import parse_initial_html
from .parse_json import parse_question_json
from .storage import atomic_json


def read_json(path: Path) -> dict[str, Any]:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ImportStopped("Expected JSON object", "validation")
    return data


def read_questions(path: Path) -> list[Question]:
    return [
        Question.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]


def read_media(path: Path) -> dict[str, MediaRecord]:
    return {url: MediaRecord.model_validate(value) for url, value in read_json(path).items()}


def snapshot_path(root: Path, snapshot_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", snapshot_id) or snapshot_id in {".", ".."}:
        raise ImportStopped("Invalid snapshot ID", "validation")
    return root / "snapshots" / snapshot_id


def current_pointer(root: Path) -> dict[str, Any] | None:
    path = root / "state/current.json"
    if not path.exists():
        return None
    pointer = read_json(path)
    if pointer.get("schema_version") != "1.0":
        raise ImportStopped("Unsupported current pointer schema", "validation")
    target = snapshot_path(root, pointer["snapshot_id"])
    if pointer["path"] != f"snapshots/{target.name}":
        raise ImportStopped("Current pointer path mismatch", "validation")
    if file_hash(target / "manifest.json") != pointer["sha256"]:
        raise ImportStopped("Current manifest hash mismatch", "validation")
    return pointer


def build_snapshot(
    stage: Path,
    import_run: Path,
    questions: list[Question],
    media: dict[str, MediaRecord],
    summary: dict[str, Any],
    diff: dict[str, Any],
    *,
    snapshot_id: str,
    media_enabled: bool,
    complete: bool,
) -> None:
    stage.mkdir()
    shutil.copytree(import_run / "raw", stage / "raw")
    with (stage / "questions.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for question in questions:
            stream.write(question.model_dump_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    atomic_json(stage / "summary.json", summary)
    atomic_json(stage / "diff.json", diff)
    atomic_json(stage / "media-manifest.json", {url: m.model_dump() for url, m in media.items()})
    paths = sorted(p for p in stage.rglob("*") if p.is_file())
    artifacts = {p.relative_to(stage).as_posix(): file_hash(p) for p in paths}
    atomic_json(
        stage / "manifest.json",
        {
            "schema_version": "1.0",
            "snapshot_id": snapshot_id,
            "complete": complete,
            "created_at": summary["created_at"],
            "expected_total": summary["reported_questions_count"],
            "media_enabled": media_enabled,
            "match_algorithm": MATCH_ALGORITHM,
            "revision_algorithm": REVISION_ALGORITHM,
            "artifacts": artifacts,
            "raw_sha256": {k: v for k, v in artifacts.items() if k.startswith("raw/")},
            "previous_snapshot": diff["previous_snapshot"],
        },
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ImportStopped(message, "validation")


def verify_snapshot(root: Path, path: Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """No network. Includes reparse of every raw question against normalized content."""
    manifest = read_json(path / "manifest.json")
    _require(manifest["schema_version"] == "1.0", "Unsupported snapshot schema")
    _require(manifest["match_algorithm"] == MATCH_ALGORITHM, "Unknown match algorithm")
    _require(manifest["revision_algorithm"] == REVISION_ALGORITHM, "Unknown revision algorithm")
    _require(manifest["complete"] or allow_incomplete, "Incomplete snapshot")
    artifacts = manifest["artifacts"]
    required = {"questions.jsonl", "summary.json", "diff.json", "media-manifest.json"}
    _require(required.issubset(artifacts), "Missing required artifact hashes")
    actual_files = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
    _require(
        actual_files == set(artifacts) | {"manifest.json"}, "Unexpected or missing snapshot files"
    )
    for relative, sha in artifacts.items():
        target = path / relative
        _require(target.resolve().is_relative_to(path.resolve()), "Invalid artifact path")
        _require(file_hash(target) == sha, "Artifact checksum mismatch")
    questions = read_questions(path / "questions.jsonl")
    for line in (path / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        _require(
            all(
                payload.get(key)
                for key in (
                    "schema_version",
                    "stable_key",
                    "match_fingerprint",
                    "revision_fingerprint",
                )
            ),
            "Snapshot missing explicit schema/identity fields",
        )
    _require(bool(questions), "Empty snapshot")
    summary = summarize(questions, manifest["expected_total"], complete=manifest["complete"])
    _require(not summary["validation_errors"], "Invalid question corpus")
    saved_summary = read_json(path / "summary.json")
    for key, value in summary.items():
        _require(saved_summary.get(key) == value, "Summary disagrees with corpus")
    raw_hashes = manifest["raw_sha256"]
    expected_raw = {
        f"raw/{q.position:04d}.{'html' if q.position == 1 else 'json'}" for q in questions
    }
    _require(set(raw_hashes) == expected_raw, "Raw coverage mismatch")
    for q in questions:
        raw_name = f"raw/{q.position:04d}.{'html' if q.position == 1 else 'json'}"
        _require(raw_hashes[raw_name] == artifacts[raw_name], "Raw hash disagrees with manifest")
        raw = (path / raw_name).read_bytes()
        parser = parse_initial_html if q.position == 1 else parse_question_json
        parsed = parser(raw, q.source.retrieved_at)
        _require(
            parsed.revision_fingerprint == q.revision_fingerprint
            and parsed.stable_key == q.stable_key
            and parsed.counters == q.counters,
            "Raw and normalized question disagree",
        )
    records = read_media(path / "media-manifest.json")
    resources = all_resources(questions)
    if manifest["media_enabled"]:
        _require(set(records) == {r.url for r in resources}, "Media coverage mismatch")
        index = read_index(root)
        checked = set()
        for resource in resources:
            record = records[resource.url]
            _require(record.url == resource.url, "Media URL mismatch")
            _require(
                (resource.sha256, resource.bytes, resource.content_type)
                == (record.sha256, record.bytes, record.content_type),
                "Resource metadata mismatch",
            )
            history = index["versions"].get(record.url, {}).get(record.sha256)
            _require(history is not None, "Media index missing historical object")
            indexed = MediaRecord.model_validate(history)
            latest = MediaRecord.model_validate(index["urls"].get(record.url))
            _require(latest.url == record.url, "Latest media index URL mismatch")
            latest_history = index["versions"].get(record.url, {}).get(latest.sha256)
            _require(latest_history == latest.model_dump(), "Latest media index/history mismatch")
            _require(
                (indexed.sha256, indexed.bytes, indexed.content_type)
                == (record.sha256, record.bytes, record.content_type),
                "Media index mismatch",
            )
            if record.sha256 not in checked:
                obj = root / "media/objects" / record.sha256
                _require(
                    obj.exists() and obj.stat().st_size == record.bytes,
                    "Media missing/size mismatch",
                )
                _require(file_hash(obj) == record.sha256, "Media checksum mismatch")
                _require(valid_media(record.content_type, obj.read_bytes()), "Invalid media bytes")
                checked.add(record.sha256)
    else:
        _require(
            not records
            and all(
                r.sha256 is None and r.bytes is None and r.content_type is None for r in resources
            ),
            "No-media snapshot has unexpected metadata",
        )
    for name in (
        "manifest.json",
        "questions.jsonl",
        "summary.json",
        "diff.json",
        "media-manifest.json",
    ):
        body = (path / name).read_text(encoding="utf-8")
        _require(
            not re.search(
                r"PHPSESSID|csrf|/testing/(?:activate|instance|test)/|"
                r'"(?:instance_uuid|instance_url|authorization|cookie|_token)"',
                body,
                re.I,
            ),
            "Session/auth material in snapshot metadata",
        )
    _require(
        saved_summary["media_objects"] == len({m.sha256 for m in records.values()}),
        "Media object count mismatch",
    )
    objects = {m.sha256: m.bytes for m in records.values()}
    _require(saved_summary["media_bytes"] == sum(objects.values()), "Media byte count mismatch")
    _require(saved_summary["current_total"] == len(questions), "Current total mismatch")
    _require(
        saved_summary["delta_total"] == len(questions) - saved_summary["previous_total"],
        "Total delta mismatch",
    )
    return saved_summary


def promote(root: Path, stage: Path) -> Path:
    verify_snapshot(root, stage)
    manifest = read_json(stage / "manifest.json")
    target = snapshot_path(root, manifest["snapshot_id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    _require(not target.exists(), "Snapshot already exists; immutable snapshots cannot be replaced")
    os.replace(stage, target)
    # Verify AFTER moving into snapshots, but BEFORE committing current. On failure,
    # return the diagnostic candidate to runs; the previous pointer never changes.
    try:
        verify_snapshot(root, target)
        pointer = {
            "schema_version": "1.0",
            "snapshot_id": target.name,
            "path": f"snapshots/{target.name}",
            "sha256": file_hash(target / "manifest.json"),
        }
        atomic_json(root / "state/current.json", pointer)
    except BaseException:
        # A signal can arrive immediately AFTER os.replace(current.json) commits.
        # Never roll back its target when the committed pointer already references it.
        committed = current_pointer(root)
        if committed and committed["snapshot_id"] == target.name:
            return target
        os.replace(target, stage)
        raise
    return target
