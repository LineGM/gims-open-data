import argparse
import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .client import ImportStopped, MarathonClient
from .models import SIMULATOR_URL, Question, summarize
from .parse_html import parse_initial_html
from .parse_json import parse_question_json
from .storage import atomic_json, save_raw

LOG = logging.getLogger("gims_open_data")


def utcnow() -> datetime:
    return datetime.now(UTC)


def run_import(
    client: MarathonClient, *, root: Path, limit: int | None, verbose: bool = False
) -> tuple[Path, dict[str, Any]]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    started = utcnow()
    run_id = started.strftime("%Y%m%dT%H%M%S.%fZ")
    run = root / run_id
    (run / "raw").mkdir(parents=True, exist_ok=False)
    (run / "normalized").mkdir()
    checkpoint: dict[str, Any] = {
        "run_id": run_id,
        "instance_url": None,
        "instance_uuid": None,
        "last_confirmed_position": 0,
        "last_confirmed_question_official_id": None,
        "expected_total": None,
        "status": "creating",
        "pending_answer_position": None,
        "resume_supported": False,
    }
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "importer_version": __version__,
        "schema_version": "1.0",
        "publisher": "МЧС России",
        "simulator_url": SIMULATOR_URL,
        "started_at": started.isoformat(),
        "delay": client.delay,
        "limit": limit,
        "raw_policy": "Original body bytes except session/auth secrets; local instance ID retained",
        "fingerprint_algorithm": "sha256-nfc-whitespace-ordered-text-resources-v1",
        "artifacts": [],
        "status": "creating",
    }
    questions: list[Question] = []

    def write_checkpoint() -> None:
        checkpoint["updated_at"] = utcnow().isoformat()
        atomic_json(run / "checkpoint.json", checkpoint)

    write_checkpoint()
    atomic_json(run / "manifest.json", manifest)
    LOG.info("Run directory: %s", run)
    try:
        instance_url, instance, raw = client.create_marathon()
        retrieved = utcnow()
        checkpoint.update(instance_url=instance_url, instance_uuid=str(instance))
        write_checkpoint()
        position = 1
        while True:
            extension = "html" if position == 1 else "json"
            artifact = save_raw(run / "raw" / f"{position:04d}.{extension}", raw, client.secrets)
            manifest["artifacts"].append(artifact)
            atomic_json(run / "manifest.json", manifest)
            question = (
                parse_initial_html(raw, retrieved)
                if position == 1
                else parse_question_json(raw, retrieved)
            )
            if question.position != position:
                raise ImportStopped("Server returned an unexpected position; raw saved")
            total = question.counters.questions_count
            if position == 1:
                checkpoint["expected_total"] = total
            elif total != checkpoint["expected_total"]:
                raise ImportStopped("Reported total changed within the marathon; raw saved")
            if question.multiple:
                LOG.info(
                    "Multiple question at position %d; original JSON saved: %s",
                    position,
                    artifact["file"],
                )
            with (run / "normalized" / "questions.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            ) as stream:
                stream.write(question.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            questions.append(question)
            checkpoint.update(
                last_confirmed_position=position,
                last_confirmed_question_official_id=(
                    str(question.official_id) if question.official_id else None
                ),
                status="running",
                pending_answer_position=None,
            )
            write_checkpoint()
            if verbose or position == 1 or position % 50 == 0 or position == total:
                LOG.info("Questions: %d/%d", position, total)
            if position == total or (limit is not None and position >= limit):
                break
            # Persist intent before sending: interruption must not hide an in-flight POST.
            checkpoint.update(status="post_pending", pending_answer_position=position)
            write_checkpoint()
            raw = client.answer(instance, question.correct_answer_ids)
            retrieved = utcnow()
            position += 1

        # Validate the actual on-disk dataset, including model invariants and fingerprints.
        questions = [
            Question.model_validate_json(line)
            for line in (run / "normalized" / "questions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = summarize(questions, checkpoint["expected_total"], complete=position == total)
        atomic_json(run / "summary.json", summary)
        if summary["validation_errors"]:
            raise ImportStopped("Dataset validation failed; see summary.json", "validation")
        status = "complete" if summary["complete"] else "limited"
        checkpoint["status"] = status
        write_checkpoint()
        manifest.update(status=status, finished_at=utcnow().isoformat(), summary=summary)
        atomic_json(run / "manifest.json", manifest)
        return run, summary
    except (Exception, KeyboardInterrupt) as exc:
        # Do not persist exception repr: parsers/http libraries may embed secrets or URLs.
        checkpoint["status"] = (
            "stopped_uncertain_post" if checkpoint["pending_answer_position"] else "stopped"
        )
        checkpoint["error_kind"] = type(exc).__name__
        write_checkpoint()
        manifest.update(status=checkpoint["status"], finished_at=utcnow().isoformat())
        atomic_json(run / "manifest.json", manifest)
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, ImportStopped):
            raise
        raise ImportStopped(
            f"Import stopped ({type(exc).__name__}); inspect local raw/checkpoint in {run}. "
            "No POST retry or automatic resume."
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sequential official GIMS marathon import")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="collect every reported position")
    scope.add_argument("--limit", type=int, help="stop after N saved questions (including initial)")
    parser.add_argument(
        "--delay", type=float, default=1.0, help="minimum request gap, >=1.0 seconds"
    )
    parser.add_argument("--output", type=Path, default=Path("data/runs"))
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < 1:
        parser.error("--delay must be finite and >= 1.0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP log messages contain sensitive redirect URLs; keep them out of normal logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        with MarathonClient(args.delay) as client:
            run, summary = run_import(client, root=args.output, limit=args.limit)
    except ImportStopped as exc:
        LOG.error("%s", exc)
        return 1
    LOG.info("Finished: %s", run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
