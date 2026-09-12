"""Sequential, bounded, conditional downloads into an immutable content-addressed cache."""

import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
from pydantic import Field

from .client import ImportStopped
from .models import Model, Question, Resource
from .storage import atomic_json

LOG = logging.getLogger("gims_open_data")
MAX_BYTES = 25 * 1024 * 1024


class MediaRecord(Model):
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)
    content_type: str
    content_length: int | None
    etag: str | None
    last_modified: str | None
    retrieved_at: str
    checked_at: str


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def all_resources(questions: list[Question]) -> list[Resource]:
    return [r for q in questions for r in q.resources + [r for a in q.answers for r in a.resources]]


def valid_media(content_type: str, body: bytes) -> bool:
    signatures = {
        "image/jpeg": body.startswith(b"\xff\xd8\xff"),
        "image/png": body.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": body.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": body.startswith(b"RIFF") and body[8:12] == b"WEBP",
        "image/bmp": body.startswith(b"BM"),
        "image/svg+xml": b"<svg" in body[:4096].lower() and b"<html" not in body[:4096].lower(),
        "application/pdf": body.startswith(b"%PDF-"),
        "video/mp4": body[4:8] == b"ftyp",
        "audio/mpeg": body.startswith(b"ID3")
        or body[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"},
        "audio/ogg": body.startswith(b"OggS"),
        "image/avif": body[4:8] == b"ftyp" and b"avif" in body[:40],
    }
    return signatures.get(content_type, False)


def read_index(root: Path) -> dict[str, Any]:
    path = root / "media/index.json"
    if not path.exists():
        return {"schema_version": "1.0", "urls": {}, "versions": {}}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0" or not isinstance(data.get("urls"), dict):
        raise ImportStopped("Invalid media index", "validation")
    return data


class MediaDownloader:
    def __init__(
        self,
        root: Path,
        delay: float = 1.0,
        *,
        attempts: int = 3,
        max_bytes: int = MAX_BYTES,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(delay) or delay < 0 or attempts < 1 or max_bytes < 1:
            raise ValueError("invalid media limits")
        self.root, self.delay, self.attempts = root, delay, attempts
        self.max_bytes, self.sleep = max_bytes, sleep
        self.last_finished: float | None = None
        self.stats = {"requests": 0, "downloads": 0, "not_modified": 0}
        self.index = read_index(root)
        (root / "media/objects").mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "gims-open-data/1.0", "Accept-Encoding": "identity"},
        )

    def __enter__(self) -> "MediaDownloader":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.http.close()

    def download(self, url: str) -> MediaRecord:
        Resource(url=url)  # Validate before any request, including cached URLs.
        cached_data = self.index["urls"].get(url)
        cached = MediaRecord.model_validate(cached_data) if cached_data else None
        headers = {}
        if cached:
            path = self.root / "media/objects" / cached.sha256
            if (
                not path.exists()
                or path.stat().st_size != cached.bytes
                or file_hash(path) != cached.sha256
            ):
                raise ImportStopped("Cached media object missing/corrupt", "validation")
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        for attempt in range(self.attempts):
            try:
                record = self._get(url, headers, cached)
                self.index["urls"][url] = record.model_dump()
                self.index["versions"].setdefault(url, {})[record.sha256] = record.model_dump()
                atomic_json(self.root / "media/index.json", self.index)
                return record
            except httpx.TransportError:
                error = ImportStopped("Media GET network failure", "transient")
            except ImportStopped as exc:
                if exc.kind != "transient":
                    raise
                error = exc
            if attempt + 1 == self.attempts:
                raise error from None
            self.sleep(min(60, 2 ** (attempt + 1)))
        raise AssertionError("unreachable")

    def _get(self, url: str, headers: dict[str, str], cached: MediaRecord | None) -> MediaRecord:
        if self.last_finished is not None:
            self.sleep(max(0, self.delay - (time.monotonic() - self.last_finished)))
        try:
            self.stats["requests"] += 1
            with self.http.stream("GET", url, headers=headers) as response:
                now = datetime.now(UTC).isoformat()
                if response.status_code == 304:
                    if not cached or not headers:
                        raise ImportStopped("Unexpected media 304")
                    self.stats["not_modified"] += 1
                    return cached.model_copy(update={"checked_at": now})
                if response.status_code == 429 or response.status_code >= 500:
                    raise ImportStopped(f"Media HTTP {response.status_code}", "transient")
                if response.status_code != 200:
                    raise ImportStopped(f"Media HTTP {response.status_code}; redirects refused")
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                length_header = response.headers.get("content-length")
                try:
                    length = int(length_header) if length_header is not None else None
                except ValueError:
                    raise ImportStopped("Invalid media Content-Length") from None
                if length is not None and (length <= 0 or length > self.max_bytes):
                    raise ImportStopped("Media exceeds maximum file size or is empty", "validation")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ImportStopped("Media exceeds maximum file size", "validation")
                data = bytes(body)
                if length is not None and len(data) != length:
                    raise ImportStopped("Media length mismatch", "transient")
                if not data or not valid_media(content_type, data):
                    raise ImportStopped(
                        "Unsupported media Content-Type or invalid bytes", "protocol"
                    )
                sha = hashlib.sha256(data).hexdigest()
                self.stats["downloads"] += 1
                path = self.root / "media/objects" / sha
                if path.exists():
                    if file_hash(path) != sha:
                        raise ImportStopped("Corrupt content-addressed object", "validation")
                else:
                    temp = path.with_suffix(".tmp")
                    with temp.open("wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, path)
                return MediaRecord(
                    url=url,
                    sha256=sha,
                    bytes=len(data),
                    content_type=content_type,
                    content_length=length,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    retrieved_at=now,
                    checked_at=now,
                )
        finally:
            self.last_finished = time.monotonic()

    def collect(
        self, questions: list[Question], *, verbose: bool = False
    ) -> dict[str, MediaRecord]:
        urls = sorted({r.url for r in all_resources(questions)})
        records = {}
        for number, url in enumerate(urls, 1):
            records[url] = self.download(url)
            if verbose or number == 1 or number % 50 == 0 or number == len(urls):
                LOG.info("Media: %d/%d", number, len(urls))
        for resource in all_resources(questions):
            record = records[resource.url]
            resource.sha256, resource.bytes, resource.content_type = (
                record.sha256,
                record.bytes,
                record.content_type,
            )
        return records
