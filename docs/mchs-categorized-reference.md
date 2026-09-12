# Categorized official reference workflow

Authority is **live current content > categorized official reference > legacy
historical reference**. The categorized PDFs supply source memberships and
independent published-answer evidence. They never overwrite live text, options,
correctness, media, UUIDs or the unresolved first-question fallback key. Existing
`reference` commands and their artifacts retain their original semantics.

Install `.[reference,dev]` as described in the legacy reference documentation.
Use Python 3.12 from the repository virtual environment:

```powershell
.\.venv\Scripts\python.exe -m gims_open_data mchs-reference inventory --json-summary
.\.venv\Scripts\python.exe -m gims_open_data mchs-reference parse --json-summary
.\.venv\Scripts\python.exe -m gims_open_data mchs-reference crosswalk --snapshot 20260912T090830.657990Z --json-summary
.\.venv\Scripts\python.exe -m gims_open_data mchs-reference verify --json-summary
.\.venv\Scripts\python.exe -m gims_open_data mchs-reference all --snapshot 20260912T090830.657990Z --json-summary
```

All stages accept `--data-root`. Snapshot selection defaults to current; an
explicit ID pins the immutable comparison. `inventory` and `all` resume from
hash-verified cached public responses. `--refresh` explicitly reacquires the
seven pages and their discovered PDFs. It invalidates old derived results until
parse/crosswalk are rerun; it does not refresh or mutate the live snapshot.
No additional landing pages, document IDs, title-page links or APIs are visited.

GETs are sequential with at least 0.51 seconds between completed acquisitions.
Only transient 429/5xx/network failures receive up to three attempts, with
backoff. Redirects are validated before requests and must remain HTTPS on
`mchs.gov.ru`. Credentials, alternate ports and query-bearing resource URLs are
rejected. PDF downloads have a 50 MiB limit and MIME/signature/open/page checks.
Only a small HTTP-header allowlist is persisted; cookies are cleared before
each request. HTML token fields are redacted before storage and hashing, with
that exception to raw-byte fidelity recorded explicitly.

Inventory must contain seven successful categories and fourteen motor PDF
memberships. Failure stops parsing and remains in `summary.json`. Successful
downloads and failures are distinguished. A corpus writer lock excludes two
CLI writers; a stale lock requires explicit inspection and removal.

## Artifacts

```text
data/reference/mchs-categorized/
  manifest.json                     # source pages, hashes, production guard
  summary.json
  sources/landing-pages/<category>-<sha>.html
  pdf/objects/<sha>.pdf
  pdf/index.json                    # URL, HTTP metadata, PDF and date evidence
  documents.jsonl                   # every distinct landing/topic membership
  parsed/
    questions.jsonl
    topics.json                     # document headings and all memberships
    parse-issues.json
    summary.json
    media/objects/<sha>
data/derived/<snapshot-id>/mchs-categorized-crosswalk/
  crosswalk.jsonl                   # exactly one row per live stable key
  reference-groups.jsonl             # semantic groups over physical PDF rows
  taxonomy.json
  summary.json
  unmatched-live.json
  unmatched-reference.json
  ambiguous.json
  answer-drift.json
  review.json
  legacy-comparison.json
  order-audit.json
```

The PDF object SHA is the document ID. A position is `<document-sha>:<order>`;
local printed numbers are retained independently. Duplicate URLs are downloaded
once, byte-identical PDFs are parsed once, and all source memberships remain
attached to their questions. The normalized output exposes ship-type memberships,
sailing-area memberships, source topics, accepted source positions and rejected
candidates. Category/topic memberships are many-to-many; no single winner is
chosen among independently supported publications.

Inventory has retrieval timestamps; offline parse/crosswalk add no wall-clock
timestamps. Identical inventory, snapshot and dependency versions reproduce
offline artifacts byte-for-byte. All generated/downloaded paths are covered by
the repository's `data/` ignore rule. Production paths are protected and their
recorded hashes plus the current pointer are checked. Verification reparses
the production snapshot with its existing verifier.

## Matching and independent answer comparison

Question normalization is NFKC/case/whitespace, ё/е, common quotes/dashes and
terminal punctuation. Numbers, units, negation, directions, polarity, vessel
terms and area terms survive. Exact question text supports official
classification even when the independently published answer differs.

Printed line-wrap hypotheses require a matching published answer or strict
image evidence. Other near wording requires similarity at least 0.97, identical
critical-token signatures, and either a matching published answer of at least
20 normalized characters or strict image support. Competing near wording within
0.01 remains ambiguous. Scores are deterministic similarities, not probabilities.

Image-dependent wording requires byte/pixel identity or mean RGB thumbnail error
at most 2/255 with at most two differing dHash bits. An unavailable image,
uncompared vector overlay or mask blocks automatic image-dependent acceptance.
Ordinary contextual images may drift while exact classification remains usable.
The dependency detector is a conservative lexical rule, not source metadata.

Physical positions are retained, but repeated non-image-dependent rows are grouped
by document/topic scope and canonical question text. Image-dependent stems also
include exact illustration identity in the group key. Each group keeps every source
row and every published answer; the live answer is used only after grouping for a
cross-check. Group statuses are `supported_exact`, `supported_format_normalized`,
`unsupported`, `not_comparable` and `image_ambiguous`. A group with several
published answers is marked `source_multiple_answers`; one supporting answer prevents
classifying that group as drift, while the full answer set remains provenance.

Safe answer normalization applies Unicode NFKC, whitespace, PDF line-wrap
dehyphenation and list punctuation while preserving token order.

Published correctness uses only the independently observed PDF convention. The
earlier row-level `same` and `wording_changed_same_semantics` fields remain
available only for audit compatibility; new crosswalk decisions use group statuses.
`same` means identical strings; `wording_changed_same_semantics` permits only
documented typography, line-wrap hypotheses and matching surrounding quotes.
`different` means these safe comparisons did not establish equality. It includes
wording/list-punctuation changes as well as substantively different answers;
it does **not** by itself establish that either answer is factually wrong.
`not_comparable` and `unknown_pdf_correctness` remain distinct. No live answer
is corrected automatically. Every accepted exact-question answer discrepancy
appears in `answer-drift.json` with both texts and source evidence.

`unmatched` is no candidate above the conservative retrieval threshold;
`probable`/`ambiguous` are retained review candidates. The broader
`live_without_confident_match` includes all three. A missing accepted link is
not proof of semantic absence from the source corpus.

## Legacy audit and verification

Legacy files are first opened **after** the new matches are calculated. The
comparison reports both the strict old content crosswalk and the old canonical
classification, since their usable counts differ. Exact-text absence from the
legacy parsed corpus is reported separately from absence of an accepted link.
Topic-title set differences retain both sets and flag disjoint sets for review;
they are not automatically interpreted as semantic conflicts. Old PDF correctness
is unknown, so comparable new/old correct-answer pairs remain unavailable.

Offline verification checks schema versions, artifact sets/hashes, rediscovery
from saved HTML, URL/SHA/document correspondence, taxonomy references, PDF
validity, parsed counts and media hashes. Crosswalk verification checks complete
live stable-key coverage, source identities, memberships, answer evidence,
recomputed metrics and every review/unmatched queue. It also checks source
privacy and the recorded production hashes. Corruption fails the command.

Observed layout details and date evidence:
[mchs-categorized-pdf-format.md](mchs-categorized-pdf-format.md).
