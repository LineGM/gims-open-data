# Categorized MChS PDFs: observed format

Investigated on 2026-09-12. The input is the seven explicitly enumerated landing
pages in `mchs_reference_inventory.SOURCES`, not the historical 1053-page bank.
The first successful inventory contains 72 distinct PDF URLs and 72 SHA-256
objects, 934 physical pages and 70,589,869 bytes. All seven pages returned 200;
all PDFs passed MIME, signature and PyMuPDF validation. There were no redirects.
MMS discovery independently returned 14 documents. Its topic codes agree with
the supplied sanity checklist. No topic URL or expected title list is hardcoded.

## Landing structure and provenance

Each `.doc-item` has a `.doc-item__title` link and a separate PDF download link.
The download link's exact text is `Скачать` with surrounding markup whitespace;
the topic label comes from the title in the same card. The title link itself is
not followed. Category and dimension are inherited from the enumerated landing
page. Link occurrences, URL deduplication and byte deduplication are separate.
All memberships of a byte-identical PDF survive parsing.

The labels include explicit topic numbers and category suffixes, such as
`1.1. Основы теории судна (ммс)`. One hydrocycle label has no space between its
number and title. Landing titles are retained exactly, including punctuation
and suffixes. Internal headings are separate evidence: sailing 2.2 says
`2.2 Управление парусным судном`, while its landing card has the longer title
about ship handling, meeting and overtaking. The parser does not replace either.

Both copies of the footer on the inspected pages contain this notice:

> Все материалы сайта доступны по лицензии:
> Creative Commons «Attribution» 4.0
> Всемирная

Exact extracted text, source URL and retrieval time are saved per page. This is
a record of the site's published notice, not a broader legal conclusion. PDFs,
HTML, media and generated datasets remain in ignored `data/`.

## Geometry and text

First-page layouts were inspected in motor 1.1, sailing 1.1, hydrocycle 1.1,
special construction 1.3.1, VVP 1.1, VP 1.1 and MP 1.1. Rendered pages and text
spans were compared, including a four-option exception. A separate offline
census traversed all 934 pages: no entirely textless page was found.

The pages are landscape, nominally 841.92 × 595.32 points. Four columns contain
question number, question, illustration, and published answer representation.
Numbers are local integers, usually with a final period; they are not UUIDs or
topic-qualified codes. Numbering restarts in each PDF. Number cells and text
cells are independently vertically centred, so number baselines are not row
boundaries.

Word exports table borders as narrow filled rectangles. The parser measures
horizontal first-cell rules and vertical column rules per row. It supports the
observed left edge at 30.36 points in sailing 2.2 as well as approximately
49–56 points elsewhere. Column cuts are not shared with the legacy parser.
The first-cell right edge can be 71.16, 84.72, 91.8 or 99.024 points; illustration
and answer columns also vary. PyMuPDF's general `find_tables` can split these
tables into extra cells, so it is not the parsing contract.

Topic headings and, in seven 1.1 documents, a general document title appear
before the first table. Repeated headers contain `No`, `Вопрос`, `Иллюстрация`
and `Варианты ответа (правильный выделен)`. The last phrase sometimes wraps
with a printed hyphen. Header rows are excluded from question records.

There are four question-row continuations across physical pages: motor 1.4
number 53, VVP 1.2 numbers 239 and 253, and MP 3.1 number 186. An unnumbered
first data row may continue the preceding page's last numbered row. Page order
is preserved before sorting spans by coordinates. An unexpected numbered or
malformed row is reported independently and cannot become such a continuation.
There is no observed need to invent missing cells or merge unrelated rows.

Question and answer text have many printed line-end hyphens. Raw line breaks
and printed characters are retained. Whitespace-normalized strings keep those
hyphens. Matching separately considers joined and retained-hyphen hypotheses,
requiring corroboration; a line-end hyphen is not unconditionally deleted from
the published text. `scripts/probe_mchs_categorized.py` writes a bounded layout
census under the ignored corpus namespace.

## Published answers and correctness

Ordinary text is Times New Roman, mainly 11.04 points. Correct answer text is
TimesNewRomanPS-BoldMT with the bold bit 16 (observed flags 20 including serif).
SymbolMT supplies U+F0B7 bullets, and Arial spans supply spaces. The legend says
the correct answer is highlighted; rendered samples show bold text, not a
colour/highlight convention. Correctness requires that legend and exactly one
answer whose substantive spans are all bold. Otherwise it remains unknown.

Of 2,603 parsed positions, **2,595 contain one published answer**. They use
`published_answer_only`, never a fabricated option set. **Eight contain four
separately bulleted options, exactly one bold**, and use `full_options`:

- hydrocycle 2.2 / 26;
- MP 1.4 / 11 and MP 1.2 / 77, 110;
- sailing 1.2 / 14 and sailing 1.4 / 3, 47;
- motor 1.4 / 3.

All 2,603 positions in this acquisition have reliable extractable published
correctness under that convention. Live answers never supply a PDF label.
Unexpected multi-paragraph structures or ambiguous styling abstain. The parser
is intentionally scoped to these observed layouts, not arbitrary styled PDFs.

## Illustrations and anomalies

Illustrations are associated by placement geometry inside each row's illustration
cell. Source PDF SHA, page, xref, bounding box, mask metadata, dimensions, format,
MIME, bytes and content SHA are retained. One image in sailing 2.2 page 4 has
no recoverable xref; its original bytes are extracted from the matching image
block instead. Header images are document media, not question illustrations.

Embedded bytes and drawing overlays are separate evidence. Eighteen positions
have drawing paths inside their illustration cells. Automatic matches for
image-dependent questions abstain when those overlays/masks have not been
compared. Byte equality, decoded pixel equality and strict thumbnail similarity
are distinct outcomes. A visual mismatch in an ordinary contextual image is
drift evidence, not permission to replace live media.

The final question numbering in sailing 4.1 is **34, 36**. Inspection of the
source text layer confirms this gap. There are 35 actual positions in that
document. The parser retains `36` and reports `nonsequential_source_numbers`;
it does not synthesize number 35. The four continuations are informational
question issues, not missing questions.

## Dates and document scope

The identical raster approval stamp occurs on page 1 of the seven 1.1 PDFs.
It was visually inspected, and its date reads **«01» июля 2022 г.** The stamp
says `УТВЕРЖДАЮ`, names the chief state inspector for small vessels and
Д.В. Тарасов. Its extracted image SHA-256 is
`35a636694222c5635428b40d13bcbd5966915f422335ec41deac8930a982b933`.

Inventory reuses that transcription only when the extracted image bytes have
this exact SHA. Evidence records its image SHA and placement; this is explicit
visual transcription, not OCR or an inferred date for all 72 PDFs. Unrecognized
stamps remain unlabelled. No effective revision date was established. The
`2024-02-09` storage path date, retrieval time, HTTP Last-Modified, PDF metadata
timestamps, visible approval date and an explicit revision are separate fields.
The landing pages' current presence does not establish a publication timestamp.

Motor 1.1 and sailing 1.1 explicitly include `(парусно-моторным судном)` in their
document headings. Their questions retain this as additional source-derived
evidence. It is not an eighth invented category, and is not extended to other
documents without that heading evidence.
