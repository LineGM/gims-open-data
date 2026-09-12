"""Transactional whole-run orchestration over the existing importer."""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import ImportStopped, MarathonClient
from .diff import compare
from .importer import run_import, utcnow
from .lock import SyncLock
from .media import MediaDownloader
from .snapshot import (
    build_snapshot,
    current_pointer,
    promote,
    read_media,
    read_questions,
    snapshot_path,
    verify_snapshot,
)
from .storage import atomic_json

LOG = logging.getLogger("gims_open_data")


def sync(
    root: Path,
    *,
    delay: float = 1.0,
    media_delay: float = 1.0,
    attempts: int = 3,
    no_media: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    client_factory: Callable[[], MarathonClient] | None = None,
    media_factory: Callable[[], MediaDownloader] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    create_client = client_factory or (lambda: MarathonClient(delay))
    create_media = media_factory or (lambda: MediaDownloader(root, media_delay))
    with SyncLock(root):
        previous = current_pointer(root)
        previous_id = previous["snapshot_id"] if previous else None
        old_questions = []
        old_media = {}
        if previous_id:
            old_path = snapshot_path(root, previous_id)
            verify_snapshot(root, old_path)
            old_questions = read_questions(old_path / "questions.jsonl")
            old_media = read_media(old_path / "media-manifest.json")
        for attempt in range(1, attempts + 1):
            run_id = utcnow().strftime("%Y%m%dT%H%M%S.%fZ")
            run = root / "runs" / run_id
            run.mkdir(parents=True)
            handler = logging.FileHandler(run / "sync.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(handler)
            try:
                LOG.info("Sync attempt %d/%d: %s", attempt, attempts, run)
                with create_client() as client:
                    import_run, summary = run_import(
                        client, root=run / "import", limit=limit, verbose=verbose
                    )
                questions = read_questions(import_run / "normalized/questions.jsonl")
                records = {}
                media_stats = {"requests": 0, "downloads": 0, "not_modified": 0}
                if not no_media:
                    with create_media() as downloader:
                        records = downloader.collect(questions, verbose=verbose)
                        media_stats = downloader.stats
                diff = compare(
                    old_questions,
                    questions,
                    previous_snapshot=previous_id,
                    current_snapshot=run_id,
                    old_media=old_media,
                    new_media=records,
                )
                objects = {r.sha256: r.bytes for r in records.values()}
                summary.update(
                    previous_total=len(old_questions),
                    current_total=len(questions),
                    delta_total=len(questions) - len(old_questions),
                    created_at=utcnow().isoformat(),
                    previous_snapshot=previous_id,
                    snapshot_id=run_id,
                    media_enabled=not no_media,
                    media_urls=len(records),
                    media_objects=len(objects),
                    media_bytes=sum(objects.values()),
                    media_http=media_stats,
                )
                stage = run / "snapshot"
                build_snapshot(
                    stage,
                    import_run,
                    questions,
                    records,
                    summary,
                    diff,
                    snapshot_id=run_id,
                    media_enabled=not no_media,
                    complete=summary["complete"],
                )
                verify_snapshot(root, stage, allow_incomplete=limit is not None)
                if limit is None:
                    promote(root, stage)
                    LOG.info("Current snapshot: %s", run_id)
                else:
                    LOG.info("Smoke verified; current unchanged. Candidate: %s", stage)
                # Return-only field: immutable snapshot artifacts are never edited after promotion.
                return {
                    **summary,
                    "promoted": limit is None,
                    "run_path": str(run),
                    "diff_counts": {
                        k: len(diff[k]) for k in ("added", "removed", "changed", "moved")
                    },
                }
            except KeyboardInterrupt:
                atomic_json(run / "failure.json", {"kind": "user_interruption"})
                LOG.warning("Interrupted; no restart")
                raise
            except Exception as exc:
                kind = exc.kind if isinstance(exc, ImportStopped) else "validation"
                atomic_json(run / "failure.json", {"kind": kind, "error_type": type(exc).__name__})
                LOG.error("Attempt failed: %s (%s); current unchanged", kind, type(exc).__name__)
                if isinstance(exc, ImportStopped):
                    LOG.error("%s", exc)
                if kind != "transient" or attempt == attempts:
                    if isinstance(exc, ImportStopped):
                        raise
                    raise ImportStopped(
                        "Sync validation/protocol failure; see diagnostic run", "validation"
                    ) from None
            finally:
                LOG.removeHandler(handler)
                handler.close()
            wait = min(300, 30 * 2 ** (attempt - 1))
            LOG.warning("New marathon attempt after %ds backoff", wait)
            sleep(wait)
    raise AssertionError("unreachable")
