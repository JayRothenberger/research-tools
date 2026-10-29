"""Exercise the MCP server over a real stdio transport (server runs as a subprocess)."""

import json
import sys
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _ok(res):
    assert not res.is_error, res.content
    return res.structured_content if res.structured_content is not None else res.content[0].text


@asynccontextmanager
async def connect(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "research_log_mcp", "--db", str(tmp_path / "log.sqlite3"), "--arxiv-dir", str(tmp_path / "arxiv")]
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


async def test_tool_listing(tmp_path):
    async with connect(tmp_path) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {
            "append_event", "append_events", "annotate_event", "search_events", "get_event",
            "get_events", "list_tags", "list_sources", "log_stats", "export_events",
            "arxiv_fetch", "arxiv_text", "arxiv_search_cache", "arxiv_list_cache",
        }


async def test_append_search_annotate_roundtrip(tmp_path):
    async with connect(tmp_path) as session:
        a = _ok(await session.call_tool("append_event", {
            "text": "Adam with lr 3e-4 diverged on run 17", "tags": ["Project-X", "#optimizer"],
            "kind": "result", "timestamp": "2026-03-05T10:00:00Z",
        }))
        assert a["id"] == 1 and a["tags"] == ["optimizer", "project-x"]
        assert a["ts"] == "2026-03-05T10:00:00+00:00"

        batch = _ok(await session.call_tool("append_events", {"events": [
            {"text": "Try SGD momentum", "tags": ["project-x"], "kind": "idea", "source": "old.md", "source_ref": "old.md#1"},
            {"text": "Read the Lion paper", "tags": ["reading"], "kind": "reference", "source": "old.md", "source_ref": "old.md#2"},
            {"text": "Try SGD momentum", "tags": ["project-x"], "source": "old.md", "source_ref": "old.md#1"},
        ]}))
        assert batch["appended"] == 2 and batch["skipped"] == 1
        assert batch["skipped_details"][0]["existing_id"] == 2

        ann = _ok(await session.call_tool("annotate_event", {"event_id": 1, "text": "Root cause: warmup schedule."}))
        assert ann["refs"] == [1] and ann["kind"] == "annotation"
        assert set(ann["tags"]) == {"optimizer", "project-x"}

        hit = _ok(await session.call_tool("search_events", {"query": "diverg*", "tags_all": ["optimizer"]}))
        assert hit["total"] == 1 and "[diverged]" in hit["events"][0]["snippet"]

        tagged = _ok(await session.call_tool("search_events", {
            "tags_any": ["reading", "project-x"], "order": "oldest"}))
        assert [e["id"] for e in tagged["events"]] == [1, 2, 3, 4]  # annotation inherited project-x

        narrowed = _ok(await session.call_tool("search_events", {
            "tags_any": ["reading", "project-x"], "tags_none": ["reading"], "kinds": ["result", "idea"], "order": "oldest"}))
        assert [e["id"] for e in narrowed["events"]] == [1, 2]

        got = _ok(await session.call_tool("get_event", {"event_id": 1}))
        assert [e["id"] for e in got["referenced_by"]] == [4]

        many = _ok(await session.call_tool("get_events", {"event_ids": [1, 3, 99]}))
        assert [e["id"] for e in many["events"]] == [1, 3] and many["missing"] == [99]

        tags = _ok(await session.call_tool("list_tags", {}))
        assert tags["tags"][0] == {"tag": "project-x", "count": 3}

        sources = _ok(await session.call_tool("list_sources", {}))
        assert sources["sources"][0]["source"] == "old.md"

        stats = _ok(await session.call_tool("log_stats", {}))
        assert stats["events"] == 4

        md = await session.call_tool("export_events", {"format": "markdown", "tags_all": ["project-x"]})
        assert md.content[0].text.startswith("### [1] 2026-03-05")
        jl = await session.call_tool("export_events", {"format": "jsonl", "kinds": ["reference"]})
        assert json.loads(jl.content[0].text)["id"] == 3


async def test_errors_are_reported(tmp_path):
    async with connect(tmp_path) as session:
        missing = await session.call_tool("get_event", {"event_id": 9999})
        assert missing.is_error and "No event with id 9999" in missing.content[0].text

        bad_ts = await session.call_tool("append_event", {"text": "x", "timestamp": "last tuesday"})
        assert bad_ts.is_error and "Could not parse timestamp" in bad_ts.content[0].text

        _ok(await session.call_tool("append_event", {"text": "x", "source_ref": "dup"}))
        dup = await session.call_tool("append_event", {"text": "y", "source_ref": "dup"})
        assert dup.is_error and "already recorded" in dup.content[0].text


async def test_resources(tmp_path):
    async with connect(tmp_path) as session:
        _ok(await session.call_tool("append_event", {"text": "resource me", "tags": ["r"]}))
        uris = {str(r.uri) for r in (await session.list_resources()).resources}
        assert {"researchlog://tags", "researchlog://stats"} <= uris
        templates = {str(t.uri_template) for t in (await session.list_resource_templates()).resource_templates}
        assert "researchlog://events/{event_id}" in templates
        ev = await session.read_resource("researchlog://events/1")
        assert json.loads(ev.contents[0].text)["text"] == "resource me"
        tags = await session.read_resource("researchlog://tags")
        assert json.loads(tags.contents[0].text) == [{"tag": "r", "count": 1}]


async def test_arxiv_tools_offline(tmp_path, monkeypatch):
    """arXiv tools are registered; errors for unparsable ids are reported (no network needed)."""
    async with connect(tmp_path) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"arxiv_fetch", "arxiv_text", "arxiv_search_cache", "arxiv_list_cache"} <= names
        templates = {str(t.uri_template) for t in (await session.list_resource_templates()).resource_templates}
        assert {"arxiv://{arxiv_id}/pdf", "arxiv://{arxiv_id}/text", "arxiv://{arxiv_id}/meta"} <= templates
        bad = await session.call_tool("arxiv_fetch", {"id_or_url": "not an id"})
        assert bad.is_error and "Could not parse an arXiv identifier" in bad.content[0].text
        empty = _ok(await session.call_tool("arxiv_list_cache", {}))
        assert empty["stats"]["papers"] == 0 and empty["papers"] == []
        none = _ok(await session.call_tool("arxiv_search_cache", {"query": "anything"}))
        assert none["results"] == []
