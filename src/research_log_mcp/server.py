"""MCP server exposing the append-only research log.

Tools
-----
append_event      – record one event (text + tags + optional kind/title/timestamp/source/refs/metadata)
append_events     – record many events atomically; idempotent via ``source_ref`` (for ingestion)
annotate_event    – append a follow-up/correction that references an earlier event
search_events     – full-text query + tag filters (all/any/none) + kind/time/source filters
get_event         – fetch one event plus the events that reference it
get_events        – fetch several events by id
list_tags         – tag vocabulary with counts (optionally filtered by prefix)
list_sources      – ingestion sources with counts and date ranges
log_stats         – summary statistics
export_events     – dump matching events as JSONL or Markdown

Resources
---------
researchlog://events/{id}   – one event as JSON
researchlog://tags          – tag vocabulary as JSON
researchlog://stats         – summary statistics as JSON
"""

from __future__ import annotations

import functools
import json
import os
from typing import Annotated, Any, Callable, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from .store import DEFAULT_DB_PATH, DuplicateSourceRef, NewEvent, Order, ResearchLogStore

INSTRUCTIONS = """\
This is an append-only research log. Events are never edited or deleted; to correct or
extend an earlier entry, append a new event that references it (annotate_event, or
append_event with `refs`). Use short, consistent lower-case tags (e.g. project names,
topics, experiment ids). Prefer `kind` values from: note, idea, question, hypothesis,
experiment, result, decision, todo, reference, meeting, annotation. Full-text search
supports FTS5 syntax (AND / OR / NOT, "exact phrase", prefix*).
"""


class EventInput(BaseModel):
    """One event to append."""

    text: Annotated[str, Field(description="Body of the entry. Markdown is fine.", min_length=1)]
    tags: Annotated[list[str], Field(default_factory=list, description="Tags; normalised to lower-case, '#' stripped.")]
    kind: Annotated[str, Field(default="note", description="Event type, e.g. note, idea, result, decision, todo, reference.")]
    title: Annotated[str | None, Field(default=None, description="Optional short title.")]
    timestamp: Annotated[
        str | None,
        Field(default=None, description="ISO-8601 time the event happened (e.g. 2026-03-05 or 2026-03-05T14:30:00Z). Defaults to now."),
    ]
    source: Annotated[str | None, Field(default=None, description="Where this came from, e.g. 'notebooks/2024-log.md' for ingested entries.")]
    source_ref: Annotated[
        str | None,
        Field(default=None, description="Stable unique id within the source (e.g. 'notebooks/2024-log.md#2024-03-05/2'). Re-appending the same source_ref is skipped, making ingestion idempotent."),
    ]
    refs: Annotated[list[int], Field(default_factory=list, description="Ids of earlier events this one refers to.")]
    metadata: Annotated[dict[str, Any], Field(default_factory=dict, description="Free-form JSON metadata.")]

    def to_new_event(self) -> NewEvent:
        return NewEvent(
            text=self.text,
            tags=self.tags,
            kind=self.kind,
            title=self.title,
            ts=self.timestamp,
            source=self.source,
            source_ref=self.source_ref,
            refs=self.refs,
            metadata=self.metadata,
        )


_F = TypeVar("_F", bound=Callable[..., Any])


def _surface_errors(fn: _F) -> _F:
    """Turn validation/store errors into ToolError so the client sees the message."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, DuplicateSourceRef) as e:
            raise ToolError(str(e)) from e

    return wrapper  # type: ignore[return-value]


def _event_to_markdown(e: dict[str, Any]) -> str:
    head = f"### [{e['id']}] {e['ts']}"
    if e.get("title"):
        head += f" — {e['title']}"
    meta_bits = [f"kind: {e['kind']}"]
    if e.get("tags"):
        meta_bits.append("tags: " + ", ".join(e["tags"]))
    if e.get("refs"):
        meta_bits.append("refs: " + ", ".join(str(r) for r in e["refs"]))
    if e.get("source"):
        meta_bits.append(f"source: {e['source']}")
    return f"{head}\n_{' · '.join(meta_bits)}_\n\n{e['text']}\n"


def create_server(db_path: str | os.PathLike[str] | None = None) -> MCPServer:
    store = ResearchLogStore(db_path or os.environ.get("RESEARCH_LOG_DB") or DEFAULT_DB_PATH)
    mcp = MCPServer(
        name="research-log",
        title="Research Log",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )
    mcp.state = {"store": store}  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ writes
    @mcp.tool(description="Append one event to the research log. Events are immutable once written.")
    @_surface_errors
    def append_event(
        text: Annotated[str, Field(description="Body of the entry. Markdown is fine.", min_length=1)],
        tags: Annotated[list[str], Field(default_factory=list, description="Tags, e.g. ['project-x', 'optimizer', 'bug'].")],
        kind: Annotated[str, Field(default="note", description="note | idea | question | hypothesis | experiment | result | decision | todo | reference | meeting | annotation | ...")],
        title: Annotated[str | None, Field(description="Optional short title.")] = None,
        timestamp: Annotated[str | None, Field(description="ISO-8601 time the event happened. Defaults to now. Use for back-dated entries.")] = None,
        source: Annotated[str | None, Field(description="Origin, e.g. path of the file this was ingested from.")] = None,
        source_ref: Annotated[str | None, Field(description="Stable unique id within the source; duplicates are rejected.")] = None,
        refs: Annotated[list[int], Field(description="Ids of earlier events this entry refers to.")] = [],
        metadata: Annotated[dict[str, Any], Field(description="Free-form JSON metadata.")] = {},
    ) -> dict[str, Any]:
        ev = EventInput(
            text=text, tags=tags, kind=kind, title=title, timestamp=timestamp,
            source=source, source_ref=source_ref, refs=refs, metadata=metadata,
        )
        return store.append(ev.to_new_event()).to_dict()

    @mcp.tool(
        description=(
            "Append many events in one atomic batch (use for ingesting an existing log). "
            "Events whose source_ref already exists are skipped and reported, so re-running is safe."
        )
    )
    @_surface_errors
    def append_events(
        events: Annotated[list[EventInput], Field(description="Events to append, in chronological order.", min_length=1)],
        skip_duplicates: Annotated[bool, Field(description="Skip events whose source_ref is already recorded instead of failing the batch.")] = True,
    ) -> dict[str, Any]:
        results = store.append_many([e.to_new_event() for e in events], skip_duplicates=skip_duplicates)
        appended = [ev.id for ev, _dup in results if ev is not None]
        skipped = [
            {"index": i, "source_ref": events[i].source_ref, "existing_id": dup}
            for i, (ev, dup) in enumerate(results)
            if ev is None
        ]
        return {"appended": len(appended), "skipped": len(skipped), "ids": appended, "skipped_details": skipped}

    @mcp.tool(
        description=(
            "Append a follow-up, correction, or outcome that references an earlier event. "
            "This is how you 'edit' in an append-only log: the original stays, the annotation links to it."
        )
    )
    @_surface_errors
    def annotate_event(
        event_id: Annotated[int, Field(description="Id of the event being annotated.")],
        text: Annotated[str, Field(min_length=1, description="The annotation / correction / outcome.")],
        tags: Annotated[list[str], Field(description="Extra tags. The original's tags are copied unless inherit_tags=false.")] = [],
        kind: Annotated[str, Field(description="Usually 'annotation', 'correction', or 'result'.")] = "annotation",
        inherit_tags: Annotated[bool, Field()] = True,
        timestamp: Annotated[str | None, Field(description="ISO-8601; defaults to now.")] = None,
    ) -> dict[str, Any]:
        original = store.get(event_id)
        if original is None:
            raise ValueError(f"No event with id {event_id}")
        all_tags = list(original.tags) + list(tags) if inherit_tags else list(tags)
        ev = NewEvent(text=text, tags=all_tags, kind=kind, ts=timestamp, refs=[event_id],
                      metadata={"annotates": event_id})
        return store.append(ev).to_dict()

    # ------------------------------------------------------------------- reads
    @mcp.tool(
        description=(
            "Search the log. Combine a full-text query (FTS5 syntax: AND/OR/NOT, \"phrases\", prefix*) with tag filters: "
            "tags_all (must have every tag), tags_any (at least one), tags_none (exclude). "
            "Also filter by kind, time range, and source. Omit query to list/filter chronologically."
        )
    )
    @_surface_errors
    def search_events(
        query: Annotated[str | None, Field(description="Full-text query over title, text and tags.")] = None,
        tags_all: Annotated[list[str], Field(description="Event must carry ALL of these tags.")] = [],
        tags_any: Annotated[list[str], Field(description="Event must carry AT LEAST ONE of these tags.")] = [],
        tags_none: Annotated[list[str], Field(description="Event must carry NONE of these tags.")] = [],
        kinds: Annotated[list[str], Field(description="Restrict to these kinds, e.g. ['result','decision'].")] = [],
        since: Annotated[str | None, Field(description="ISO-8601 lower bound on event time (inclusive).")] = None,
        until: Annotated[str | None, Field(description="ISO-8601 upper bound on event time (inclusive).")] = None,
        source: Annotated[str | None, Field(description="Exact source match.")] = None,
        order: Annotated[Order, Field(description="newest | oldest | relevance (relevance requires a query).")] = "newest",
        limit: Annotated[int, Field(ge=1, le=500)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        full_text: Annotated[bool, Field(description="Include full event text. Set false for compact results (snippet/title only).")] = True,
    ) -> dict[str, Any]:
        res = store.search(
            query=query, tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, kinds=kinds,
            since=since, until=until, source=source, order=order, limit=limit, offset=offset,
        )
        items = []
        for e in res.events:
            d = e.to_dict(include_snippet=bool(query))
            if not full_text:
                d["text"] = (e.text[:200] + "…") if len(e.text) > 200 else e.text
            items.append(d)
        return {"total": res.total, "limit": res.limit, "offset": res.offset, "events": items}

    @mcp.tool(description="Fetch one event by id, together with any later events that reference it (annotations, corrections, outcomes).")
    @_surface_errors
    def get_event(event_id: Annotated[int, Field(description="Event id.")]) -> dict[str, Any]:
        e = store.get(event_id)
        if e is None:
            raise ValueError(f"No event with id {event_id}")
        return {"event": e.to_dict(), "referenced_by": [r.to_dict() for r in store.referencing(event_id)]}

    @mcp.tool(description="Fetch several events by id.")
    @_surface_errors
    def get_events(event_ids: Annotated[list[int], Field(min_length=1, max_length=500)]) -> dict[str, Any]:
        found = store.get_many(event_ids)
        missing = sorted(set(event_ids) - {e.id for e in found})
        return {"events": [e.to_dict() for e in found], "missing": missing}

    @mcp.tool(description="List tags in use with their counts, most-used first. Optionally restrict to a prefix.")
    @_surface_errors
    def list_tags(
        prefix: Annotated[str | None, Field(description="Only tags starting with this.")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 200,
    ) -> dict[str, Any]:
        return {"tags": [{"tag": t, "count": n} for t, n in store.tags(prefix=prefix, limit=limit)]}

    @mcp.tool(description="List ingestion sources with event counts and date ranges.")
    @_surface_errors
    def list_sources() -> dict[str, Any]:
        return {"sources": store.sources()}

    @mcp.tool(description="Summary statistics for the log: event count, tag count, date range, kinds, database path.")
    @_surface_errors
    def log_stats() -> dict[str, Any]:
        return store.stats()

    @mcp.tool(description="Export matching events (same filters as search_events, no limit) as JSONL or Markdown text.")
    @_surface_errors
    def export_events(
        format: Annotated[Literal["jsonl", "markdown"], Field()] = "jsonl",
        query: Annotated[str | None, Field()] = None,
        tags_all: Annotated[list[str], Field()] = [],
        tags_any: Annotated[list[str], Field()] = [],
        tags_none: Annotated[list[str], Field()] = [],
        kinds: Annotated[list[str], Field()] = [],
        since: Annotated[str | None, Field()] = None,
        until: Annotated[str | None, Field()] = None,
        source: Annotated[str | None, Field()] = None,
        max_events: Annotated[int, Field(ge=1, le=100000)] = 5000,
    ) -> str:
        res = store.search(
            query=query, tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, kinds=kinds,
            since=since, until=until, source=source, order="oldest", limit=min(max_events, 500), offset=0,
        )
        events = list(res.events)
        while len(events) < min(res.total, max_events):
            more = store.search(
                query=query, tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, kinds=kinds,
                since=since, until=until, source=source, order="oldest",
                limit=min(500, max_events - len(events)), offset=len(events),
            )
            if not more.events:
                break
            events.extend(more.events)
        dicts = [e.to_dict() for e in events]
        if format == "markdown":
            return "\n".join(_event_to_markdown(d) for d in dicts)
        return "\n".join(json.dumps(d, ensure_ascii=False) for d in dicts)

    # --------------------------------------------------------------- resources
    @mcp.resource("researchlog://events/{event_id}", mime_type="application/json", description="A single research-log event as JSON.")
    def event_resource(event_id: int) -> str:
        e = store.get(int(event_id))
        if e is None:
            raise ValueError(f"No event with id {event_id}")
        return json.dumps(e.to_dict(), ensure_ascii=False, indent=2)

    @mcp.resource("researchlog://tags", mime_type="application/json", description="Tag vocabulary with usage counts.")
    def tags_resource() -> str:
        return json.dumps([{"tag": t, "count": n} for t, n in store.tags(limit=5000)], indent=2)

    @mcp.resource("researchlog://stats", mime_type="application/json", description="Research-log summary statistics.")
    def stats_resource() -> str:
        return json.dumps(store.stats(), indent=2)

    return mcp


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="research-log-mcp", description="Append-only research log MCP server.")
    p.add_argument("--db", default=None, help=f"SQLite database path (default: $RESEARCH_LOG_DB or {DEFAULT_DB_PATH}).")
    p.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    args = p.parse_args()
    create_server(args.db).run(transport=args.transport)


if __name__ == "__main__":
    main()
