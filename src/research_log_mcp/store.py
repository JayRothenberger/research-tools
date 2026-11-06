"""SQLite-backed append-only event store with full-text search and tag filtering.

Design notes
------------
* Every row in ``events`` is immutable. ``BEFORE UPDATE`` / ``BEFORE DELETE``
  triggers raise, so even direct SQL access cannot rewrite history. Corrections
  are expressed as *new* events that reference earlier ones (see ``refs``).
* Full-text search uses an FTS5 table populated alongside each insert (it
  stores its own copy of the text so snippets/highlights work). Tags are indexed in a separate ``event_tags`` table for exact
  filtering and also folded into the FTS index so a free-text query can hit them.
* Timestamps are stored as ISO-8601 strings normalised to UTC with second
  precision, so lexicographic ordering == chronological ordering.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Literal

DEFAULT_DB_PATH = Path(os.environ.get("RESEARCH_LOG_DB", "~/.research-log/log.sqlite3")).expanduser()

# Seconds to wait on another process holding the write lock.
BUSY_TIMEOUT_S = float(os.environ.get("RESEARCH_LOG_BUSY_TIMEOUT", "60"))

Order = Literal["newest", "oldest", "relevance"]

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT    NOT NULL UNIQUE,
    ts          TEXT    NOT NULL,            -- when the thing happened (UTC ISO-8601)
    recorded_at TEXT    NOT NULL,            -- when it was appended to the log (UTC ISO-8601)
    kind        TEXT    NOT NULL DEFAULT 'note',
    title       TEXT,
    text        TEXT    NOT NULL,
    source      TEXT,                        -- e.g. original file an entry was ingested from
    source_ref  TEXT    UNIQUE,              -- stable id within the source, for idempotent ingest
    refs        TEXT    NOT NULL DEFAULT '[]',  -- JSON list of event ids this event refers to
    metadata    TEXT    NOT NULL DEFAULT '{}'   -- JSON object, free-form
);
CREATE INDEX IF NOT EXISTS events_ts_idx     ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_idx   ON events(kind);
CREATE INDEX IF NOT EXISTS events_source_idx ON events(source);

CREATE TABLE IF NOT EXISTS event_tags (
    event_id INTEGER NOT NULL REFERENCES events(id),
    tag      TEXT    NOT NULL,
    PRIMARY KEY (event_id, tag)
);
CREATE INDEX IF NOT EXISTS event_tags_tag_idx ON event_tags(tag, event_id);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    title, text, tags,
    tokenize='porter unicode61'
);

-- Append-only enforcement -------------------------------------------------
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'research log is append-only: events cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'research log is append-only: events cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS event_tags_no_update BEFORE UPDATE ON event_tags
BEGIN SELECT RAISE(ABORT, 'research log is append-only: tags cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS event_tags_no_delete BEFORE DELETE ON event_tags
BEGIN SELECT RAISE(ABORT, 'research log is append-only: tags cannot be removed'); END;
"""

_TAG_SEP_RE = re.compile(r"\s+")


def normalize_tag(tag: str) -> str:
    """Lower-case, trim, strip a leading '#', collapse internal whitespace to '-'."""
    t = tag.strip().lstrip("#").strip().lower()
    t = _TAG_SEP_RE.sub("-", t)
    return t


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for raw in tags or []:
        t = normalize_tag(raw)
        if t:
            seen.setdefault(t, None)
    return list(seen)


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_timestamp(value: str | datetime | None, *, default_to_now: bool = True) -> str | None:
    """Parse an ISO-8601 string (or date) into a normalised UTC ISO string.

    Naive datetimes are assumed to be UTC. A bare date (YYYY-MM-DD) becomes
    midnight UTC on that day.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return now_utc() if default_to_now else None
    if isinstance(value, datetime):
        dt = value
    else:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise ValueError(f"Could not parse timestamp {value!r}; use ISO-8601 like 2026-03-05T14:30:00Z") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class Event:
    id: int
    uid: str
    ts: str
    recorded_at: str
    kind: str
    title: str | None
    text: str
    tags: list[str]
    source: str | None
    source_ref: str | None
    refs: list[int]
    metadata: dict[str, Any]
    snippet: str | None = None
    rank: float | None = None

    def to_dict(self, *, include_snippet: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "uid": self.uid,
            "ts": self.ts,
            "recorded_at": self.recorded_at,
            "kind": self.kind,
            "title": self.title,
            "text": self.text,
            "tags": self.tags,
            "source": self.source,
            "source_ref": self.source_ref,
            "refs": self.refs,
            "metadata": self.metadata,
        }
        if include_snippet:
            d["snippet"] = self.snippet
        return d


@dataclass
class NewEvent:
    text: str
    tags: list[str] = field(default_factory=list)
    kind: str = "note"
    title: str | None = None
    ts: str | None = None
    source: str | None = None
    source_ref: str | None = None
    refs: list[int] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    events: list[Event]
    total: int
    limit: int
    offset: int


class DuplicateSourceRef(Exception):
    """Raised when appending an event whose source_ref already exists."""

    def __init__(self, source_ref: str, existing_id: int):
        super().__init__(f"source_ref {source_ref!r} already recorded as event {existing_id}")
        self.source_ref = source_ref
        self.existing_id = existing_id


class ResearchLogStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path).expanduser() if path else DEFAULT_DB_PATH
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Several processes (one MCP server per Claude session) share this file.
        # Wait for locks instead of failing, and never hold the schema/WAL
        # setup against another writer: only run it when the DB is not yet set up.
        self._conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        self._init_schema()

    def _init_schema(self) -> None:
        for attempt in range(5):
            try:
                have = self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'events_fts'"
                ).fetchone()
                if have is None:
                    self._conn.executescript(_SCHEMA)  # idempotent (IF NOT EXISTS)
                else:
                    self._conn.execute("PRAGMA foreign_keys = ON")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                if attempt == 4:
                    raise
                time.sleep(0.5 * (attempt + 1))

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ResearchLogStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes (append only) ----------------------------------------------
    def append(self, ev: NewEvent) -> Event:
        return self.append_many([ev], skip_duplicates=False)[0][0]  # type: ignore[return-value]

    def append_many(
        self, items: Iterable[NewEvent], *, skip_duplicates: bool = True
    ) -> list[tuple[Event | None, int | None]]:
        """Append many events atomically.

        Returns one ``(event, duplicate_of)`` tuple per input. When an input's
        ``source_ref`` already exists and ``skip_duplicates`` is true, ``event``
        is None and ``duplicate_of`` is the existing event id. Otherwise a
        duplicate raises :class:`DuplicateSourceRef` and nothing is written.
        """
        items = list(items)
        out: list[tuple[Event | None, int | None]] = []
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")  # take the write lock up front; busy_timeout waits for it
        try:
            for ev in items:
                text = (ev.text or "").strip()
                if not text:
                    raise ValueError("event text must not be empty")
                kind = normalize_tag(ev.kind or "note") or "note"
                tags = normalize_tags(ev.tags)
                ts = parse_timestamp(ev.ts)
                refs = sorted({int(r) for r in (ev.refs or [])})
                for r in refs:
                    if cur.execute("SELECT 1 FROM events WHERE id = ?", (r,)).fetchone() is None:
                        raise ValueError(f"refs points at unknown event id {r}")
                source_ref = (ev.source_ref or "").strip() or None
                if source_ref is not None:
                    row = cur.execute("SELECT id FROM events WHERE source_ref = ?", (source_ref,)).fetchone()
                    if row is not None:
                        if skip_duplicates:
                            out.append((None, int(row["id"])))
                            continue
                        raise DuplicateSourceRef(source_ref, int(row["id"]))
                uid = uuid.uuid4().hex
                cur.execute(
                    """INSERT INTO events (uid, ts, recorded_at, kind, title, text, source, source_ref, refs, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uid,
                        ts,
                        now_utc(),
                        kind,
                        (ev.title or "").strip() or None,
                        text,
                        (ev.source or "").strip() or None,
                        source_ref,
                        json.dumps(refs),
                        json.dumps(ev.metadata or {}, sort_keys=True, default=str),
                    ),
                )
                event_id = int(cur.lastrowid)  # type: ignore[arg-type]
                cur.executemany("INSERT INTO event_tags (event_id, tag) VALUES (?, ?)", [(event_id, t) for t in tags])
                cur.execute(
                    "INSERT INTO events_fts (rowid, title, text, tags) VALUES (?, ?, ?, ?)",
                    (event_id, (ev.title or "").strip(), text, " ".join(tags)),
                )
                out.append((self._get(cur, event_id), None))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return out

    # -- reads --------------------------------------------------------------
    def _row_to_event(self, row: sqlite3.Row, tags: list[str], *, snippet: str | None = None, rank: float | None = None) -> Event:
        return Event(
            id=int(row["id"]),
            uid=row["uid"],
            ts=row["ts"],
            recorded_at=row["recorded_at"],
            kind=row["kind"],
            title=row["title"],
            text=row["text"],
            tags=tags,
            source=row["source"],
            source_ref=row["source_ref"],
            refs=json.loads(row["refs"] or "[]"),
            metadata=json.loads(row["metadata"] or "{}"),
            snippet=snippet,
            rank=rank,
        )

    def _tags_for(self, cur: sqlite3.Cursor, ids: list[int]) -> dict[int, list[str]]:
        if not ids:
            return {}
        out: dict[int, list[str]] = {i: [] for i in ids}
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start : chunk_start + 500]
            q = f"SELECT event_id, tag FROM event_tags WHERE event_id IN ({','.join('?' * len(chunk))}) ORDER BY tag"
            for r in cur.execute(q, chunk):
                out[int(r["event_id"])].append(r["tag"])
        return out

    def _get(self, cur: sqlite3.Cursor, event_id: int) -> Event | None:
        row = cur.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_event(row, self._tags_for(cur, [event_id])[event_id])

    def get(self, event_id: int) -> Event | None:
        return self._get(self._conn.cursor(), int(event_id))

    def get_many(self, ids: Iterable[int]) -> list[Event]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        cur = self._conn.cursor()
        rows = cur.execute(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        tags = self._tags_for(cur, [int(r["id"]) for r in rows])
        by_id = {int(r["id"]): self._row_to_event(r, tags[int(r["id"])]) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def referencing(self, event_id: int, *, limit: int = 100) -> list[Event]:
        """Events whose ``refs`` include ``event_id`` (annotations, follow-ups, corrections)."""
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT e.* FROM events e, json_each(e.refs) j WHERE j.value = ? ORDER BY e.id LIMIT ?",
            (int(event_id), limit),
        ).fetchall()
        tags = self._tags_for(cur, [int(r["id"]) for r in rows])
        return [self._row_to_event(r, tags[int(r["id"])]) for r in rows]

    @staticmethod
    def _fts_query(query: str) -> tuple[str, str]:
        """Return (raw, safe) FTS5 query strings.

        ``raw`` passes the user's text straight through so FTS5 operators
        (AND/OR/NOT, quotes, prefix*) work. ``safe`` quotes every token and ANDs
        them so arbitrary text never causes a syntax error.
        """
        tokens = [t for t in re.split(r"\s+", query.strip()) if t]
        safe = " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens) or '""'
        return query.strip(), safe

    def search(
        self,
        *,
        query: str | None = None,
        tags_all: Iterable[str] | None = None,
        tags_any: Iterable[str] | None = None,
        tags_none: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        source: str | None = None,
        order: Order = "newest",
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where: list[str] = []
        params: list[Any] = []

        tags_all_n = normalize_tags(tags_all)
        tags_any_n = normalize_tags(tags_any)
        tags_none_n = normalize_tags(tags_none)
        kinds_n = normalize_tags(kinds)

        for t in tags_all_n:
            where.append("EXISTS (SELECT 1 FROM event_tags et WHERE et.event_id = e.id AND et.tag = ?)")
            params.append(t)
        if tags_any_n:
            where.append(
                f"EXISTS (SELECT 1 FROM event_tags et WHERE et.event_id = e.id AND et.tag IN ({','.join('?' * len(tags_any_n))}))"
            )
            params.extend(tags_any_n)
        if tags_none_n:
            where.append(
                f"NOT EXISTS (SELECT 1 FROM event_tags et WHERE et.event_id = e.id AND et.tag IN ({','.join('?' * len(tags_none_n))}))"
            )
            params.extend(tags_none_n)
        if kinds_n:
            where.append(f"e.kind IN ({','.join('?' * len(kinds_n))})")
            params.extend(kinds_n)
        if since:
            where.append("e.ts >= ?")
            params.append(parse_timestamp(since))
        if until:
            where.append("e.ts <= ?")
            params.append(parse_timestamp(until))
        if source:
            where.append("e.source = ?")
            params.append(source.strip())

        has_query = bool(query and query.strip())
        order_sql = {"newest": "e.ts DESC, e.id DESC", "oldest": "e.ts ASC, e.id ASC"}.get(order, "e.ts DESC, e.id DESC")

        cur = self._conn.cursor()
        if has_query:
            raw, safe = self._fts_query(query or "")
            if order == "relevance":
                order_sql = "f.rank, e.ts DESC"
            base = (
                "FROM events_fts f JOIN events e ON e.id = f.rowid WHERE events_fts MATCH ? "
                + ("AND " + " AND ".join(where) if where else "")
            )
            select = (
                "SELECT e.*, f.rank AS rank, "
                "snippet(events_fts, 1, '[', ']', ' … ', 24) AS snippet "
            )
            attempts = [raw, safe] if raw != safe else [safe]
            last_err: Exception | None = None
            rows: list[sqlite3.Row] = []
            total = 0
            for q in attempts:
                try:
                    total = int(cur.execute(f"SELECT COUNT(*) {base}", [q, *params]).fetchone()[0])
                    rows = cur.execute(
                        f"{select} {base} ORDER BY {order_sql} LIMIT ? OFFSET ?", [q, *params, limit, offset]
                    ).fetchall()
                    last_err = None
                    break
                except sqlite3.OperationalError as e:  # FTS syntax error on the raw form
                    last_err = e
            if last_err is not None:
                raise ValueError(f"Invalid search query {query!r}: {last_err}") from last_err
        else:
            base = "FROM events e" + (" WHERE " + " AND ".join(where) if where else "")
            total = int(cur.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])
            rows = cur.execute(
                f"SELECT e.*, NULL AS rank, NULL AS snippet {base} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

        tags = self._tags_for(cur, [int(r["id"]) for r in rows])
        events = [
            self._row_to_event(r, tags[int(r["id"])], snippet=r["snippet"], rank=r["rank"]) for r in rows
        ]
        return SearchResult(events=events, total=total, limit=limit, offset=offset)

    def tags(self, *, prefix: str | None = None, limit: int = 500) -> list[tuple[str, int]]:
        cur = self._conn.cursor()
        if prefix:
            p = normalize_tag(prefix)
            rows = cur.execute(
                "SELECT tag, COUNT(*) n FROM event_tags WHERE tag >= ? AND tag < ? GROUP BY tag ORDER BY n DESC, tag LIMIT ?",
                (p, p + "￿", limit),
            )
        else:
            rows = cur.execute("SELECT tag, COUNT(*) n FROM event_tags GROUP BY tag ORDER BY n DESC, tag LIMIT ?", (limit,))
        return [(r["tag"], int(r["n"])) for r in rows]

    def sources(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts FROM events WHERE source IS NOT NULL "
            "GROUP BY source ORDER BY n DESC"
        )
        return [{"source": r["source"], "count": int(r["n"]), "first_ts": r["first_ts"], "last_ts": r["last_ts"]} for r in rows]

    def kinds(self) -> list[tuple[str, int]]:
        rows = self._conn.execute("SELECT kind, COUNT(*) n FROM events GROUP BY kind ORDER BY n DESC, kind")
        return [(r["kind"], int(r["n"])) for r in rows]

    def stats(self) -> dict[str, Any]:
        r = self._conn.execute("SELECT COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts, MAX(recorded_at) last_recorded FROM events").fetchone()
        ntags = self._conn.execute("SELECT COUNT(DISTINCT tag) FROM event_tags").fetchone()[0]
        return {
            "db_path": str(self.path),
            "events": int(r["n"]),
            "distinct_tags": int(ntags),
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
            "last_recorded_at": r["last_recorded"],
            "kinds": dict(self.kinds()),
        }

    def iter_all(self, *, batch: int = 1000) -> Iterable[Event]:
        """Iterate every event in append order (for exports / backups)."""
        last = 0
        cur = self._conn.cursor()
        while True:
            rows = cur.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last, batch)).fetchall()
            if not rows:
                return
            tags = self._tags_for(cur, [int(r["id"]) for r in rows])
            for r in rows:
                last = int(r["id"])
                yield self._row_to_event(r, tags[last])
