import sqlite3

import pytest

from research_log_mcp.store import DuplicateSourceRef, NewEvent, ResearchLogStore, normalize_tag, parse_timestamp


@pytest.fixture
def store(tmp_path):
    with ResearchLogStore(tmp_path / "log.sqlite3") as s:
        yield s


def test_normalize_tag():
    assert normalize_tag("  #Project X ") == "project-x"
    assert normalize_tag("ML/Optimizers") == "ml/optimizers"


def test_parse_timestamp():
    assert parse_timestamp("2026-03-05") == "2026-03-05T00:00:00+00:00"
    assert parse_timestamp("2026-03-05T14:30:00Z") == "2026-03-05T14:30:00+00:00"
    assert parse_timestamp("2026-03-05T09:30:00-05:00") == "2026-03-05T14:30:00+00:00"
    with pytest.raises(ValueError):
        parse_timestamp("yesterday")


def test_append_and_get(store):
    e = store.append(NewEvent(text="hello world", tags=["A", "#b", "a"], kind="Idea", title=" T "))
    assert e.id == 1 and e.tags == ["a", "b"] and e.kind == "idea" and e.title == "T"
    got = store.get(1)
    assert got is not None and got.text == "hello world"
    assert store.get(99) is None


def test_empty_text_rejected(store):
    with pytest.raises(ValueError):
        store.append(NewEvent(text="   "))


def test_refs_must_exist(store):
    with pytest.raises(ValueError):
        store.append(NewEvent(text="x", refs=[42]))
    a = store.append(NewEvent(text="a"))
    b = store.append(NewEvent(text="b", refs=[a.id]))
    assert b.refs == [a.id]
    assert [e.id for e in store.referencing(a.id)] == [b.id]


def test_append_only_enforced_at_sql_level(store):
    store.append(NewEvent(text="immutable", tags=["t"]))
    conn = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET text = 'changed' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM event_tags WHERE event_id = 1")
    conn.close()
    assert store.get(1).text == "immutable"


def test_source_ref_idempotency(store):
    first = store.append(NewEvent(text="a", source="f.md", source_ref="f.md#1"))
    results = store.append_many(
        [NewEvent(text="a", source_ref="f.md#1"), NewEvent(text="b", source_ref="f.md#2")], skip_duplicates=True
    )
    assert results[0] == (None, first.id)
    assert results[1][0] is not None and results[1][0].text == "b"
    with pytest.raises(DuplicateSourceRef):
        store.append_many([NewEvent(text="c", source_ref="f.md#2")], skip_duplicates=False)
    assert store.stats()["events"] == 2  # failed batch wrote nothing


def test_batch_is_atomic(store):
    with pytest.raises(ValueError):
        store.append_many([NewEvent(text="ok"), NewEvent(text="")], skip_duplicates=True)
    assert store.stats()["events"] == 0


def test_search_text_and_tags(store):
    store.append(NewEvent(text="Adam diverged at lr 3e-4", tags=["optimizer", "proj-x"], kind="result", ts="2026-01-01"))
    store.append(NewEvent(text="Switch to SGD with momentum", tags=["optimizer", "proj-x"], kind="decision", ts="2026-01-02"))
    store.append(NewEvent(text="Read the Lion optimizer paper", tags=["reading"], kind="reference", ts="2026-01-03"))
    store.append(NewEvent(text="Unrelated grant deadline", tags=["admin"], ts="2026-01-04"))

    r = store.search(query="optimizer")
    assert r.total == 3  # tag match on two + text match on one
    assert r.events[0].snippet is not None

    r = store.search(query="diverg*")
    assert [e.id for e in r.events] == [1] and "[diverged]" in r.events[0].snippet

    r = store.search(query="momentum OR Lion", order="oldest")
    assert [e.id for e in r.events] == [2, 3]

    r = store.search(query='"with momentum"')
    assert r.total == 1

    r = store.search(query='unbalanced "quote (x')  # falls back to safe tokenised query
    assert r.total == 0

    assert store.search(tags_all=["optimizer", "proj-x"]).total == 2
    assert store.search(tags_any=["reading", "admin"], order="oldest").events[0].id == 3
    assert store.search(tags_all=["optimizer"], tags_none=["proj-x"]).total == 0
    assert store.search(kinds=["decision"]).events[0].id == 2
    assert store.search(since="2026-01-02", until="2026-01-03").total == 2
    assert store.search(query="optimizer", tags_none=["reading"]).total == 2


def test_pagination_and_order(store):
    for i in range(7):
        store.append(NewEvent(text=f"event {i}", ts=f"2026-02-0{i + 1}"))
    r = store.search(limit=3, offset=0)
    assert r.total == 7 and [e.text for e in r.events] == ["event 6", "event 5", "event 4"]
    r = store.search(limit=3, offset=6, order="oldest")
    assert [e.text for e in r.events] == ["event 6"]


def test_tags_sources_kinds_stats(store):
    store.append(NewEvent(text="a", tags=["x", "y"], source="s1", source_ref="s1#1"))
    store.append(NewEvent(text="b", tags=["x"], source="s1", source_ref="s1#2", kind="idea"))
    assert store.tags() == [("x", 2), ("y", 1)]
    assert store.tags(prefix="y") == [("y", 1)]
    assert store.sources()[0]["source"] == "s1" and store.sources()[0]["count"] == 2
    st = store.stats()
    assert st["events"] == 2 and st["distinct_tags"] == 2 and st["kinds"] == {"idea": 1, "note": 1}
    assert [e.id for e in store.iter_all()] == [1, 2]


def test_concurrent_writers(tmp_path):
    import threading

    from research_log_mcp.store import NewEvent, ResearchLogStore

    db = tmp_path / "log.sqlite3"
    errors: list[Exception] = []

    def work(n: int) -> None:
        try:
            st = ResearchLogStore(db)  # separate connection, like a separate server process
            for i in range(20):
                st.append(NewEvent(text=f"w{n} e{i}"))
            st.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    with ResearchLogStore(db) as st:
        assert st._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 120
