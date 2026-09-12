# Reference PDF: observed format and parser contract

Scope: `data/reference/gims-attestation-question-bank.pdf`, SHA-256
`29439e2e12df5792ab3b1ff19c530c9dc34f03973a9179c3574179365b4f84b4`.
Investigated locally on 2026-09-12 with PyMuPDF. No OCR, external question bank,
network API, or bulk model-based matching was used. This is a parser for this
observed Word table layout, not a general-purpose PDF table extractor.

## Observed

The file has **1053 physical landscape pages**, each 841.92 × 595.32 PDF points.
Page 1053 has no text. Physical page numbers in all artifacts are one-based;
answer indexes are zero-based, while question and answer `order` are one-based.
There is a usable Cyrillic text layer throughout the 1052 content pages.

The document begins directly with `ВВП.1.1.` and a table, without a title page.
Four columns contain code, question, illustration, and answer variants. Column
headings include `Варианты ответа (правильный выделен)`. Word exports the table
borders as thin filled rectangles. Row boundaries must come from these rules:
codes and question/answer blocks are vertically centred independently, so a
code's y-coordinate is not the start of its question. The column boundaries
change between sections and occasionally within a page. The parser derives
them per row inside measured coordinate ranges; it does not use fixed cuts.

Most pages contain several question positions. Six positions have row
continuations on the next page. Headings and new tables also begin mid-page.
Some PyMuPDF lines combine a code and question spans across columns; the parser
separates spans using geometry before assigning them to cells. No complete
document text is sent to the model. Probe scripts save their detailed results
under ignored `data/reference/` and print bounded samples or aggregates.

### Codes and headings

Observed prefixes, without expanded meanings: **ВВП, ВП, Г, М, МП, МТ, П**.
Normal question codes have two or three numeric components after the prefix,
for example `ВВП.3.82` and `ВВП.1.1.1`. Published exceptions are retained:
`М.423`, `М.424`, `ВП.1.1.`, `Г.3.1.`, `МП.1.1.`, `П.2.1.79.`, and
`П.2.2. 20`. Missing components are not repaired or inferred from neighbouring
numbers. `code` preserves the extracted spelling and internal whitespace;
`code_normalized` removes whitespace only. No backend identifiers are invented.

Headings consist of a prefix, one or two numeric levels, an optional final dot,
whitespace and a title; titles can wrap. For example:

- `ВВП.1.1. Нормативные правовые акты Российской Федерации, регулирующие безопасность судоходства`
- `ВВП.3. Основы навигации и радиосвязи в районе плавания`
- `Г.1.3 Уход за судовым двигателем.`

There are **66 heading occurrences and 65 distinct heading codes**, distributed
over **27 prefix + first-number branches**. These are two different counts:
some first-number branches have their own title, others only titled subsections.
The taxonomy retains all heading occurrences, their pages, exact line breaks,
and separately normalized heading/title forms. Untitled parent nodes have no
invented official title. A question inherits the preceding published heading;
code/heading disagreements are issues, not silently repaired classifications.

Codes are not unique: there are 89 repeated-code groups. In particular,
`ВВП.3.1` through `ВВП.3.82` are reused after a repeated heading. A position is
addressed by PDF SHA-256, page, and sequential order, with its published code
retained. Multiple codes for the same content are valid classification links.

The final position audit found 89 duplicate code strings (89 excess positions;
every duplicate group has two positions). **82 groups repeat identical question,
ordered answers and embedded image identity**, usually because the same `ВВП.3`
table is printed twice (for example `ВВП.3.1` appears on pages 141 and 169).
Seven groups are source-level code collisions with different content, not parser
duplication: `Г.1.1.3` (pages 266/267), `Г.3.1.` (315/316), `Г.4.5` (317/318),
`МП.1.1.` (517/530), `МТ.2.98` (787), `П.2.2.6` (998), and `П.2.2.12`
(1000). The raw page text itself contains each repeated code; the parser keeps
both positions and does not overwrite one with the other. These seven cases are
retained as published irregularities and should be reviewed by consumers that
need a unique code-to-content mapping.

### Answers and correctness

Most answer paragraphs start with a Symbol `U+F0B7` bullet followed by an Arial
space and Calibri text. Wrapped lines have no new bullet. Some paragraphs have
erroneous extra bullets: on page 1, `л.с.)` is a separate bullet in the source.
It remains a separate parsed answer, with differences available for review.
Blank answer bullets and a completely blank coded position also exist.

258 positions contain unbulleted answer paragraphs. Their layout uses a small
paragraph gap, generally about 0.6 points greater than wrapped-line spacing.
The parser records `boundary_method=paragraph_spacing` and
`answer_boundaries_inferred`. It derives the two spacing clusters within a row;
this is less certain than explicit bullets. Broken paragraphs in the source
remain broken; the parser never copies live answers into PDF records to make
them match. Answer count distributions therefore describe extracted source
paragraphs, not a claim that every source question is a valid test item.

**Despite the column legend, this file does not provide an extractable correct
answer marker.** Across the full file, answer text uses ordinary Calibri, mainly
11.04 points; other observed sizes are 11.52 and 12.0. There are no bold spans.
Flags 1 on 26 spans are superscript detection, not bold (flag 16); Times New
Roman flag 4 denotes serif. Two red text spans and five red drawing paths do
not form an answer-label convention. Grey fills are table headers. There are
no yellow answer backgrounds. A rendered page and its text traces also showed
ordinary unmarked answer text, not synthetic bold text rendering.

All 3263 positions consequently have `correct_answer_indexes=null` and all
`correct_pdf` flags are null. The generic style routine supports one consistently
bold or yellow-highlighted option and otherwise abstains. Synthetic fixtures
exercise those cases. There are **zero comparable PDF/live correct-answer
pairs**, so agreement accuracy and correct-answer drift are **unknown**, not
100% agreement or zero proven changes. Live correctness never fills PDF labels.

### Illustrations

3233 embedded raster image placements are geometrically associated with question
rows; 30 coded positions have no embedded image. Content-addressed extraction
deduplicates 958 unique byte objects. Each placement records xref, page, bbox,
dimensions, extension, SHA-256, mask presence and column role. Embedded bytes are
stored once under `data/reference/media/<sha256>`. Drawing overlays inside the
illustration cell are retained separately as `graphic_refs`; image-dependent
questions with these overlays require review because embedded-image comparison
alone does not verify the complete illustration.

No embedded PDF image SHA-256 equals a live media SHA-256. Small inspected pairs
and aggregate pixel comparisons show resizing/recompression, including changed
aspect ratios. Other pictures have actually been replaced. Image evidence
therefore distinguishes byte identity, decoded-pixel identity, visual similarity,
presence differences and unavailable comparisons. Similarity is not byte identity.

### Provenance and age

PDF metadata: format PDF 1.5; creator and producer Microsoft Word 2010; author
`g534_fde`; creation and modification timestamps both
`D:20170130173659+03'00'`. Metadata title, subject and keywords are empty.
No publication/revision date, publisher, document title or version was established
from the document, so those fields remain null. The software creation timestamp
is preserved as raw metadata and is **not relabelled as publication date**.

## Inferred

The prefix/number tree represents the observed grouping; expanded names for the
seven prefixes are not established by headings alone. Terms such as category,
area, section and topic must be read through the published titles and machine
codes. A separate unsupported expansion table is intentionally absent.

The embedded creation date and observed wording/image differences are consistent
with an older reference export. They cannot establish the effective date of every
question. The current live snapshot remains authoritative for text, options,
correctness, media and UUIDs.

Exact content counts use explicitly defined fingerprints. The question + answers
fingerprint includes normalized text and ordered answers; the image variant adds
the sorted list of embedded image byte hashes. It does not assert semantic
equivalence across different pictures, source paragraph defects or vector overlays.
Consequently a semantic count of all “really identical questions” remains unknown;
the reproducible content-fingerprint counts are reported instead.

## Unknown and reviewable cases

Correct-answer labels throughout this particular file; missing code components;
the effective publication/revision date; and semantic equivalence of unresolved
crosswalk candidates remain unknown. Three source positions have incomplete
content: `П.1.2.31` (page 879), `П.1.2.103` (903), `П.2.3.12` (1009).
They remain in the position inventory and cannot supply usable automatic links.

The separate audit traverses all page structures and compares row coverage,
unassigned text and image totals. Full-run metrics and representative review cases
are recorded in [pdf-reference-investigation.md](pdf-reference-investigation.md).
