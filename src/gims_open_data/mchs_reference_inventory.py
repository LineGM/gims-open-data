"""Bounded, sequential acquisition of the seven official categorized sources."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .media import file_hash
from .reference_pdf import safe_output, write_jsonl
from .snapshot import read_json
from .storage import atomic_json, mask_secrets

VERSION = "mchs-categorized-1"
BASE = (
    "https://mchs.gov.ru/deyatelnost/attestaciya-i-akkreditaciya/"
    "attestaciya-sudovoditeley/voprosy-dlya-attestacii-sudovoditeley/"
)
SOURCES = [
    ("ship_type", "motor", BASE + "razdel-tip-sudna/malomernoe-motornoe-sudno-plus"),
    ("ship_type", "sailing", BASE + "razdel-tip-sudna/malomernoe-parusnoe-sudno-plus"),
    ("ship_type", "hydrocycle", BASE + "razdel-tip-sudna/gidrocikl-plus"),
    ("ship_type", "special_construction", BASE + "razdel-tip-sudna/sudno-osoboy-konstrukcii-plus"),
    ("sailing_area", "vvp", BASE + "razdel-rayon-plavaniya/vvp-plus"),
    ("sailing_area", "vp", BASE + "razdel-rayon-plavaniya/vp-plus"),
    ("sailing_area", "mp", BASE + "razdel-rayon-plavaniya/mp-plus"),
]
MAX_PDF = 50 * 1024 * 1024
LOG = logging.getLogger(__name__)
# Visually transcribed once; reused only for byte-identical embedded stamp images.
# Evidence and limits are documented in docs/mchs-categorized-pdf-format.md.
APPROVAL_IMAGE = "35a636694222c5635428b40d13bcbd5966915f422335ec41deac8930a982b933"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def allowed_url(url: str) -> str:
    value = urlsplit(url)
    if (
        value.scheme != "https"
        or value.hostname != "mchs.gov.ru"
        or value.port not in (None, 443)
        or value.username
        or value.password
        or value.fragment
        or value.query
        or "\\" in url
        or any(p in {".", ".."} for p in unquote(value.path).split("/"))
    ):
        raise ValueError("URL outside HTTPS mchs.gov.ru allowlist")
    return url


class PublicClient:
    """No cookie reuse/persistence; manual allowlisted redirects, bounded bodies."""

    def __init__(self, client: httpx.Client, delay: float = 0.51) -> None:
        self.client = client
        self.delay = delay
        self.last_request = 0.0

    def get(self, url: str, *, pdf: bool) -> tuple[bytes, dict[str, Any]]:
        allowed_url(url)
        attempts: list[dict[str, Any]] = []
        for attempt in range(3):
            target = url
            redirects: list[dict[str, Any]] = []
            try:
                for _ in range(6):
                    allowed_url(target)
                    time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
                    self.client.cookies.clear()
                    self.last_request = time.monotonic()
                    with self.client.stream("GET", target, follow_redirects=False) as response:
                        status = response.status_code
                        if status in (301, 302, 303, 307, 308):
                            location = allowed_url(urljoin(target, response.headers["location"]))
                            redirects.append(
                                {"url": target, "status": status, "location": location}
                            )
                            target = location
                            self.last_request = time.monotonic()
                            continue
                        if status == 429 or 500 <= status < 600:
                            attempts.append({"attempt": attempt + 1, "status": status})
                            break
                        if status != 200:
                            raise ValueError(f"Non-retryable HTTP status {status}")
                        limit = MAX_PDF if pdf else 8 * 1024 * 1024
                        length = response.headers.get("content-length")
                        if length is not None and int(length) > limit:
                            raise ValueError("Response exceeds byte limit")
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > limit:
                                raise ValueError("Response exceeds byte limit")
                        metadata = {
                            "url": url,
                            "final_url": target,
                            "status": status,
                            "redirect_chain": redirects,
                            "retrieved_at": datetime.now(UTC).isoformat(),
                            "headers": {
                                key: response.headers[key]
                                for key in (
                                    "content-type",
                                    "content-length",
                                    "etag",
                                    "last-modified",
                                    "date",
                                )
                                if key in response.headers
                            },
                            "actual_bytes": len(body),
                            "attempts": attempts + [{"attempt": attempt + 1, "status": status}],
                        }
                        self.last_request = time.monotonic()
                        return bytes(body), metadata
                else:
                    raise ValueError("Redirect limit exceeded")
            except httpx.TransportError:
                attempts.append({"attempt": attempt + 1, "error": "network_error"})
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        raise ValueError(f"GET exhausted three transient attempts: {attempts}")


def discover(html: bytes, dimension: str, category: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")
    title = soup.find("h1") or soup.title
    title_text = title.get_text() if title else ""
    canonical = soup.find("link", rel="canonical")
    documents = []
    ignored = []
    for anchor in soup.select("a[href]"):
        href = str(anchor["href"])
        target = urljoin(url, href)
        if not urlsplit(target).path.casefold().endswith(".pdf"):
            if "/uploads/document/" in href:
                ignored.append({"href": href, "link_text": anchor.get_text(), "reason": "non_pdf"})
            continue
        allowed_url(target)
        link_text = anchor.get_text()
        container = anchor.find_parent(class_="doc-item")
        heading = container.select_one(".doc-item__title") if container else None
        topic_text = heading.get_text() if heading else link_text
        # The observed site separates the title anchor from its Download anchor.
        topic = re.match(r"(\d+(?:\.\d+)+)\.?\s*(\D.+)", topic_text.strip(), re.S)
        if not topic and heading is None:
            ignored.append({"href": href, "link_text": link_text, "reason": "no_explicit_topic"})
            continue
        documents.append(
            {
                "membership_id": sha256(f"{url}\n{target}\n{link_text}".encode())[:24],
                "dimension": dimension,
                "category": category,
                "landing_page_url": url,
                "landing_page_title": title_text,
                "link_text": link_text,
                "topic_label": topic_text,
                "topic_code": topic.group(1) if topic else None,
                "topic_title": topic.group(2) if topic else topic_text,
                "pdf_url": target,
                "filename": unquote(urlsplit(target).path.rsplit("/", 1)[-1]),
                "storage_path_date": (re.findall(r"/\d{4}-\d{2}-\d{2}/", target) or [None])[0],
            }
        )
    notices = []
    for node in soup.find_all(string=re.compile(r"Creative Commons|лицензии|лицензией", re.I)):
        parent = node.parent.parent if node.parent else None
        if parent:
            notices.append({"text": parent.get_text(), "source_url": url})
    unique: dict[str, dict[str, Any]] = {}
    for document in documents:
        key = document["membership_id"]
        if key not in unique:
            unique[key] = {**document, "link_occurrences": 0}
        unique[key]["link_occurrences"] += 1
    return {
        "dimension": dimension,
        "category": category,
        "url": url,
        "title": title_text,
        "canonical_url": str(canonical.get("href")) if canonical else None,
        "licensing_notices": notices,
        "documents": list(unique.values()),
        "ignored_links": ignored,
    }


def pdf_metadata(body: bytes, content_type: str) -> dict[str, Any]:
    import pymupdf

    if content_type.split(";")[0].strip().lower() not in {"application/pdf", "application/x-pdf"}:
        raise ValueError("Unexpected PDF Content-Type")
    if not body.startswith(b"%PDF-"):
        raise ValueError("Missing PDF signature")
    with pymupdf.open(stream=body, filetype="pdf") as pdf:  # type: ignore[no-untyped-call]
        if pdf.is_encrypted or pdf.page_count <= 0:
            raise ValueError("Encrypted or empty PDF")
        first = str(pdf[0].get_text())
        before_table = re.split(r"\n(?:No|№)\s*\n", first, maxsplit=1)[0].strip()
        title_lines = before_table.splitlines()
        title_start = next((i for i, s in enumerate(title_lines) if "Перечень вопросов" in s), None)
        title = None
        if title_start is not None:
            title = "\n".join(
                s
                for s in title_lines[title_start:]
                if s.strip() and not re.match(r"\d+\.", s.strip())
            )
        approval = []
        for im in pdf[0].get_image_info(xrefs=True):
            if im["xref"] and sha256(pdf.extract_image(im["xref"])["image"]) == APPROVAL_IMAGE:
                approval.append(
                    {
                        "date": "2022-07-01",
                        "visible_date_text": "«01» июля 2022 г.",
                        "visible_approval_text": "УТВЕРЖДАЮ",
                        "method": "visual_transcription_of_identical_sha256_image",
                        "image_sha256": APPROVAL_IMAGE,
                        "page": 1,
                        "bbox": list(im["bbox"]),
                        "scope": "approval stamp on this document; not a revision date",
                    }
                )
        return {
            "sha256": sha256(body),
            "valid_pdf": True,
            "page_count": pdf.page_count,
            "first_page_text": first,
            "internal_heading": before_table,
            "visible_document_title": title,
            "visible_approval_date_evidence": approval,
            "visible_date_text_layer_evidence": [
                line
                for line in before_table.splitlines()
                if re.search(r"утвержд|\d{2}\.\d{2}\.\d{4}|\b20\d{2}\b", line, re.I)
            ],
            "revision_date": None,
            "pdf_metadata": pdf.metadata,
        }


def inventory(root: Path, *, refresh: bool = False) -> dict[str, Any]:
    from .mchs_reference_verify import production_guard, verify_guard

    output = root / "reference/mchs-categorized"
    safe_output(output, root)
    for directory in ("sources/landing-pages", "pdf/objects"):
        (output / directory).mkdir(parents=True, exist_ok=True)
    pages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    objects: dict[str, dict[str, Any]] = {}
    cached_pages = {}
    cached_objects = {}
    guard = production_guard(root)
    if not refresh and (output / "manifest.json").exists():
        old_manifest = read_json(output / "manifest.json")
        guard = old_manifest.get("production_guard", guard)
        verify_guard(root, guard)
        cached_pages = {p["url"]: p for p in old_manifest["sources"]}
        cached_objects = read_json(output / "pdf/index.json")["urls"]
    with httpx.Client(
        timeout=60, headers={"User-Agent": "gims-open-data categorized-reference/1"}
    ) as http:
        client = PublicClient(http)
        for dimension, category, url in SOURCES:
            LOG.info("Landing %s", category)
            try:
                if url in cached_pages:
                    metadata = cached_pages[url]
                    body = (output / metadata["html_path"]).read_bytes()
                    if sha256(body) != metadata["sha256"]:
                        raise ValueError("Cached HTML checksum mismatch")
                else:
                    body, metadata = client.get(url, pdf=False)
                if "text/html" not in metadata["headers"].get("content-type", ""):
                    raise ValueError("Unexpected HTML Content-Type")
                saved = mask_secrets(body, set(), is_html=True)
                name = f"sources/landing-pages/{category}-{sha256(saved)}.html"
                (output / name).write_bytes(saved)
                page = discover(saved, dimension, category, url)
                page.update({k: v for k, v in metadata.items() if k not in page})
                page.update(
                    {"html_path": name, "sha256": sha256(saved), "secrets_masked": saved != body}
                )
                for notice in page["licensing_notices"]:
                    notice["retrieved_at"] = metadata["retrieved_at"]
                documents.extend(page.pop("documents"))
                pages.append(page)
            except (ValueError, httpx.HTTPError) as exc:
                failures.append({"category": category, "stage": "landing", "error": str(exc)})
        counts = Counter(d["category"] for d in documents)
        coherent = (
            len(pages) == 7 and counts["motor"] == 14 and all(counts[c] for _, c, _ in SOURCES)
        )
        if coherent:
            for url in dict.fromkeys(d["pdf_url"] for d in documents):
                LOG.info("PDF %s/%s", len(objects) + 1, len({d["pdf_url"] for d in documents}))
                try:
                    if url in cached_objects:
                        metadata = cached_objects[url]
                        body = (output / metadata["path"]).read_bytes()
                        if sha256(body) != metadata["sha256"]:
                            raise ValueError("Cached PDF checksum mismatch")
                    else:
                        body, metadata = client.get(url, pdf=True)
                    info = pdf_metadata(body, metadata["headers"].get("content-type", ""))
                    info = {**metadata, **info}
                    name = f"pdf/objects/{info['sha256']}.pdf"
                    (output / name).write_bytes(body)
                    info["path"] = name
                    objects[url] = info
                except (ValueError, httpx.HTTPError, RuntimeError) as exc:
                    failures.append({"pdf_url": url, "stage": "pdf", "error": str(exc)})
        else:
            failures.append({"stage": "stop_check", "error": "Require 7 pages and 14 motor PDFs"})
    by_sha: dict[str, list[str]] = defaultdict(list)
    for url, obj in objects.items():
        by_sha[obj["sha256"]].append(url)
    for doc in documents:
        linked_object = objects.get(doc["pdf_url"])
        doc.update(
            {
                "document_id": linked_object["sha256"] if linked_object else None,
                "source_pdf_sha256": linked_object["sha256"] if linked_object else None,
            }
        )
    summary = {
        "schema_version": VERSION,
        "landing_pages": len(pages),
        "category_pdf_memberships": dict(counts),
        "motor_14_confirmed": counts["motor"] == 14,
        "pdf_memberships": len(documents),
        "unique_urls": len({d["pdf_url"] for d in documents}),
        "downloaded_urls": len(objects),
        "unique_pdf_sha256": len(by_sha),
        "duplicate_url_memberships": len(documents) - len({d["pdf_url"] for d in documents}),
        "duplicate_link_occurrences": sum(d["link_occurrences"] - 1 for d in documents),
        "duplicate_sha_urls": {s: u for s, u in by_sha.items() if len(u) > 1},
        "total_downloaded_bytes": sum(o["actual_bytes"] for o in objects.values()),
        "unique_object_bytes": sum(objects[u[0]]["actual_bytes"] for u in by_sha.values()),
        "total_physical_pages": sum(objects[u[0]]["page_count"] for u in by_sha.values()),
        "redirects": sum(len(p["redirect_chain"]) for p in [*pages, *objects.values()]),
        "visible_approval_date_documents": sum(
            bool(o["visible_approval_date_evidence"]) for o in objects.values()
        ),
        "failures": failures,
        "coherent": coherent and not failures,
    }
    write_jsonl(output / "documents.jsonl", documents)
    atomic_json(output / "pdf/index.json", {"schema_version": VERSION, "urls": objects})
    atomic_json(output / "summary.json", summary)
    atomic_json(
        output / "manifest.json",
        {
            "schema_version": VERSION,
            "sources": pages,
            "production_guard": guard,
            "artifacts": {
                name: file_hash(output / name)
                for name in ("documents.jsonl", "pdf/index.json", "summary.json")
            },
            "authority": (
                "live current content > categorized published reference > legacy reference"
            ),
        },
    )
    if not summary["coherent"]:
        raise ValueError("Inventory incomplete; see ignored summary.json failures; parser stopped")
    return summary
