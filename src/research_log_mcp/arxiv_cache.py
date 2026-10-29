"""Persistent arXiv PDF cache.

* First request for a paper fetches metadata from the arXiv Atom API and the PDF
  from arxiv.org, stores both under the cache directory and indexes them in a
  small SQLite database (with FTS5 over title/abstract/full text).
* Every later request for the same paper is served from disk, never touching
  arXiv again (unless ``refresh=True``).
* PDFs are fetched from the public Google Cloud Storage mirror of arXiv
  (``gs://arxiv-dataset``, maintained for the Kaggle arXiv dataset) when it has
  the requested version, and from arxiv.org otherwise. Metadata always comes
  from the arXiv API. Requests to arxiv.org are throttled to >= 3 s apart, per
  arXiv's API terms; the mirror is not rate limited.

Layout under the cache dir (default ``~/.research-log/arxiv``)::

    arxiv.sqlite3            index + full-text search
    pdf/<id>v<n>.pdf         the PDF exactly as served by arXiv
    text/<id>v<n>.txt        extracted text, pages separated by form-feed (\\f)
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import __version__

DEFAULT_CACHE_DIR = Path(os.environ.get("RESEARCH_LOG_ARXIV_DIR", "~/.research-log/arxiv")).expanduser()
API_URL = "https://export.arxiv.org/api/query"
PDF_URL = "https://arxiv.org/pdf/{id}"
# Public, anonymously readable mirror of all arXiv PDFs (backs the Kaggle "arXiv Dataset").
GCS_MIRROR = "https://storage.googleapis.com/arxiv-dataset/{path}"
USER_AGENT = f"research-log-mcp/{__version__} (+https://github.com/JayRothenberger/research-tools)"
MIN_REQUEST_INTERVAL = 3.0  # seconds, per https://info.arxiv.org/help/api/tou.html
PAGE_SEP = "\f"

# pypdf logs a warning per embedded font when fontTools is absent; the text comes out fine without it.
logging.getLogger("pypdf").setLevel(logging.ERROR)

_NEW_ID = r"\d{4}\.\d{4,5}"
_OLD_ID = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}"
_ID_RE = re.compile(rf"(?P<id>{_NEW_ID}|{_OLD_ID})(?:v(?P<v>\d+))?", re.I)
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class ArxivError(Exception):
    pass


def parse_arxiv_id(value: str) -> tuple[str, int | None]:
    """Extract ``(base_id, version)`` from an id, ``arXiv:`` prefix, or abs/pdf URL."""
    s = value.strip()
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = re.sub(r"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I).split("?")[0].split("#")[0].strip("/")
    m = _ID_RE.fullmatch(s)
    if not m:
        raise ArxivError(f"Could not parse an arXiv identifier from {value!r}")
    base = m.group("id")
    if "/" not in base:
        base = base  # new-style ids are case-insensitive digits only
    return base, (int(m.group("v")) if m.group("v") else None)


def _safe_name(versioned_id: str) -> str:
    return versioned_id.replace("/", "_")


def gcs_mirror_path(base_id: str, version: int) -> str:
    """Object path of a PDF in the gs://arxiv-dataset mirror.

    New-style ids live under ``arxiv/arxiv/pdf/YYMM/<id>v<n>.pdf``; old-style ids
    under ``arxiv/<archive>/pdf/YYMM/<number>v<n>.pdf`` (e.g. ``arxiv/hep-th/pdf/9901/9901001v3.pdf``).
    """
    if "/" in base_id:
        archive, number = base_id.split("/", 1)
        archive = archive.split(".")[0]  # math.GT/0309136 -> math
        return f"arxiv/{archive}/pdf/{number[:4]}/{number}v{version}.pdf"
    return f"arxiv/arxiv/pdf/{base_id[:4]}/{base_id}v{version}.pdf"


@dataclass
class Paper:
    arxiv_id: str  # base id, e.g. 1706.03762 or hep-th/9901001
    version: int
    title: str
    authors: list[str]
    abstract: str
    published: str | None
    updated: str | None
    categories: list[str]
    doi: str | None
    pdf_path: str
    text_path: str | None
    pages: int | None
    bytes: int
    sha256: str
    fetched_at: str
    last_access: str
    hits: int
    origin: str | None = None

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "version": self.version,
            "versioned_id": self.versioned_id,
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract,
            "published": self.published,
            "updated": self.updated,
            "categories": self.categories,
            "doi": self.doi,
            "abs_url": f"https://arxiv.org/abs/{self.versioned_id}",
            "pdf_url": f"https://arxiv.org/pdf/{self.versioned_id}",
            "pdf_path": self.pdf_path,
            "pages": self.pages,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "fetched_at": self.fetched_at,
            "last_access": self.last_access,
            "hits": self.hits,
            "origin": self.origin,
        }


_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    authors     TEXT NOT NULL,     -- JSON list
    abstract    TEXT NOT NULL,
    published   TEXT,
    updated     TEXT,
    categories  TEXT NOT NULL,     -- JSON list
    doi         TEXT,
    pdf_path    TEXT NOT NULL,
    text_path   TEXT,
    pages       INTEGER,
    bytes       INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    last_access TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    origin      TEXT,                -- 'gcs-mirror' or 'arxiv.org'
    PRIMARY KEY (arxiv_id, version)
);
CREATE TABLE IF NOT EXISTS latest (
    arxiv_id   TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    versioned_id UNINDEXED, title, abstract, fulltext,
    tokenize='porter unicode61'
);
"""


class _Throttle:
    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class ArxivCache:
    def __init__(
        self,
        cache_dir: str | os.PathLike[str] | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = MIN_REQUEST_INTERVAL,
    ):
        self.dir = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
        (self.dir / "pdf").mkdir(parents=True, exist_ok=True)
        (self.dir / "text").mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.dir / "arxiv.sqlite3"), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._http = httpx.Client(
            transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=httpx.Timeout(60.0)
        )
        self._throttle = _Throttle(min_interval)
        self.use_mirror = os.environ.get("RESEARCH_LOG_ARXIV_MIRROR", "1") not in ("0", "false", "no")
        self.last_pdf_origin: str | None = None

    def close(self) -> None:
        self._http.close()
        self._conn.close()

    # ------------------------------------------------------------ network
    def _get(self, url: str, **kw: Any) -> httpx.Response:
        self._throttle.wait()
        try:
            r = self._http.get(url, **kw)
        except httpx.HTTPError as e:
            raise ArxivError(f"Network error fetching {url}: {e}") from e
        if r.status_code != 200:
            raise ArxivError(f"arXiv returned HTTP {r.status_code} for {url}")
        return r

    def _fetch_metadata(self, base_id: str, version: int | None) -> dict[str, Any]:
        ident = f"{base_id}v{version}" if version else base_id
        r = self._get(API_URL, params={"id_list": ident, "max_results": 1})
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            raise ArxivError(f"Could not parse arXiv API response for {ident}") from e
        entry = root.find("a:entry", _ATOM)
        if entry is None:
            raise ArxivError(f"arXiv API returned no entry for {ident}")
        entry_id = (entry.findtext("a:id", default="", namespaces=_ATOM) or "").strip()
        title = (entry.findtext("a:title", default="", namespaces=_ATOM) or "").strip()
        if not entry_id or "/abs/" not in entry_id or title.lower() == "error":
            summary = (entry.findtext("a:summary", default="", namespaces=_ATOM) or "").strip()
            raise ArxivError(f"arXiv does not know {ident}: {summary or 'no such paper'}")
        resolved_base, resolved_v = parse_arxiv_id(entry_id)
        if resolved_v is None:
            raise ArxivError(f"arXiv API returned an unversioned id for {ident}: {entry_id}")
        return {
            "arxiv_id": resolved_base,
            "version": resolved_v,
            "title": re.sub(r"\s+", " ", title),
            "authors": [
                (a.findtext("a:name", default="", namespaces=_ATOM) or "").strip()
                for a in entry.findall("a:author", _ATOM)
            ],
            "abstract": re.sub(r"\s+", " ", (entry.findtext("a:summary", default="", namespaces=_ATOM) or "").strip()),
            "published": (entry.findtext("a:published", default=None, namespaces=_ATOM) or None),
            "updated": (entry.findtext("a:updated", default=None, namespaces=_ATOM) or None),
            "categories": [c.get("term", "") for c in entry.findall("a:category", _ATOM) if c.get("term")],
            "doi": (entry.findtext("arxiv:doi", default=None, namespaces=_ATOM) or None),
        }

    def _fetch_pdf(self, base_id: str, version: int) -> bytes:
        versioned_id = f"{base_id}v{version}"
        if self.use_mirror:
            try:
                r = self._http.get(GCS_MIRROR.format(path=gcs_mirror_path(base_id, version)))
                if r.status_code == 200 and r.content.startswith(b"%PDF"):
                    self.last_pdf_origin = "gcs-mirror"
                    return r.content
            except httpx.HTTPError:
                pass  # fall through to arxiv.org
        r = self._get(PDF_URL.format(id=versioned_id))
        ctype = r.headers.get("content-type", "")
        if "pdf" not in ctype and not r.content.startswith(b"%PDF"):
            raise ArxivError(f"arXiv did not return a PDF for {versioned_id} (content-type {ctype!r})")
        self.last_pdf_origin = "arxiv.org"
        return r.content

    # ----------------------------------------------------------- extraction
    @staticmethod
    def extract_text(pdf_bytes: bytes) -> tuple[str, int]:
        """Return (text with pages separated by form-feed, page count)."""
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages = []
            for p in reader.pages:
                try:
                    pages.append(p.extract_text() or "")
                except Exception:  # noqa: BLE001 - keep going on a single bad page
                    pages.append("")
            return PAGE_SEP.join(pages), len(pages)
        except Exception as e:  # noqa: BLE001
            return f"[text extraction failed: {e}]", 0

    # -------------------------------------------------------------- lookup
    def _row(self, base_id: str, version: int) -> Paper | None:
        r = self._conn.execute("SELECT * FROM papers WHERE arxiv_id = ? AND version = ?", (base_id, version)).fetchone()
        return self._to_paper(r) if r else None

    @staticmethod
    def _to_paper(r: sqlite3.Row) -> Paper:
        return Paper(
            arxiv_id=r["arxiv_id"], version=int(r["version"]), title=r["title"], authors=json.loads(r["authors"]),
            abstract=r["abstract"], published=r["published"], updated=r["updated"], categories=json.loads(r["categories"]),
            doi=r["doi"], pdf_path=r["pdf_path"], text_path=r["text_path"], pages=r["pages"], bytes=int(r["bytes"]),
            sha256=r["sha256"], fetched_at=r["fetched_at"], last_access=r["last_access"], hits=int(r["hits"]),
            origin=r["origin"],
        )

    def _cached_version_for(self, base_id: str, version: int | None) -> Paper | None:
        if version is not None:
            return self._row(base_id, version)
        r = self._conn.execute("SELECT version FROM latest WHERE arxiv_id = ?", (base_id,)).fetchone()
        if r is not None:
            p = self._row(base_id, int(r["version"]))
            if p is not None and Path(p.pdf_path).exists():
                return p
        r = self._conn.execute(
            "SELECT version FROM papers WHERE arxiv_id = ? ORDER BY version DESC LIMIT 1", (base_id,)
        ).fetchone()
        return self._row(base_id, int(r["version"])) if r else None

    def _touch(self, p: Paper) -> Paper:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE papers SET hits = hits + 1, last_access = ? WHERE arxiv_id = ? AND version = ?",
            (now, p.arxiv_id, p.version),
        )
        p.hits += 1
        p.last_access = now
        return p

    # ----------------------------------------------------------------- API
    def get(self, id_or_url: str, *, refresh: bool = False) -> tuple[Paper, bool]:
        """Return ``(paper, from_cache)``. Fetches from arXiv only on a miss or when ``refresh``."""
        base_id, version = parse_arxiv_id(id_or_url)
        if not refresh:
            cached = self._cached_version_for(base_id, version)
            if cached is not None and Path(cached.pdf_path).exists():
                return self._touch(cached), True

        meta = self._fetch_metadata(base_id, version)
        base_id, resolved_v = meta["arxiv_id"], meta["version"]
        versioned = f"{base_id}v{resolved_v}"
        existing = self._row(base_id, resolved_v)
        if existing is not None and Path(existing.pdf_path).exists() and not refresh:
            self._remember_latest(base_id, resolved_v, version is None)
            return self._touch(existing), True

        pdf = self._fetch_pdf(base_id, resolved_v)
        import hashlib

        sha = hashlib.sha256(pdf).hexdigest()
        pdf_path = self.dir / "pdf" / f"{_safe_name(versioned)}.pdf"
        pdf_path.write_bytes(pdf)
        text, pages = self.extract_text(pdf)
        text_path = self.dir / "text" / f"{_safe_name(versioned)}.txt"
        text_path.write_text(text, encoding="utf-8")
        now = datetime.now(UTC).isoformat(timespec="seconds")

        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM papers WHERE arxiv_id = ? AND version = ?", (base_id, resolved_v))
            cur.execute("DELETE FROM papers_fts WHERE versioned_id = ?", (versioned,))
            cur.execute(
                """INSERT INTO papers (arxiv_id, version, title, authors, abstract, published, updated, categories, doi,
                                       pdf_path, text_path, pages, bytes, sha256, fetched_at, last_access, hits, origin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    base_id, resolved_v, meta["title"], json.dumps(meta["authors"]), meta["abstract"], meta["published"],
                    meta["updated"], json.dumps(meta["categories"]), meta["doi"], str(pdf_path), str(text_path),
                    pages or None, len(pdf), sha, now, now, self.last_pdf_origin,
                ),
            )
            cur.execute(
                "INSERT INTO papers_fts (versioned_id, title, abstract, fulltext) VALUES (?, ?, ?, ?)",
                (versioned, meta["title"], meta["abstract"], text.replace(PAGE_SEP, "\n")),
            )
            if version is None:
                cur.execute(
                    "INSERT INTO latest (arxiv_id, version, checked_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(arxiv_id) DO UPDATE SET version = excluded.version, checked_at = excluded.checked_at",
                    (base_id, resolved_v, now),
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        paper = self._row(base_id, resolved_v)
        assert paper is not None
        return paper, False

    def _remember_latest(self, base_id: str, version: int, asked_unversioned: bool) -> None:
        if asked_unversioned:
            now = datetime.now(UTC).isoformat(timespec="seconds")
            self._conn.execute(
                "INSERT INTO latest (arxiv_id, version, checked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(arxiv_id) DO UPDATE SET version = excluded.version, checked_at = excluded.checked_at",
                (base_id, version, now),
            )

    def lookup(self, id_or_url: str) -> Paper | None:
        """Cache-only lookup; never touches the network."""
        base_id, version = parse_arxiv_id(id_or_url)
        p = self._cached_version_for(base_id, version)
        return p if p is not None and Path(p.pdf_path).exists() else None

    def read_pdf(self, paper: Paper) -> bytes:
        return Path(paper.pdf_path).read_bytes()

    def read_text(self, paper: Paper, *, first_page: int = 1, last_page: int | None = None) -> tuple[str, int, int]:
        """Return (text, first_page, last_page) for a 1-based inclusive page range."""
        if not paper.text_path or not Path(paper.text_path).exists():
            text, _ = self.extract_text(self.read_pdf(paper))
        else:
            text = Path(paper.text_path).read_text(encoding="utf-8")
        pages = text.split(PAGE_SEP)
        n = len(pages)
        first = max(1, min(first_page, n))
        last = n if last_page is None else max(first, min(last_page, n))
        chunk = "\n\n".join(f"--- page {i} ---\n{pages[i - 1]}" for i in range(first, last + 1))
        return chunk, first, last

    def list(self, *, limit: int = 200) -> list[Paper]:
        rows = self._conn.execute("SELECT * FROM papers ORDER BY last_access DESC LIMIT ?", (limit,)).fetchall()
        return [self._to_paper(r) for r in rows]

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over cached papers (title, abstract, full text)."""
        tokens = [t for t in re.split(r"\s+", query.strip()) if t]
        safe = " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens) or '""'
        sql = (
            "SELECT versioned_id, rank, snippet(papers_fts, 3, '[', ']', ' … ', 24) AS snippet, "
            "snippet(papers_fts, 2, '[', ']', ' … ', 24) AS abs_snippet "
            "FROM papers_fts WHERE papers_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        rows: list[sqlite3.Row] = []
        for q in ([query.strip(), safe] if query.strip() != safe else [safe]):
            try:
                rows = self._conn.execute(sql, (q, limit)).fetchall()
                break
            except sqlite3.OperationalError:
                continue
        out = []
        for r in rows:
            base, v = parse_arxiv_id(r["versioned_id"])
            p = self._row(base, v or 0)
            if p is None:
                continue
            d = p.to_dict()
            d.pop("abstract", None)
            d["snippet"] = r["snippet"] if "[" in (r["snippet"] or "") else r["abs_snippet"]
            out.append(d)
        return out

    def stats(self) -> dict[str, Any]:
        r = self._conn.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b, SUM(hits) h FROM papers").fetchone()
        return {"cache_dir": str(self.dir), "papers": int(r["n"]), "bytes": int(r["b"]), "total_hits": int(r["h"] or 0)}
