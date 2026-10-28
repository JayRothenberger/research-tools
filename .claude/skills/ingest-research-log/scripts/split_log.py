#!/usr/bin/env python3
"""Split an existing research log into candidate events for the research-log MCP.

Reads Markdown, plain text, or JSONL and writes a JSON array of objects shaped
for the `append_events` tool: {text, title, tags, kind, timestamp, source, source_ref}.

Nothing is sent anywhere; review the output, then pass batches to `append_events`.

Usage:
    split_log.py LOG.md [LOG2.md ...] [-o candidates.json]
                 [--granularity heading|date|paragraph]
                 [--source-prefix notes/] [--default-tag project-x]
                 [--no-inherit-dates] [--default-kind note]

Heuristics (Markdown / text):
  * A heading or line that contains a date starts a new dated section.
    Recognised: 2024-03-05, 2024/03/05, 03/05/2024, March 5, 2024, 5 March 2024,
    Mar 5 2024, optionally followed by a time (14:30, 2:30pm).
  * --granularity heading (default): every heading becomes an event; headings
    under a dated heading inherit its date. Text before the first heading of a
    section is its own event.
  * --granularity date: one event per dated section (headings concatenated).
  * --granularity paragraph: blank-line-separated paragraphs become events.
  * Inline `#tags` and `tags: a, b` lines are extracted into `tags`.
  * Leading markers set `kind`: TODO/Idea/Question/Hypothesis/Result/Decision/
    Reference/Meeting/Experiment (case-insensitive, e.g. "Result: ..." or "[idea]").
  * Undated entries inherit the most recent date seen (disable with --no-inherit-dates).
  * source_ref = "<source>#<date>/<sha1(text)[:10]>" so re-running is idempotent.

JSONL: each line is an object; accepted keys: text|content|body|note, title,
tags (list or comma string), timestamp|ts|date|created|time, kind|type, id|ref.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
TIME_RE = r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)?"
DATE_PATTERNS = [
    re.compile(rf"(?P<y>\d{{4}})[-/.](?P<m>\d{{1,2}})[-/.](?P<d>\d{{1,2}})(?:[T\s]+{TIME_RE})?"),
    re.compile(rf"\b(?P<m>\d{{1,2}})/(?P<d>\d{{1,2}})/(?P<y>\d{{4}})\b(?:\s+{TIME_RE})?"),
    re.compile(rf"\b(?P<mon>{MONTHS})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})(?:\s+{TIME_RE})?", re.I),
    re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{MONTHS})\.?,?\s+(?P<y>\d{{4}})(?:\s+{TIME_RE})?", re.I),
]
MONTH_INDEX = {m: i % 12 + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec january february march april may june july august september october november december".split()
)}
MONTH_INDEX["sept"] = 9
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HASHTAG_RE = re.compile(r"(?<![\w&/#])#([A-Za-z][\w\-/.]*)")
TAGLINE_RE = re.compile(r"^\s*(?:tags?|topics?|labels?)\s*[:=]\s*(.+?)\s*$", re.I)
KIND_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\[\s*)?(?P<kind>todo|idea|question|hypothesis|result|results|decision|reference|ref|meeting|experiment|note|bug|finding)(?:\s*\])?\s*[:\-–—]\s*",
    re.I,
)
KIND_ALIASES = {"results": "result", "ref": "reference", "finding": "result"}


def parse_date(s: str) -> tuple[str | None, re.Match | None]:
    """Return (ISO timestamp, match) for the first date found in s, else (None, None)."""
    for pat in DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        g = m.groupdict()
        try:
            y = int(g["y"])
            d = int(g["d"])
            mo = int(g["m"]) if g.get("m") else MONTH_INDEX[g["mon"].lower()]
            if not (1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
                continue
            hh = mm = ss = 0
            t = g.get("time")
            if t:
                tm = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?", t.strip(), re.I)
                if tm:
                    hh, mm = int(tm.group(1)), int(tm.group(2))
                    ss = int(tm.group(3) or 0)
                    ap = (tm.group(4) or "").lower()
                    if ap == "pm" and hh < 12:
                        hh += 12
                    if ap == "am" and hh == 12:
                        hh = 0
            dt = datetime(y, mo, d, hh, mm, ss)
            return dt.isoformat(timespec="seconds"), m
        except (ValueError, KeyError):
            continue
    return None, None


def extract_tags(text: str) -> tuple[list[str], str]:
    """Pull #hashtags and 'tags:' lines out of text. Returns (tags, text_without_taglines)."""
    tags: list[str] = []
    kept: list[str] = []
    for line in text.split("\n"):
        tl = TAGLINE_RE.match(line)
        if tl:
            tags += [t.strip().lstrip("#") for t in re.split(r"[,;]\s*|\s{2,}", tl.group(1)) if t.strip()]
            continue
        kept.append(line)
    tags += HASHTAG_RE.findall(text)
    norm: dict[str, None] = {}
    for t in tags:
        t = re.sub(r"\s+", "-", t.strip().lower()).rstrip(".,;:")
        if t and not t.isdigit():
            norm.setdefault(t, None)
    return list(norm), "\n".join(kept).strip()


def detect_kind(text: str, default: str) -> str:
    m = KIND_RE.match(text)
    if not m:
        return default
    k = m.group("kind").lower()
    return KIND_ALIASES.get(k, k)


@dataclass
class Candidate:
    text: str
    timestamp: str | None
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    kind: str = "note"
    source: str = ""
    source_ref: str = ""
    line: int = 0
    dated_explicitly: bool = False

    def finalize(self, default_tags: list[str]) -> dict:
        extra, body = extract_tags(self.text)
        tags = list(dict.fromkeys([*default_tags, *self.tags, *extra]))
        body = body or self.text.strip()
        h = hashlib.sha1(body.encode("utf-8")).hexdigest()[:10]
        day = (self.timestamp or "undated")[:10]
        out = {
            "text": body,
            "title": self.title or None,
            "tags": tags,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "source": self.source,
            "source_ref": f"{self.source}#{day}/{h}",
            "_line": self.line,
            "_dated_explicitly": self.dated_explicitly,
        }
        return out


def split_markdown(lines: list[str], source: str, granularity: str, default_kind: str, inherit: bool) -> list[Candidate]:
    cands: list[Candidate] = []
    current_date: str | None = None
    date_level: int | None = None  # heading level that carried the current date
    buf: list[str] = []
    buf_title: str | None = None
    section_title: str | None = None  # paragraph mode: last heading, inherited by following paragraphs
    buf_line = 0
    buf_dated = False
    in_fence = False

    def flush():
        nonlocal buf, buf_title, buf_dated
        text = "\n".join(buf).strip()
        if text:  # a bare heading with no body (e.g. the document title) is not an event
            kind = detect_kind(text, default_kind)
            if kind == default_kind and buf_title:
                kind = detect_kind(buf_title, default_kind)
            title = buf_title or (section_title if granularity == "paragraph" else None)
            cands.append(Candidate(
                text=text, title=title, timestamp=current_date, kind=kind,
                source=source, line=buf_line, dated_explicitly=buf_dated,
            ))
        buf, buf_title, buf_dated = [], None, False

    for i, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        if line.strip().startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if in_fence:
            buf.append(line)
            continue
        h = HEADING_RE.match(line)
        if h:
            level, title = len(h.group(1)), h.group(2).strip()
            ts, m = parse_date(title)
            if ts:
                flush()
                current_date, date_level = ts, level
                rest = (title[: m.start()] + title[m.end() :]).strip(" -–—:|,")
                buf_title = section_title = rest or None
                buf_line, buf_dated = i, True
                if granularity == "date":
                    buf_title = rest or title
                continue
            if granularity == "date":
                buf.append(f"**{title}**")
                continue
            flush()
            if date_level is not None and level <= date_level and not inherit:
                current_date = None
            buf_title = section_title = title
            buf_line = i
            continue
        # non-heading lines
        if not line.strip():
            if granularity == "paragraph":
                flush()
                buf_line = i + 1
            else:
                buf.append(line)
            continue
        ts, m = parse_date(line)
        # A short standalone line that is mostly a date acts like a dated heading.
        if ts and len(line.strip()) <= len(m.group(0)) + 24 and not line.lstrip().startswith(("-", "*", ">", "|")):
            flush()
            current_date = ts
            rest = (line[: m.start()] + line[m.end() :]).strip(" -–—:|,*_")
            buf_title = rest or None
            buf_line, buf_dated = i, True
            continue
        if not buf and not buf_title:
            buf_line = i
        buf.append(line)
    flush()
    if inherit:
        last = None
        for c in cands:
            if c.timestamp:
                last = c.timestamp
            elif last:
                c.timestamp = last
    return cands


def split_jsonl(lines: list[str], source: str, default_kind: str) -> list[Candidate]:
    cands = []
    for i, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            print(f"warning: {source}:{i} is not valid JSON, skipped", file=sys.stderr)
            continue
        text = next((obj[k] for k in ("text", "content", "body", "note", "message") if obj.get(k)), None)
        if not text:
            continue
        ts_raw = next((obj[k] for k in ("timestamp", "ts", "date", "created", "created_at", "time") if obj.get(k)), None)
        ts = None
        if ts_raw:
            ts, _ = parse_date(str(ts_raw))
            if ts is None:
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).isoformat(timespec="seconds")
                except ValueError:
                    pass
        tags = obj.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,;\s]+", tags) if t]
        c = Candidate(text=str(text), timestamp=ts, title=obj.get("title"), tags=[str(t) for t in tags],
                      kind=str(obj.get("kind") or obj.get("type") or detect_kind(str(text), default_kind)).lower(),
                      source=source, line=i, dated_explicitly=ts is not None)
        cands.append(c)
    return cands


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="Write JSON array here (default: stdout).")
    ap.add_argument("--granularity", choices=["heading", "date", "paragraph"], default="heading")
    ap.add_argument("--source-prefix", default="", help="Prefix for the `source` field (e.g. 'notebooks/').")
    ap.add_argument("--default-tag", action="append", default=[], help="Tag added to every event (repeatable).")
    ap.add_argument("--default-kind", default="note")
    ap.add_argument("--no-inherit-dates", action="store_true", help="Do not carry the last seen date onto undated entries.")
    ap.add_argument("--keep-undated", action="store_true", help="Keep entries with no timestamp (default: still kept but flagged).")
    args = ap.parse_args()

    all_cands: list[dict] = []
    for p in args.paths:
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        source = f"{args.source_prefix}{p.name}" if args.source_prefix else str(p)
        if p.suffix.lower() in {".jsonl", ".ndjson"} or (p.suffix.lower() == ".json" and text.lstrip().startswith("{")):
            cands = split_jsonl(lines, source, args.default_kind)
        else:
            cands = split_markdown(lines, source, args.granularity, args.default_kind, not args.no_inherit_dates)
        all_cands += [c.finalize(args.default_tag) for c in cands]

    # Stable chronological order; undated entries keep their file position relative to neighbours.
    all_cands.sort(key=lambda c: ((c["timestamp"] or "9999"), c["source"], c["_line"]))

    dated = [c for c in all_cands if c["timestamp"]]
    tags: dict[str, int] = {}
    for c in all_cands:
        for t in c["tags"]:
            tags[t] = tags.get(t, 0) + 1
    kinds: dict[str, int] = {}
    for c in all_cands:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"candidates: {len(all_cands)}  dated: {len(dated)}  undated: {len(all_cands) - len(dated)}", file=sys.stderr)
    if dated:
        print(f"date range: {min(c['timestamp'] for c in dated)} .. {max(c['timestamp'] for c in dated)}", file=sys.stderr)
    print(f"kinds: {dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}", file=sys.stderr)
    top = sorted(tags.items(), key=lambda kv: -kv[1])[:25]
    print(f"top tags: {top}", file=sys.stderr)
    lengths = sorted(len(c["text"]) for c in all_cands) or [0]
    print(f"text length: min {lengths[0]}  median {lengths[len(lengths)//2]}  max {lengths[-1]}", file=sys.stderr)

    out = json.dumps(all_cands, ensure_ascii=False, indent=1)
    if args.output:
        args.output.write_text(out, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
