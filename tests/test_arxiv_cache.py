"""Offline tests for the arXiv cache using a mocked HTTP transport."""

import io

import httpx
import pytest
from pypdf import PdfWriter

from research_log_mcp.arxiv_cache import ArxivCache, ArxivError, parse_arxiv_id


def make_pdf(pages=("hello transformer world", "second page about attention")) -> bytes:
    w = PdfWriter()
    for _ in pages:
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/{vid}</id>
    <title>Attention Is
    All You Need</title>
    <summary>The dominant sequence transduction models use attention.</summary>
    <published>2017-06-12T17:57:34Z</published>
    <updated>2023-08-02T00:41:18Z</updated>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:doi>10.0000/x</arxiv:doi>
    <category term="cs.CL"/><category term="cs.LG"/>
  </entry>
</feed>"""

MISSING = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/api/errors#incorrect_id_format_for_9999.99999</id>
<title>Error</title><summary>incorrect id format for 9999.99999</summary>
</entry></feed>"""


class FakeArxiv:
    """Pretends to be export.arxiv.org + arxiv.org; counts requests."""

    def __init__(self, latest_version=7):
        self.latest = latest_version
        self.calls: list[str] = []
        self.pdf = make_pdf()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.host == "export.arxiv.org":
            ident = request.url.params["id_list"]
            if ident.startswith("9999"):
                return httpx.Response(200, text=MISSING)
            base, v = parse_arxiv_id(ident)
            vid = f"{base}v{v or self.latest}"
            return httpx.Response(200, text=ATOM.format(vid=vid))
        if request.url.path.startswith("/pdf/"):
            return httpx.Response(200, content=self.pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeArxiv()


@pytest.fixture
def cache(tmp_path, fake):
    c = ArxivCache(tmp_path / "arxiv", transport=httpx.MockTransport(fake.handler), min_interval=0)
    yield c
    c.close()


def test_parse_ids():
    assert parse_arxiv_id("1706.03762") == ("1706.03762", None)
    assert parse_arxiv_id("1706.03762v5") == ("1706.03762", 5)
    assert parse_arxiv_id("arXiv:1706.03762v2") == ("1706.03762", 2)
    assert parse_arxiv_id("https://arxiv.org/abs/1706.03762") == ("1706.03762", None)
    assert parse_arxiv_id("https://arxiv.org/pdf/1706.03762v3.pdf") == ("1706.03762", 3)
    assert parse_arxiv_id("http://export.arxiv.org/abs/hep-th/9901001v3") == ("hep-th/9901001", 3)
    assert parse_arxiv_id("math.GT/0309136") == ("math.GT/0309136", None)
    with pytest.raises(ArxivError):
        parse_arxiv_id("not a paper")


def test_miss_then_hit(cache, fake, tmp_path):
    paper, from_cache = cache.get("1706.03762")
    assert not from_cache
    assert paper.versioned_id == "1706.03762v7"
    assert paper.title == "Attention Is All You Need"
    assert paper.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert paper.categories == ["cs.CL", "cs.LG"] and paper.doi == "10.0000/x"
    assert paper.pages == 2 and paper.bytes == len(fake.pdf)
    assert (tmp_path / "arxiv" / "pdf" / "1706.03762v7.pdf").read_bytes() == fake.pdf
    assert len(fake.calls) == 2  # one API call, one PDF download

    # Unversioned, versioned, and URL forms are all served from cache; arXiv is never contacted again.
    for ident in ("1706.03762", "1706.03762v7", "https://arxiv.org/abs/1706.03762", "arxiv:1706.03762v7"):
        p, hit = cache.get(ident)
        assert hit and p.versioned_id == "1706.03762v7"
    assert len(fake.calls) == 2
    assert cache.get("1706.03762")[0].hits == 6
    assert cache.lookup("1706.03762") is not None and cache.lookup("1706.03762v3") is None


def test_specific_older_version_is_fetched_separately(cache, fake):
    cache.get("1706.03762")
    p, hit = cache.get("1706.03762v5")
    assert not hit and p.versioned_id == "1706.03762v5"
    assert len(fake.calls) == 4
    assert {p.versioned_id for p in cache.list()} == {"1706.03762v7", "1706.03762v5"}
    assert cache.stats()["papers"] == 2


def test_refresh_refetches(cache, fake):
    cache.get("1706.03762")
    fake.latest = 8
    p, hit = cache.get("1706.03762", refresh=True)
    assert not hit and p.versioned_id == "1706.03762v8"
    # Unversioned requests now resolve to the newest cached version.
    assert cache.get("1706.03762")[0].versioned_id == "1706.03762v8"


def test_unknown_id_errors(cache):
    with pytest.raises(ArxivError, match="does not know"):
        cache.get("9999.99999")


def test_old_style_id_filenames(cache, tmp_path):
    p, _ = cache.get("hep-th/9901001")
    assert p.versioned_id == "hep-th/9901001v7"
    assert (tmp_path / "arxiv" / "pdf" / "hep-th_9901001v7.pdf").exists()


def test_text_pages_and_search(cache, monkeypatch):
    monkeypatch.setattr(ArxivCache, "extract_text", staticmethod(lambda b: ("intro about transformers\fmethods: multi-head attention\fresults", 3)))
    paper, _ = cache.get("1706.03762")
    text, first, last = cache.read_text(paper, first_page=2, last_page=2)
    assert (first, last) == (2, 2) and "multi-head attention" in text and "intro" not in text
    text, first, last = cache.read_text(paper, first_page=1, last_page=99)
    assert last == 3
    hits = cache.search("multi-head")
    assert len(hits) == 1 and hits[0]["versioned_id"] == "1706.03762v7" and "[multi-head]" in hits[0]["snippet"]
    assert cache.search("attention") and cache.search("unrelated-term") == []
    assert cache.search('bad "quote (')  == []  # falls back to safe query without raising


def test_missing_pdf_file_triggers_refetch(cache, fake, tmp_path):
    p, _ = cache.get("1706.03762")
    (tmp_path / "arxiv" / "pdf" / "1706.03762v7.pdf").unlink()
    p2, hit = cache.get("1706.03762")
    assert not hit and (tmp_path / "arxiv" / "pdf" / "1706.03762v7.pdf").exists()
