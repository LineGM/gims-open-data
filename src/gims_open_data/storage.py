"""Durable local artifacts; raw bodies are never reserialized or overwritten."""

import hashlib
import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def mask_secrets(raw: bytes, secrets: set[str], *, is_html: bool) -> bytes:
    values = set(secrets)
    if is_html:
        soup = BeautifulSoup(raw, "html.parser", from_encoding="utf-8")
        for tag in soup.select("input[value], meta[content]"):
            name = " ".join(str(tag.get(key, "")) for key in ("name", "id", "type"))
            if re.search(
                r"token|csrf|secret|password|authorization|session|activation", name, re.I
            ):
                values.add(str(tag.get("value", tag.get("content", ""))))
    else:
        # Added fields are tolerated, but unknown auth values must not leak to raw.
        def collect(value: object, sensitive: bool = False) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    marked = sensitive or bool(
                        re.search(
                            r"csrf|token|secret|password|authorization|cookie|phpsessid|"
                            r"activation|start_(?:id|uuid|code)",
                            str(key),
                            re.I,
                        )
                    )
                    collect(item, marked)
            elif isinstance(value, list):
                for item in value:
                    collect(item, sensitive)
            elif sensitive and isinstance(value, str):
                values.add(value)

        try:
            collect(json.loads(raw))
        except (ValueError, UnicodeError):
            pass  # Preserve malformed question bodies for diagnostics.
    for value in sorted(values, key=len, reverse=True):
        if value:
            variants = {
                value,
                html.escape(value),
                quote(value, safe=""),
                json.dumps(value, ensure_ascii=True)[1:-1],
            }
            for variant in variants:
                raw = raw.replace(variant.encode("utf-8"), b"REDACTED_SECRET")
    return raw


def save_raw(path: Path, body: bytes, secrets: set[str]) -> dict[str, Any]:
    saved = mask_secrets(body, secrets, is_html=path.suffix == ".html")
    with path.open("xb") as stream:
        stream.write(saved)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "file": f"raw/{path.name}",
        "bytes": len(saved),
        "sha256": hashlib.sha256(saved).hexdigest(),
        "secrets_masked": saved != body,
    }
