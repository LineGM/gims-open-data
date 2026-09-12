"""Separate CLI stage orchestration with an exclusive categorized-corpus writer lock."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .reference_pdf import safe_output


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    corpus = root / "reference/mchs-categorized"
    safe_output(corpus, root)
    corpus.mkdir(parents=True, exist_ok=True)
    path = corpus / ".writer.lock"
    try:
        stream = path.open("x")
    except FileExistsError:
        raise ValueError(
            "Categorized writer lock exists; inspect running process before removal"
        ) from None
    try:
        with stream:
            yield
    finally:
        path.unlink()


def run(stage: str, root: Path, snapshot: str | None, *, refresh: bool = False) -> dict[str, Any]:
    from .mchs_reference_inventory import inventory
    from .mchs_reference_match import crosswalk
    from .mchs_reference_pdf import parse
    from .mchs_reference_verify import verify, verify_inventory, verify_parsed

    if stage == "verify":
        return verify(root, snapshot)
    with writer_lock(root):
        if stage == "inventory":
            result = inventory(root, refresh=refresh)
            verify_inventory(root)
        elif stage == "parse":
            result = parse(root)
            verify_parsed(root)
        elif stage == "crosswalk":
            result = crosswalk(root, snapshot)
            verify(root, snapshot)
        elif stage == "all":
            inv = inventory(root, refresh=refresh)
            verify_inventory(root)
            parsed = parse(root)
            verify_parsed(root)
            matched = crosswalk(root, snapshot)
            result = {
                "inventory": inv,
                "parse": parsed,
                "crosswalk": matched,
                "verify": verify(root, snapshot),
            }
        else:
            raise ValueError("Unknown categorized stage")
    return result
