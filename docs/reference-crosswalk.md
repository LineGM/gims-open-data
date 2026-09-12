# Offline reference workflow

Install the optional local parser dependencies and development checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[reference,dev]'
.\.venv\Scripts\python.exe -m gims_open_data reference parse --json-summary
.\.venv\Scripts\python.exe -m gims_open_data reference crosswalk --json-summary
```

The default PDF is `data/reference/gims-attestation-question-bank.pdf`.
`reference parse` accepts `--pdf`, `--parsed`, `--media`, and `--data-root`;
`reference crosswalk` accepts the last three options. Explicit paths are interpreted
relative to the working directory. Optional dependencies are imported only when
the reference commands are called; ordinary sync/verify/status does not need them.

Parse outputs are `questions.jsonl`, `sections.json`, `summary.json` and
`parse-issues.json` under `data/reference/parsed/`. The parser records input SHA-256
and output checksums. Crosswalk checks the current pointer/manifest and questions
checksum, validates live question models, validates parsed checksums and PDF
provenance, and checks every compared media object's SHA-256. It does not contact
the network. Unsupported media becomes an explicit evidence issue.

Crosswalk always resolves `data/state/current.json`. It writes only
`data/derived/<resolved-snapshot-id>/reference-crosswalk/`. Outputs:

- `crosswalk.jsonl`: one row per live stable key; accepted links and review candidates.
- `taxonomy.json`: the published PDF heading tree and all heading occurrences.
- `summary.json`: counts, input hashes, comparison policy and unresolved initial item.
- `unmatched-live.json`: all live rows without an accepted counterpart, including review.
- `unmatched-pdf.json`: all PDF positions without an accepted live counterpart.
- `ambiguous.json`, `changed-wording.json`, `review.json`: explicit review selections.

JSON queue files wrap their list in `records`; parse issues wrap the list in `issues`.
Generated artifacts have stable ordering and no wall-clock generation timestamps.
Use identical input bytes and dependency versions for byte-reproducible results.
The full PDF and generated media/data are ignored by Git. Snapshot, state and
production media paths are protected against reference output writes.

## Deterministic matching policy

Normalization is Unicode NFKC, case folding, whitespace collapse, ё/е and common
quote/dash variants. It retains numbers, punctuation and negations. Original PDF
and live text are kept separately. Scores are deterministic similarities, not
estimated probabilities.

1. Index normalized text + ordered answers for exact candidates; test media identity
   separately. `exact_text_answers_media` means byte/pixel equality or both absent;
   `exact_text_answers` means exact normalized text/options without identity proof.
2. Index text and answer multisets for reordered options and near wording. Compare
   answers by deterministic optimal assignment for up to seven options and sorted
   similarity for larger lists. Record ordered and unordered scores separately.
3. For live items without exact candidates, generate at most 40 deterministic
   RapidFuzz question candidates with text similarity at least 0.55. No LLM is used.
4. A near link is strong when text similarity is at least 0.92 and answer-set
   similarity at least 0.96, or when text similarity is at least 0.80 and answers
   are identical under assignment with at least 70 total normalized characters.
   Changed numeric/negation/directional tokens in question text block this promotion.
   Exact question + reordered identical answer multiset is also strong.
5. Review competition: distinct near text/options within 0.025 of the leading score
   prevent automatic selection when there is no exact match. Multiple PDF codes for
   identical content are retained together and are not treated as competitors.

The displayed score is `0.6 × question + 0.3 × answer-set + 0.1 × answer-order`.
Image evidence gates acceptance separately rather than being hidden inside this
score. PDF/live correctness agreement is retained as a separate field-level
difference; a known disagreement is drift evidence, never a reason to replace live.

The parser keeps image identity even for identical wording. Image-dependent wording
(references to illustrations, diagrams, marked objects, or only short code-like
answers) requires byte/pixel identity or strict visual support. Different image
variants without one supported group remain probable/ambiguous. For ordinary text
questions, identical text/options may legitimately occur with different contextual
pictures: retain all codes and report the media difference. This dependency test
is a conservative lexical inference, stored in each match, not a PDF fact.

Visual support compares RGB thumbnails and a difference hash after resizing;
strict support requires mean RGB error ≤2/255 and at most two differing hash bits.
Broader support uses ≤8/255 and six bits. Aspect ratio differences are recorded,
but do not veto support because the observed PDF stretches some source pictures.
Very small diagram changes can evade perceptual comparison; these scores never
claim byte identity. Vector overlays, incomplete source content and live answer
media prevent automatic acceptance where comparison is insufficient.

Only **exact/strong** links have `classification_usable=true`. Probable/ambiguous
rows and rejected image alternatives are retained in review. A row with a leading
candidate score below 0.70 is unmatched; its low-score candidates may still help
manual investigation. A usable row may also have unaccepted classification
alternatives in review. Absence of an accepted counterpart is not proof that a
question is absent from the other source.

Differences include wording, answer text, answer order, correctness and image
evidence, plus unmatched answer strings and an explicit live-to-PDF alignment.
For images, true means changed presence or visual mismatch, false means identity,
and null means uncertain (including similar recompressed images). Correct-answer
change is null without a confidently labelled PDF answer and a complete alignment.
Live/backend UUIDs are copied unchanged; PDF codes never become UUIDs.

## Validation and bounded investigation

```powershell
.\.venv\Scripts\python.exe scripts/probe_reference_pdf.py
.\.venv\Scripts\python.exe scripts/probe_pdf_layout.py
.\.venv\Scripts\python.exe scripts/audit_reference_parse.py
.\.venv\Scripts\ruff.exe check
.\.venv\Scripts\ruff.exe format --check
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\mypy.exe --strict
.\.venv\Scripts\python.exe -m gims_open_data verify --json-summary
```

Tests generate a two-page PDF and use small structured fixtures for Cyrillic
codes, headings, wrapped answers, page continuation, image association, bold and
highlight correctness, duplicate-image ambiguity, matching stages, drift,
many-code mappings, unresolved UUIDs, determinism and snapshot immutability.
The real 1053-page PDF is never a test fixture.
