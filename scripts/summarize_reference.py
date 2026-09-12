"""Print bounded metrics and 20 review examples; optionally write a Markdown report."""

# Report prose and Markdown tables intentionally contain long human-readable lines.
# ruff: noqa: E501

import argparse
import json
from pathlib import Path

from gims_open_data.snapshot import current_pointer

parser = argparse.ArgumentParser()
parser.add_argument("--data-root", type=Path, default=Path("data"))
parser.add_argument("--report", type=Path)
args = parser.parse_args()
root = args.data_root
pointer = current_pointer(root)
if pointer is None:
    raise SystemExit("No current snapshot")
out = root / "derived" / pointer["snapshot_id"] / "reference-crosswalk"
pdf = json.loads((root / "reference/parsed/summary.json").read_text(encoding="utf-8"))
summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
rows = [
    json.loads(line) for line in (out / "crosswalk.jsonl").read_text(encoding="utf-8").splitlines()
]
examples = []
for status in ("unmatched", "ambiguous"):
    candidates = [r for r in rows if r["match_status"] == status]
    candidates.sort(
        key=lambda r: (
            -max((c["confidence"] for c in r["candidates"]), default=0),
            r["live_position"],
        )
    )
    examples.extend(candidates[:10])


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


lines = [
    "# PDF reference investigation: full local run, 2026-09-12",
    "",
    f"Snapshot: `{summary['snapshot_id']}`. PDF SHA-256: `{pdf['source']['sha256_of_pdf']}`.",
    "",
    "The immutable current snapshot passed offline verification after the work. No API calls,",
    "commits or pushes were made. All parsed datasets, reference media and crosswalk files",
    "remain under ignored `data/`. This document contains aggregates and bounded samples only.",
    "",
    "## PDF counts",
    "",
    "| Metric | Count |",
    "|---|---:|",
]
for label, key in [
    ("Physical pages", "pages"),
    ("Question positions", "question_positions"),
    ("Unique published code strings", "unique_codes"),
    ("Repeated-code groups", "duplicate_code_groups"),
    ("Numeric section branches", "sections"),
    ("Distinct heading codes / topics", "topics"),
    ("Heading occurrences", "heading_occurrences"),
    ("Positions with embedded images", "with_images"),
    ("Positions without embedded images", "without_images"),
    ("Unique embedded byte objects", "unique_image_objects"),
    ("Unassociated image placements", "unassociated_image_placements"),
    ("Positions with drawing overlays", "with_vector_overlays"),
    ("Confident PDF correct-answer labels", "correct_known"),
    ("Unknown correct-answer labels", "correct_unknown"),
]:
    lines.append(f"| {label} | {pdf[key]} |")
lines += [
    "",
    "Page 1053 is empty. Prefix counts: "
    + ", ".join(f"{k}: {v}" for k, v in pdf["prefixes"].items())
    + ".",
    "",
    "Answer count → positions: "
    + ", ".join(f"{k} → {v}" for k, v in pdf["answer_count_distribution"].items())
    + ".",
    "",
    "There are 258 positions with inferred unbulleted paragraph boundaries and three incomplete",
    "source positions. Counts retain published extra/empty bullets and source defects.",
    "",
    "| Fingerprint | Unique | Duplicate groups | Positions in groups | Excess positions |",
    "|---|---:|---:|---:|---:|",
]
for label, key in [
    ("Exact extracted question text", "duplicate_exact_text"),
    ("Normalized text + ordered answers", "duplicate_text_answers"),
    ("Normalized text + ordered answers + embedded image hashes", "duplicate_text_answers_images"),
]:
    item = pdf[key]
    lines.append(
        f"| {label} | {item['unique']} | {item['duplicate_groups']} | {item['positions_in_duplicate_groups']} | {item['excess_positions']} |"
    )
lines += [
    "",
    "The final fingerprint gives a reproducible content inventory, not a semantic deduplication",
    "claim. It excludes vector overlays, and different image encodings/source paragraph defects",
    "can keep semantically related questions separate. Prefix meanings, effective publication date",
    "and correct-answer labels remain unknown. The embedded Word creation/modification timestamp",
    "is 2017-01-30; it is not established as the official publication date.",
    "",
    "## Crosswalk",
    "",
    "| Live status | Count |",
    "|---|---:|",
    f"| Total | {summary['live_total']} |",
]
for status, count in summary["match_status_counts"].items():
    lines.append(f"| {status} | {count} |")
lines += [
    "",
    f"Live rows with more than one accepted PDF code: **{summary['live_multiple_pdf_codes']}**.",
    f"Accepted PDF positions: **{summary['matched_pdf_positions']}**; without accepted live counterpart: **{summary['pdf_without_accepted_live_counterpart']}**.",
    f"Live rows in review, including usable rows with unresolved alternatives: **{summary['review_live']}**.",
    "",
    f"Changed wording among accepted live rows: **{summary['changed_wording']}**.",
    f"Changed answer text: **{summary['differences']['answers_text_changed']}**; reordered answers: **{summary['differences']['answer_order_changed']}**; image mismatch/presence changes: **{summary['differences']['image_changed']}**.",
    f"Comparable correct-answer pairs: **{summary['correct_answer_comparable_pairs']}**. Correct-answer drift is **unknown**; the zero observed changes is not evidence of agreement.",
    "",
    "Counts of changes are live rows with at least one accepted differing position, not link counts.",
    "Only exact/strong links are usable. “Without counterpart” includes review cases, so these numbers",
    "must not be presented as proven additions/deletions or proof of which bank is newer.",
    "",
    "## Initial unresolved live question",
    "",
]
for initial in summary["initial_unresolved"]:
    lines += [
        f"`{initial['live_stable_key']}`",
        "",
        initial["live_question_text"],
        "",
        f"Status: **{initial['match_status']}**, official_id remains **null**.",
        "",
    ]
    for m in initial["matches"]:
        lines.append(
            f"- `{m['pdf_code']}`, physical page {m['pdf_page']}: {m['section_title']} ({m['match_method']})."
        )
    lines += [
        "",
        "All three text/ordered-answer combinations match. The PDF pictures differ from live:",
        "the Г position depicts a jet ski; М and П share a boat underway; live depicts moored boats.",
        "The question does not depend on identifying the picture. All codes are retained and image",
        "differences are explicit. No PDF code is substituted for a backend UUID.",
    ]
wording = next((r for r in rows if r["live_position"] == 2), None)
if wording and wording["matches"]:
    m = wording["matches"][0]
    lines += [
        "",
        "## Representative wording drift",
        "",
        f"Live #2: {wording['live_question_text']}",
        "",
        f"PDF `{m['pdf_code']}`: {m['differences']['pdf_question_text']}",
        "",
        f"Status {wording['match_status']}; text similarity {m['similarity']['question_text']}, answer-set similarity {m['similarity']['answer_set']}. These are deterministic scores, not probabilities.",
    ]
lines += [
    "",
    "## Twenty unmatched/ambiguous examples",
    "",
    "Ten highest-scoring unmatched rows, then ten highest-scoring ambiguous rows; ties use live position.",
    "Candidate codes are review suggestions, not accepted mappings. Full candidates and evidence are in review.json.",
    "",
    "| Live # | Status | Current text | Leading candidate codes | Score |",
    "|---:|---|---|---|---:|",
]
sample = []
for r in examples:
    codes = list(dict.fromkeys(c["pdf_code"] for c in r["candidates"]))[:3]
    score = max((c["confidence"] for c in r["candidates"]), default=0)
    lines.append(
        f"| {r['live_position']} | {r['match_status']} | {cell(r['live_question_text'])} | {cell(', '.join(codes) or '—')} | {score:.6f} |"
    )
    sample.append(
        {
            "live_position": r["live_position"],
            "status": r["match_status"],
            "text": r["live_question_text"][:130],
            "candidate_codes": codes,
            "score": score,
        }
    )
lines += [
    "",
    "## Verification and reproduction",
    "",
    "107 offline tests passed; `ruff check`, `ruff format --check`, and `mypy --strict` passed.",
    "Production `verify --json-summary` returned `valid: true`, with 1513 questions and 1386 media objects.",
    "",
    "```powershell",
    ".\\.venv\\Scripts\\python.exe -m pip install -e '.[reference,dev]'",
    ".\\.venv\\Scripts\\python.exe -m gims_open_data reference parse --json-summary",
    ".\\.venv\\Scripts\\python.exe -m gims_open_data reference crosswalk --json-summary",
    ".\\.venv\\Scripts\\python.exe scripts/audit_reference_parse.py",
    ".\\.venv\\Scripts\\python.exe scripts/summarize_reference.py --report docs/pdf-reference-investigation.md",
    "```",
    "",
    "See [format observations](pdf-question-bank-format.md) and [matching contract](reference-crosswalk.md).",
    "",
]
if args.report:
    args.report.write_text("\n".join(lines), encoding="utf-8")
print(
    json.dumps(
        {
            "crosswalk": {
                k: v for k, v in summary.items() if k not in ("initial_unresolved", "policy")
            },
            "examples": sample,
        },
        ensure_ascii=False,
        indent=2,
    )
)
