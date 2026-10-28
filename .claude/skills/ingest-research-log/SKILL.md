---
name: ingest-research-log
description: Ingest an existing research log (Markdown, plain text, or JSONL notes/journal/lab notebook) into the append-only research-log MCP server as tagged, timestamped events. Use when the user asks to import, migrate, convert, back-fill, or ingest old notes, a journal, a lab notebook, or a research log into the research log, or invokes /ingest-research-log.
argument-hint: <path-to-log> [more paths...]
---

# Ingest an existing research log

Goal: turn a free-form log into discrete, timestamped, tagged events in the
`research-log` MCP server **without rewriting the author's words**, and in a way
that is safe to re-run.

The MCP tools you will use: `log_stats`, `list_tags`, `list_sources`,
`append_events`, `search_events`, `annotate_event`, `append_event`.

## 0. Preconditions

1. Call `log_stats`. If the tool is missing, the server is not configured; point
   the user at the README ("Configure in Claude Code") and stop.
2. Call `list_tags` (and `list_sources`). Note the existing vocabulary so new
   tags match it (e.g. the log already uses `optimizer`, so do not introduce
   `optimizers` or `optim`).
3. If `list_sources` already shows the file you are about to ingest, this is a
   re-run: still proceed. `source_ref` makes duplicates skip automatically.

## 1. Look at the source before splitting

Read the first ~100 lines and a sample from the middle and end. Decide:

- **Date convention**: headings like `## 2024-03-05`, `# March 5, 2024`, a bare
  date line, or dates in JSONL fields? Timezone (if unstated, treat as local
  date at midnight; the server stores UTC).
- **Natural unit of an event**: one per dated heading, one per sub-heading, or
  one per paragraph/bullet? Prefer the smallest unit that still stands on its own.
  A paragraph that only makes sense with its neighbours should stay with them.
- **Existing tag signals**: `#hashtags`, `tags:` lines, bracketed `[topic]`,
  project names in headings, file name (e.g. `project-x-log.md` → tag `project-x`).
- **Kind signals**: lines starting `TODO:`, `Idea:`, `Result:`, `Decision:`,
  `Meeting:`, `Ref:`.

## 2. Split into candidate events with the helper script

The script lives in `scripts/split_log.py` next to this file. Run it from the
research-log repo (any Python ≥3.10 works; it has no dependencies):

```bash
python3 <skill-dir>/scripts/split_log.py PATH/TO/log.md \
    --granularity heading            # or: date | paragraph
    --source-prefix "notes/"         # makes source = notes/log.md
    --default-tag project-x          # repeatable; applied to every event
    -o <scratchpad>/candidates.json
```

Read the summary it prints to stderr (count, dated/undated, date range, kinds,
top tags, text-length distribution). Then open `candidates.json` and spot-check
5–10 entries across the file. Red flags and what to do:

| Symptom | Fix |
|---|---|
| Far fewer events than dated sections | Dates not recognised. Check the format; pre-process with `sed` into `YYYY-MM-DD` headings, or hand-write JSONL. |
| Huge events (thousands of chars) | Use `--granularity paragraph`. |
| Many one-line fragments with no meaning alone | Use `--granularity heading` or `date`. |
| `undated` > 0 | Dates are inherited from the previous entry by default. Check those entries look right; if the file is not chronological, pass `--no-inherit-dates` and fix timestamps by hand. |
| Code blocks got split | Only ``` fences are protected; indent-style code is not. Fall back to `heading` granularity. |
| `_dated_explicitly: false` on many entries | Fine for diary-style logs; suspicious for logs where every entry is dated. |

The script writes two helper fields (`_line`, `_dated_explicitly`) that
`append_events` does not accept — strip them (step 4 does).

## 3. Enrich tags and kinds (lightly)

For each candidate, set `tags` to 1–5 lower-case, hyphenated tags and a sensible
`kind`. Rules:

- Keep `text` **verbatim**. Never summarise, fix typos, or reorder the author's
  words. The only edits are removing `tags:` lines the script already stripped.
- Reuse existing tags from `list_tags` where they fit; add new ones sparingly.
  Good tags: project names, methods, datasets, people/collaborator handles,
  venues, recurring themes. Bad tags: one-off adjectives, full sentences.
- `kind` ∈ {note, idea, question, hypothesis, experiment, result, decision,
  todo, reference, meeting, annotation}. Default to `note` when unsure.
- For logs under ~200 events, read every entry and tag by hand. For larger
  logs, trust the script's heuristics and tag by heading/section instead.
- Always include one tag that identifies the import (the project or notebook
  name) so the whole batch can be found later with `tags_all`.

Do this by editing `candidates.json` with a short Python script, not by
retyping entries.

## 4. Append in batches

Sort by timestamp (the script already does), then call `append_events` with
batches of **25–50 events**, each event carrying `source` and `source_ref`
exactly as the script produced them. Example of one call:

```json
{
  "events": [
    {"text": "...", "title": "Baseline runs", "tags": ["project-x", "baseline"],
     "kind": "result", "timestamp": "2024-03-04T00:00:00",
     "source": "notes/log.md", "source_ref": "notes/log.md#2024-03-04/1e307f5858"}
  ],
  "skip_duplicates": true
}
```

Track `appended` / `skipped` totals from each response. If a batch errors
(bad timestamp, empty text), nothing in that batch was written: fix the
offending entry and resend the whole batch.

A convenient way to generate batches without manual JSON: a short Python
snippet that loads `candidates.json`, drops keys starting with `_`, and prints
slices of 40 as JSON for you to paste into tool calls.

## 5. Verify

1. `list_sources` → the new source shows the expected count and date range.
2. `search_events` with `source` set and `order: "oldest"`, `limit: 5`, then
   `order: "newest"`: first and last entries match the file.
3. Pick two distinctive phrases from the original and `search_events` them.
4. Compare totals: candidates = appended + skipped. Report any gap.

## 6. Record the ingestion itself

Append one closing event describing the import so the log is self-documenting:

```json
{"text": "Ingested notes/log.md: 142 events (2024-03-04 … 2024-11-20), 3 skipped as duplicates. Granularity: heading. Tags added: project-x, optimizer, reading.",
 "kind": "note", "tags": ["ingest", "project-x"], "source": "notes/log.md"}
```

## Append-only etiquette

- If you spot an error in an *ingested* entry (wrong date, obvious typo that
  changes meaning), do **not** re-ingest. Use `annotate_event` on that event id
  with the correction.
- If the user later edits the original file and wants the additions ingested,
  just re-run steps 2–5; unchanged entries are skipped by `source_ref`, new or
  changed paragraphs are appended. Changed paragraphs appear as *new* events;
  mention this so the user can annotate the old one if needed.
- Never ingest the same file under two different `source` names; that defeats
  deduplication.

## Non-Markdown inputs

- **JSONL** (one object per line): the script maps `text|content|body|note`,
  `tags`, `timestamp|ts|date|created`, `kind|type`, `title` automatically.
- **CSV / spreadsheets**: convert to JSONL first (`python -c` with the csv
  module), then run the script.
- **Obsidian / Logseq daily notes** (one file per day): pass all files at once
  with a glob; the filename date is *not* read, so make sure each file starts
  with its date as a heading, or pre-pend it with a one-line shell loop.
- **Email / chat exports**: usually best handled as JSONL with
  `timestamp`, `text`, and `tags: [sender]`.
