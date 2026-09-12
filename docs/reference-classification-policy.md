# Offline reference topic classification

`reference classification` is a parallel derived layer. It answers which
published PDF section or topic a current live question belongs to. It does not
assert that the live question is the same content version as a PDF row.

The strict content crosswalk in `reference_match.py` is unchanged. The
classifier copies its `match_status` into `original_content_status` and uses
its candidate positions as additional evidence. It never uses the PDF's
unknown answer correctness.

Classification-only normalization applies Unicode NFKC, case folding, `ё` to
`е`, whitespace and ordinary quote/dash normalization, terminal punctuation
removal, and homoglyph replacement only inside very short label-like tokens.
It does not remove numbers, negation, direction, vessel type, polarity, or
units.

`exact_topic` requires exact normalized question text and a single compatible
topic group. Multiple PDF codes and positions remain in the result. Codes
from the same section or from branches with the same substantive topic title
do not create ambiguity by themselves. A strict image match may resolve
otherwise competing image variants.

`strong_topic` requires close wording, unchanged critical tokens, and a strong
corroborating signal from answers or strict image evidence. `probable` and
`ambiguous` are review-only. `unmatched` means that no conservative thematic
candidate was found. An image-dependent question with an unsupported image
variant cannot be auto-accepted.

The CLI is fully offline:

```text
python -m gims_open_data reference classify --json-summary
```

It writes ignored artifacts under the current snapshot's
`reference-classification/` directory and does not edit the snapshot itself.
