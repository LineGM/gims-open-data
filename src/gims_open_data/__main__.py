import argparse
import json
import logging
import math
from pathlib import Path

from .client import ImportStopped
from .diff import compare
from .snapshot import (
    current_pointer,
    read_json,
    read_media,
    read_questions,
    snapshot_path,
    verify_snapshot,
)
from .sync import sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Official GIMS periodic snapshot sync")
    commands = parser.add_subparsers(dest="command", required=True)
    reference = commands.add_parser("reference", help="offline PDF reference and crosswalk")
    ref_commands = reference.add_subparsers(dest="reference_command", required=True)
    ref_parse = ref_commands.add_parser("parse", help="parse local reference PDF")
    ref_parse.add_argument(
        "--pdf", type=Path, default=Path("data/reference/gims-attestation-question-bank.pdf")
    )
    ref_match = ref_commands.add_parser("crosswalk", help="match against current snapshot offline")
    ref_classify = ref_commands.add_parser(
        "classify", help="classify live questions into PDF topics offline"
    )
    for ref in (ref_parse, ref_match, ref_classify):
        ref.add_argument("--data-root", type=Path, default=Path("data"))
        ref.add_argument("--parsed", type=Path, default=Path("data/reference/parsed"))
        ref.add_argument("--media", type=Path, default=Path("data/reference/media"))
        ref.add_argument("--json-summary", action="store_true")
    sync_parser = commands.add_parser("sync", help="download, verify, diff and promote full corpus")
    sync_parser.add_argument("--delay", type=float, default=1.0)
    sync_parser.add_argument("--media-delay", type=float, default=1.0)
    sync_parser.add_argument("--attempts", type=int, default=3)
    sync_parser.add_argument("--no-media", action="store_true")
    sync_parser.add_argument("--limit", type=int, help="debug smoke; NEVER promotes current")
    sync_parser.add_argument("--verbose", action="store_true")
    verify = commands.add_parser("verify", help="verify current or named snapshot offline")
    verify.add_argument("--snapshot")
    diff = commands.add_parser("diff", help="compare two verified snapshots offline")
    diff.add_argument("previous")
    diff.add_argument("current")
    status = commands.add_parser("status", help="local current snapshot status")
    for command in (sync_parser, verify, diff, status):
        command.add_argument("--output", type=Path, default=Path("data"))
        command.add_argument("--json-summary", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "reference":
            if args.reference_command == "parse":
                from .reference_pdf import parse_pdf

                result = parse_pdf(args.pdf, args.parsed, args.media, root=args.data_root)
            elif args.reference_command == "classify":
                from .reference_classification import classify

                result = classify(args.data_root, args.parsed, args.media)
            else:
                from .reference_match import crosswalk

                result = crosswalk(args.data_root, args.parsed, args.media)
        elif args.command == "sync":
            if (
                not math.isfinite(args.delay)
                or args.delay < 1
                or not math.isfinite(args.media_delay)
                or args.media_delay < 0
                or args.attempts < 1
                or (args.limit is not None and args.limit < 1)
            ):
                parser.error(
                    "delay >=1, media-delay >=0, attempts/limit >=1; finite numbers required"
                )
            result = sync(
                args.output,
                delay=args.delay,
                media_delay=args.media_delay,
                attempts=args.attempts,
                no_media=args.no_media,
                limit=args.limit,
                verbose=args.verbose,
            )
        elif args.command == "diff":
            old = snapshot_path(args.output, args.previous)
            new = snapshot_path(args.output, args.current)
            verify_snapshot(args.output, old)
            verify_snapshot(args.output, new)
            result = compare(
                read_questions(old / "questions.jsonl"),
                read_questions(new / "questions.jsonl"),
                previous_snapshot=args.previous,
                current_snapshot=args.current,
                old_media=read_media(old / "media-manifest.json"),
                new_media=read_media(new / "media-manifest.json"),
            )
        else:
            selected = getattr(args, "snapshot", None)
            pointer = current_pointer(args.output) if not selected else None
            snapshot_id = selected or (pointer["snapshot_id"] if pointer else None)
            if snapshot_id is None:
                if args.command == "verify":
                    raise ImportStopped("No current snapshot", "validation")
                result = {"current_snapshot": None}
            else:
                path = snapshot_path(args.output, snapshot_id)
                if args.command == "verify":
                    result = verify_snapshot(args.output, path)
                    result = {**result, "valid": True}
                else:
                    summary = read_json(path / "summary.json")
                    changes = read_json(path / "diff.json")
                    result = {
                        k: summary[k]
                        for k in (
                            "created_at",
                            "current_total",
                            "with_official_id",
                            "without_official_id",
                            "single",
                            "multiple",
                            "with_question_media",
                            "with_answer_media",
                            "media_objects",
                            "media_bytes",
                            "previous_snapshot",
                            "media_enabled",
                        )
                    }
                    result["current_snapshot"] = snapshot_id
                    result["diff_counts"] = {
                        k: len(changes[k]) for k in ("added", "removed", "changed", "moved")
                    }
        if args.json_summary or args.command == "diff":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for key, value in result.items():
                if key not in {
                    "duplicates_by_uuid",
                    "duplicates_by_fingerprint",
                    "validation_errors",
                }:
                    print(f"{key}: {value}")
        return 0
    except KeyboardInterrupt:
        logging.getLogger("gims_open_data").warning("Interrupted; previous current retained")
        return 130
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ImportStopped)
            else f"Local validation error ({type(exc).__name__})"
        )
        logging.getLogger("gims_open_data").error("%s", message)
        if args.json_summary:
            print(
                json.dumps(
                    {"ok": False, "kind": getattr(exc, "kind", "validation"), "error": message}
                )
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
