"""Tests for ArxivRetriever."""

import time
from types import SimpleNamespace

import feedparser
import pytest
import requests
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

from zotero_arxiv_daily.retriever.arxiv_retriever import ArxivRetriever, _run_with_hard_timeout
import zotero_arxiv_daily.retriever.arxiv_retriever as arxiv_retriever


def _sleep_and_return(value: str, delay_seconds: float) -> str:
    time.sleep(delay_seconds)
    return value


def _raise_runtime_error() -> None:
    raise RuntimeError("boom")


def test_arxiv_retriever(config, mock_feedparser, monkeypatch):
    monkeypatch.setattr(arxiv_retriever.requests, "get", lambda *a, **kw: _response(200, Path("tests/retriever/arxiv_rss_example.xml").read_bytes()))
    new_entries = [
        e for e in mock_feedparser.entries
        if e.get("arxiv_announce_type", "new") == "new"
    ]
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata retrieval must not call the query API or fetch full text")
    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", forbidden)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_html", forbidden)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_pdf", forbidden)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_tar", forbidden)

    retriever = ArxivRetriever(config)
    papers = retriever.retrieve_papers()

    assert len(papers) == len(new_entries)
    assert set(p.title for p in papers) == set(e.title for e in new_entries)
    assert all(p.full_text is None and p.abstract and p.authors for p in papers)
    assert all(not p.abstract.startswith("arXiv:") for p in papers)
    assert papers[0].authors == [s.strip() for s in new_entries[0].author.split(",")]


EMPTY_FEED = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>cs.AI updates</title></feed>'


def _response(status, content=EMPTY_FEED, headers=None):
    response = requests.Response()
    response.status_code = status
    response._content = content
    response._content_consumed = True
    response.headers.update(headers or {})
    return response


def test_feed_recovers_from_429_and_503(monkeypatch):
    responses = iter([_response(429, headers={"Retry-After": "60"}), _response(503), _response(200)])
    waits = []
    monkeypatch.setattr(arxiv_retriever.requests, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr(arxiv_retriever, "sleep", waits.append)
    monkeypatch.setattr(arxiv_retriever, "uniform", lambda *a: 0)
    assert arxiv_retriever._fetch_feed("https://rss.arxiv.org/atom/cs.AI").entries == []
    assert waits == [60, 30]


@pytest.mark.parametrize("status, attempts", [(429, 5), (500, 5), (502, 5), (503, 5), (504, 5), (403, 1), (404, 1)])
def test_feed_failures_are_not_empty_results(monkeypatch, status, attempts):
    calls = []
    def get(*args, **kwargs):
        calls.append(kwargs)
        return _response(status)
    monkeypatch.setattr(arxiv_retriever.requests, "get", get)
    monkeypatch.setattr(arxiv_retriever, "sleep", lambda _: None)
    with pytest.raises(requests.HTTPError):
        arxiv_retriever._fetch_feed("https://rss.arxiv.org/atom/cs.AI")
    assert len(calls) == attempts
    assert all(call["timeout"][0] <= 10 and call["timeout"][1] <= 45 for call in calls)


def test_retry_after_beyond_budget_stops_without_retrying_early(monkeypatch):
    monkeypatch.setattr(arxiv_retriever.requests, "get", lambda *a, **kw: _response(429, headers={"Retry-After": "3600"}))
    monkeypatch.setattr(arxiv_retriever, "sleep", lambda _: pytest.fail("Must not retry before Retry-After"))
    with pytest.raises(requests.HTTPError):
        arxiv_retriever._fetch_feed("https://rss.arxiv.org/atom/cs.AI")


def test_retry_after_dates_and_invalid_values():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    assert 118 <= arxiv_retriever._retry_after(date) <= 120
    for value in ("", "garbage", "nan", "inf", "-1"):
        assert arxiv_retriever._retry_after(value) == 0


def test_connection_failures_stop_at_elapsed_budget(monkeypatch):
    clock, calls, waits = [0], [], []
    def get(*args, **kwargs):
        calls.append(kwargs)
        clock[0] += 40
        raise requests.Timeout("read timeout")
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(arxiv_retriever, "FEED_RETRY_BUDGET", 90)
    monkeypatch.setattr(arxiv_retriever, "monotonic", lambda: clock[0])
    monkeypatch.setattr(arxiv_retriever, "uniform", lambda *a: 0)
    monkeypatch.setattr(arxiv_retriever, "sleep", sleep)
    monkeypatch.setattr(arxiv_retriever.requests, "get", get)
    with pytest.raises(requests.Timeout):
        arxiv_retriever._fetch_feed("https://rss.arxiv.org/atom/cs.AI")
    assert len(calls) == 2
    assert waits == [15]
    assert sum(calls[1]["timeout"]) <= 35


@pytest.mark.parametrize("body", [b"<html><title>Unavailable</title></html>", b"<feed", b'<rss version="2.0"><channel><title>Unexpected</title></channel></rss>'])
def test_invalid_feed_rejected(monkeypatch, body):
    monkeypatch.setattr(arxiv_retriever.requests, "get", lambda *a, **kw: _response(200, body))
    with pytest.raises(ValueError, match="Invalid arXiv Atom feed"):
        arxiv_retriever._fetch_feed("https://rss.arxiv.org/atom/cs.AI")


@pytest.mark.parametrize("debug,cross", [(False, False), (False, True), (True, False)])
def test_feed_filter_dedup_and_debug(config, monkeypatch, debug, cross):
    feed = feedparser.parse("tests/retriever/arxiv_rss_example.xml")
    feed.entries.append(deepcopy(feed.entries[0]))
    config.executor.debug = debug
    config.source.arxiv.include_cross_list = cross
    monkeypatch.setattr(arxiv_retriever, "_fetch_feed", lambda _: feed)
    papers = ArxivRetriever(config).retrieve_papers()
    expected = {e.id for e in feed.entries if e.arxiv_announce_type in ({"new", "cross"} if cross else {"new"})}
    assert len(papers) == (min(10, len(expected)) if debug else len(expected))
    assert len({p.url for p in papers}) == len(papers)


def test_invalid_entries_not_misreported_as_empty(config, monkeypatch):
    feed = feedparser.parse(EMPTY_FEED)
    feed.entries = [feedparser.FeedParserDict(id="oai:arXiv.org:../../bad", arxiv_announce_type="new")]
    monkeypatch.setattr(arxiv_retriever, "_fetch_feed", lambda _: feed)
    with pytest.raises(ValueError, match="All eligible"):
        ArxivRetriever(config).retrieve_papers()


@pytest.mark.parametrize("missing", ["title", "summary", "authors"])
def test_missing_metadata_is_logged_and_skipped(config, monkeypatch, missing):
    feed = feedparser.parse("tests/retriever/arxiv_rss_example.xml")
    entry = deepcopy(next(e for e in feed.entries if e.arxiv_announce_type == "new"))
    invalid = deepcopy(entry)
    invalid.id = "oai:arXiv.org:2609.99999v1"
    del invalid[missing]
    feed.entries = [invalid, entry]
    monkeypatch.setattr(arxiv_retriever, "_fetch_feed", lambda _: feed)
    papers = ArxivRetriever(config).retrieve_papers()
    assert len(papers) == 1
    assert papers[0].title == entry.title


def test_plain_abstract_preserves_math():
    entry = {"summary": "For x < y and z > 0, A & B are distinct.", "summary_detail": {"type": "text/plain"}}
    assert arxiv_retriever._entry_text(entry, "summary") == entry["summary"]


def test_html_metadata_cleaning(config, monkeypatch):
    feed = feedparser.parse(EMPTY_FEED)
    feed.entries = [feedparser.FeedParserDict(
        id="oai:arXiv.org:2609.12345v1", title="A &amp; B", title_detail={"type": "text/html"},
        summary="arXiv:2609.12345v1 Announce Type: new<br/>Abstract: <p>Learn <b>better</b> &amp; faster.</p>",
        summary_detail={"type": "text/html"}, authors=[{"name": "Alice, Bob"}],
    )]
    monkeypatch.setattr(arxiv_retriever, "_fetch_feed", lambda _: feed)
    paper, = ArxivRetriever(config).retrieve_papers()
    assert paper.title == "A & B"
    assert paper.abstract == "Learn better & faster."
    assert paper.authors == ["Alice", "Bob"]


def test_enrichment_falls_back_to_pdf(config, monkeypatch):
    from tests.canned_responses import make_sample_paper
    calls = []
    for name, result in (("tar", None), ("html", None), ("pdf", "PDF content")):
        def extract(paper, name=name, result=result):
            calls.append(name)
            return result
        monkeypatch.setattr(arxiv_retriever, f"extract_text_from_{name}", extract)
    paper = make_sample_paper(full_text=None)
    ArxivRetriever(config).enrich_paper(paper)
    assert calls == ["tar", "html", "pdf"]
    assert paper.full_text == "PDF content"


def test_run_with_hard_timeout_returns_value():
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 0.01), timeout=1, operation="test op", paper_title="paper"
    )
    assert result == "done"


def test_run_with_hard_timeout_returns_none_on_timeout(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 1.0), timeout=0.01, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "timed out" in warnings[0]


def test_run_with_hard_timeout_returns_none_on_failure(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _raise_runtime_error, (), timeout=1, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "boom" in warnings[0]
