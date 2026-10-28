# research-log-mcp

An **append-only, searchable, tagged research log** served over the
[Model Context Protocol](https://modelcontextprotocol.io). Point Claude (or any
MCP client) at it and say "log that the warmup sweep fixed the divergence" or
"what did I decide about the optimizer in March?".

- **Append-only.** Events are immutable. SQLite triggers reject `UPDATE`/`DELETE`
  even for direct database access. Corrections are new events that *reference*
  the old one.
- **Full-text search** via SQLite FTS5 with Porter stemming: `AND`/`OR`/`NOT`,
  `"exact phrases"`, `prefix*`. Matches are returned with highlighted snippets.
- **Tag filtering** that composes with text search: `tags_all`, `tags_any`,
  `tags_none`, plus `kind`, time range, and source filters.
- **Idempotent bulk ingest** through `source_ref`, so importing an old notebook
  twice is harmless.
- **Single file** at `~/.research-log/log.sqlite3`; back it up by copying it.
- Ships with a Claude Code **skill** (`/ingest-research-log`) that migrates an
  existing Markdown/text/JSONL log into events.

## Install

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:JayRothenberger/research-tools.git
cd research-tools
uv sync
uv run research-log-mcp --help
```

### Configure in Claude Code

Opening this repository in Claude Code picks up [`.mcp.json`](.mcp.json)
automatically. To use the server from *any* project, register it globally:

```bash
claude mcp add --scope user research-log \
  --env RESEARCH_LOG_DB="$HOME/.research-log/log.sqlite3" \
  -- uv run --project /ABSOLUTE/PATH/TO/research-tools research-log-mcp
```

### Configure in Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "research-log": {
      "command": "uv",
      "args": ["run", "--project", "/ABSOLUTE/PATH/TO/research-tools", "research-log-mcp"],
      "env": { "RESEARCH_LOG_DB": "/Users/you/.research-log/log.sqlite3" }
    }
  }
}
```

### Database location

Resolution order: `--db PATH` flag → `RESEARCH_LOG_DB` env var →
`~/.research-log/log.sqlite3`. The directory is created on first run.
The database uses WAL mode, so copy all of `log.sqlite3*` when backing up, or
use `export_events` for a plain JSONL/Markdown dump.

## Tools

| Tool | Purpose |
|---|---|
| `append_event` | Record one event: `text`, `tags`, `kind`, optional `title`, `timestamp` (back-dating allowed), `source`, `source_ref`, `refs`, `metadata`. |
| `append_events` | Atomic batch append. Events whose `source_ref` already exists are skipped and reported. |
| `annotate_event` | Append a follow-up / correction / outcome that references an earlier event (the append-only way to "edit"). |
| `search_events` | Full-text `query` + `tags_all` / `tags_any` / `tags_none` + `kinds` + `since` / `until` + `source`; `order` = newest / oldest / relevance; paginated. |
| `get_event` | One event plus every later event that references it. |
| `get_events` | Several events by id. |
| `list_tags` | Tag vocabulary with counts, optional prefix filter. |
| `list_sources` | Ingestion sources with counts and date ranges. |
| `log_stats` | Totals, date range, kinds, database path. |
| `export_events` | Same filters as search; returns JSONL or Markdown for backups and reports. |

Resources: `researchlog://events/{id}`, `researchlog://tags`, `researchlog://stats`.

### Event model

```json
{
  "id": 42,                      // monotonic, assigned on append
  "uid": "…",                    // random hex, stable across exports
  "ts": "2026-03-05T14:30:00+00:00",   // when it happened (UTC)
  "recorded_at": "2026-03-05T14:31:07+00:00",  // when it was appended
  "kind": "result",              // note | idea | question | hypothesis | experiment | result | decision | todo | reference | meeting | annotation | …
  "title": "Warmup sweep",
  "text": "Warmup 2000 steps fixes the divergence at lr 3e-4.",
  "tags": ["project-x", "optimizer"],
  "source": "notes/2026-log.md",   // for ingested entries
  "source_ref": "notes/2026-log.md#2026-03-05/1e307f5858",
  "refs": [17],                  // earlier events this one refers to
  "metadata": {}
}
```

Tags are normalised: trimmed, lower-cased, leading `#` removed, internal
whitespace → `-`. Timestamps accept ISO-8601 (`2026-03-05`, `2026-03-05T14:30Z`,
with offsets); naive values are treated as UTC.

### Search examples

| Ask | Call |
|---|---|
| Everything about the optimizer on project X | `search_events(query="optimizer", tags_all=["project-x"])` |
| Decisions made in Q1 | `search_events(kinds=["decision"], since="2026-01-01", until="2026-03-31", order="oldest")` |
| Reading notes not about transformers | `search_events(tags_any=["reading","paper"], tags_none=["transformers"])` |
| Phrase + prefix | `search_events(query='"learning rate" AND diverg*', order="relevance")` |
| Open todos | `search_events(kinds=["todo"])` then `get_event` to see if any annotation closed them |

## Ingesting an existing log

The skill in [`.claude/skills/ingest-research-log/`](.claude/skills/ingest-research-log/SKILL.md)
walks Claude through importing a Markdown, plain-text, or JSONL log:

1. Inspect the source and pick a granularity (heading / date / paragraph).
2. Run `scripts/split_log.py` to produce candidate events with timestamps,
   extracted `#tags`, inferred kinds, and stable `source_ref`s.
3. Lightly tag/kind each candidate without changing its text.
4. `append_events` in batches; re-runs skip duplicates.
5. Verify with `list_sources` / `search_events` and append a closing
   "ingested X" event.

In Claude Code, with this repo open (or the skill copied to
`~/.claude/skills/`), run:

```
/ingest-research-log path/to/old-log.md
```

The splitter can also be used standalone:

```bash
uv run python .claude/skills/ingest-research-log/scripts/split_log.py old-log.md \
  --granularity paragraph --default-tag project-x -o candidates.json
```

## Development

```bash
uv sync
uv run pytest            # store tests + end-to-end tests over stdio
uv run research-log-mcp --db /tmp/scratch.sqlite3   # run against a throwaway db
```

Layout:

```
src/research_log_mcp/store.py    SQLite schema, append-only triggers, FTS5 search
src/research_log_mcp/server.py   MCP tools/resources (mcp >= 2.0, MCPServer API)
tests/                           pytest suite
.claude/skills/ingest-research-log/   Claude Code skill + split_log.py helper
.mcp.json                        Claude Code project-level server registration
```

## Design notes

- Append-only is enforced in the database, not just the API: `BEFORE UPDATE`
  and `BEFORE DELETE` triggers on `events` and `event_tags` raise. Dropping the
  triggers is the only way around it, and that is a deliberate act.
- `ts` (when it happened) and `recorded_at` (when it was logged) are separate so
  back-dated imports stay honest.
- Free-text queries are tried verbatim as FTS5 syntax first; if that fails to
  parse, every token is quoted and AND-ed, so odd punctuation never errors.
- Tags are duplicated into the FTS index so `query="optimizer"` finds entries
  tagged `optimizer` even when the word is absent from the text.
