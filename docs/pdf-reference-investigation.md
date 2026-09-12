# PDF reference investigation: full local run, 2026-09-12

Snapshot: `20260912T090830.657990Z`. PDF SHA-256: `29439e2e12df5792ab3b1ff19c530c9dc34f03973a9179c3574179365b4f84b4`.

The immutable current snapshot passed offline verification after the work. No API calls,
commits or pushes were made. All parsed datasets, reference media and crosswalk files
remain under ignored `data/`. This document contains aggregates and bounded samples only.

## PDF counts

| Metric | Count |
|---|---:|
| Physical pages | 1053 |
| Question positions | 3263 |
| Unique published code strings | 3174 |
| Repeated-code groups | 89 |
| Numeric section branches | 27 |
| Distinct heading codes / topics | 65 |
| Heading occurrences | 66 |
| Positions with embedded images | 3233 |
| Positions without embedded images | 30 |
| Unique embedded byte objects | 958 |
| Unassociated image placements | 0 |
| Positions with drawing overlays | 25 |
| Confident PDF correct-answer labels | 0 |
| Unknown correct-answer labels | 3263 |

Page 1053 is empty. Prefix counts: ВВП: 591, ВП: 207, Г: 191, М: 459, МП: 677, МТ: 529, П: 609.

Answer count → positions: 1 → 2, 2 → 6, 3 → 73, 4 → 3117, 5 → 57, 6 → 3, 7 → 2, 8 → 3.

There are 258 positions with inferred unbulleted paragraph boundaries and three incomplete
source positions. Counts retain published extra/empty bullets and source defects.

Final duplicate-code audit: 89 duplicate code strings (89 excess positions), all
with two physical positions. 82 groups have identical text, ordered answers and
image identity and represent repeated source tables. Seven are source-level code
collisions with different content: `Г.1.1.3` (266/267), `Г.3.1.` (315/316),
`Г.4.5` (317/318), `МП.1.1.` (517/530), `МТ.2.98` (787), `П.2.2.6` (998),
and `П.2.2.12` (1000). Their raw PDF text repeats the code on these pages; they
are retained as separate positions, not collapsed by the parser.

| Fingerprint | Unique | Duplicate groups | Positions in groups | Excess positions |
|---|---:|---:|---:|---:|
| Exact extracted question text | 1351 | 980 | 2892 | 1912 |
| Normalized text + ordered answers | 1605 | 1056 | 2714 | 1658 |
| Normalized text + ordered answers + embedded image hashes | 1828 | 927 | 2362 | 1435 |

The final fingerprint gives a reproducible content inventory, not a semantic deduplication
claim. It excludes vector overlays, and different image encodings/source paragraph defects
can keep semantically related questions separate. Prefix meanings, effective publication date
and correct-answer labels remain unknown. The embedded Word creation/modification timestamp
is 2017-01-30; it is not established as the official publication date.

## Crosswalk

| Live status | Count |
|---|---:|
| Total | 1513 |
| exact | 671 |
| strong | 357 |
| probable | 274 |
| ambiguous | 165 |
| unmatched | 46 |

Live rows with more than one accepted PDF code: **718**.
Accepted PDF positions: **2130**; without accepted live counterpart: **1133**.
Live rows in review, including usable rows with unresolved alternatives: **568**.

Changed wording among accepted live rows: **123**.
Changed answer text: **176**; reordered answers: **166**; image mismatch/presence changes: **151**.
Comparable correct-answer pairs: **0**. Correct-answer drift is **unknown**; the zero observed changes is not evidence of agreement.

Counts of changes are live rows with at least one accepted differing position, not link counts.
Only exact/strong links are usable. “Without counterpart” includes review cases, so these numbers
must not be presented as proven additions/deletions or proof of which bank is newer.

## Initial unresolved live question

`fallback:5c5ae3a208f04990c8f4add83e6531cada8a129f15740759842ba503bdf1a62b`

В каком из перечисленных случаев будет наилучшая управляемость судна?

Status: **exact**, official_id remains **null**.

- `Г.2.2.4`, physical page 305: Теория управления судном при выполнении расхождения, включая плавание на встречных курсах и при выполнении обгона. (exact_text_answers).
- `М.2.2.3`, physical page 419: Теория управления судном при выполнении расхождения, включая плавание на встречных курсах и при выполнении обгона. (exact_text_answers).
- `П.2.2.3`, physical page 997: Теория управления судном при выполнении расхождения, включая плавание на встречных курсах и при выполнении обгона. (exact_text_answers).

All three text/ordered-answer combinations match. The PDF pictures differ from live:
the Г position depicts a jet ski; М and П share a boat underway; live depicts moored boats.
The question does not depend on identifying the picture. All codes are retained and image
differences are explicit. No PDF code is substituted for a backend UUID.

## Representative wording drift

Live #2: На каком расстоянии рекомендуется начинать производить обгон на маломерном судне больших судов?

PDF `М.2.2.26`: На каком расстоянии, во избежание присасывания, рекомендуется производить обгон на маломерном судне больших судов?

Status strong; text similarity 0.822967, answer-set similarity 1.0. These are deterministic scores, not probabilities.

## Twenty unmatched/ambiguous examples

Ten highest-scoring unmatched rows, then ten highest-scoring ambiguous rows; ties use live position.
Candidate codes are review suggestions, not accepted mappings. Full candidates and evidence are in review.json.

| Live # | Status | Current text | Leading candidate codes | Score |
|---:|---|---|---|---:|
| 1358 | unmatched | Если оба парусных судна идут одним и тем же галсом таким образом, что может возникнуть опасность столкновения, то: | ВВП.1.2.274, ВВП.1.2.273, МП.1.1.110 | 0.672829 |
| 1307 | unmatched | Может ли экипаж изменять положение центра бокового сопротивления швертбота? | П.2.1.47, П.2.1.55 | 0.628347 |
| 1308 | unmatched | Может ли экипаж изменять положение центра бокового сопротивления килевой яхты? | П.2.1.55, П.2.1.47 | 0.628347 |
| 133 | unmatched | Для каких целей на спасательном плоту используются водобалластные карманы и плавучий якорь? | М.1.4.1, П.1.5.1 | 0.551503 |
| 1371 | unmatched | Какое из двух парусных судов должно уступить дорогу? | ВВП.1.2.25, ВП.1.2.7 | 0.536533 |
| 1372 | unmatched | Какое из двух парусных судов должно уступить дорогу? | ВВП.1.2.25, ВП.1.2.7 | 0.536533 |
| 1367 | unmatched | Как называется условная точка, к которой приложена равнодействующая аэродинамических сил, действующих на яхту? | М.1.1.33, П.1.1.33 | 0.524575 |
| 154 | unmatched | Укажите правильный порядок действий при отходе от аварийного судна? | — | 0.000000 |
| 285 | unmatched | Как называется явление образования у кромок лопастей газовых пузырьков? | — | 0.000000 |
| 288 | unmatched | Какая ошибка судоводителя может привести к посадке судна на мель? | — | 0.000000 |
| 76 | ambiguous | Какой должна быть поверхность рабочей палубы и комингсов на маломерном судне? | М.1.2.67, П.1.2.65 | 1.000000 |
| 83 | ambiguous | Как называется устройство, в состав которого входят следующие элементы: носовой роульс, стопор, цепь, лебедка, цепной ящик? | М.1.2.75, П.1.2.73 | 1.000000 |
| 207 | ambiguous | На каком из этих судов (А или Б) при движении против течения правильно производится поворот на обратный курс? | П.2.1.13, Г.2.1.13, М.2.1.13 | 1.000000 |
| 653 | ambiguous | Какой из изображенных на иллюстрации береговых навигационных информационных знаков запрещает отдавать якоря? | ВВП.1.2.65, ВВП.1.2.67, ВВП.1.2.66 | 1.000000 |
| 674 | ambiguous | Укажите огни судна, занятого ловом рыбы. | ВВП.1.2.130, ВВП.1.2.129 | 1.000000 |
| 978 | ambiguous | Какой тип волнения показан на рисунке? | МП.3.53, МП.3.54, МП.3.57 | 1.000000 |
| 979 | ambiguous | Какой тип волнения показан на рисунке? | МП.3.53, МП.3.54, МП.3.57 | 1.000000 |
| 1008 | ambiguous | Какой тип волнения показан на рисунке? | МП.3.53, МП.3.54, МП.3.57 | 1.000000 |
| 1197 | ambiguous | Что обозначает изображенный на иллюстрации плавучий навигационный знак? | ВВП.1.2.10, ВВП.1.2.88, ВВП.1.2.13 | 1.000000 |
| 1199 | ambiguous | Что обозначает изображенный на иллюстрации плавучий навигационный знак? | ВВП.1.2.14, ВВП.1.2.10, ВВП.1.2.88 | 1.000000 |

## Verification and reproduction

107 offline tests passed; `ruff check`, `ruff format --check`, and `mypy --strict` passed.
Production `verify --json-summary` returned `valid: true`, with 1513 questions and 1386 media objects.

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[reference,dev]'
.\.venv\Scripts\python.exe -m gims_open_data reference parse --json-summary
.\.venv\Scripts\python.exe -m gims_open_data reference crosswalk --json-summary
.\.venv\Scripts\python.exe scripts/audit_reference_parse.py
.\.venv\Scripts\python.exe scripts/summarize_reference.py --report docs/pdf-reference-investigation.md
```

See [format observations](pdf-question-bank-format.md) and [matching contract](reference-crosswalk.md).
