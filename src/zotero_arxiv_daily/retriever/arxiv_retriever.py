from .base import BaseRetriever, register_retriever
import arxiv
from arxiv import Result as ArxivResult
from ..protocol import Paper
from ..utils import extract_markdown_from_pdf, extract_tex_code_from_tar
from tempfile import TemporaryDirectory
import feedparser
import multiprocessing
import os
from queue import Empty
from time import sleep
from typing import Any, Callable, TypeVar
from loguru import logger
import requests
import re
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from random import uniform
from time import monotonic

T = TypeVar("T")

DOWNLOAD_TIMEOUT = (10, 60)
PDF_EXTRACT_TIMEOUT = 180
TAR_EXTRACT_TIMEOUT = 180
HTML_EXTRACT_TIMEOUT = 60
FEED_MAX_ATTEMPTS = 5
FEED_RETRY_BUDGET = 300


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")


def _entry_text(entry: dict, field: str) -> str:
    value = entry.get(field, "")
    if entry.get(f"{field}_detail", {}).get("type") in {"text/html", "application/xhtml+xml"}:
        parser = _TextParser()
        parser.feed(value)
        value = "".join(parser.parts)
    return value.strip()


def _retry_after(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def _fetch_feed(url: str) -> feedparser.FeedParserDict:
    """One bounded retry layer; malformed feeds must not masquerade as empty days."""
    deadline = monotonic() + FEED_RETRY_BUDGET
    for attempt in range(FEED_MAX_ATTEMPTS):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("arXiv feed retry budget exhausted")
        try:
            with requests.get(
                url,
                headers={"User-Agent": "zotero-arxiv-daily/1.0 (arXiv daily recommendations)"},
                timeout=(min(10, remaining / 2), min(45, remaining / 2)),
            ) as response:
                response.raise_for_status()
                feed = feedparser.parse(response.content)
            if feed.get("bozo") or feed.get("version") != "atom10" or not feed.feed.get("title"):
                raise ValueError("Invalid arXiv Atom feed; refusing to report no new papers")
            if "Feed error for query" in feed.feed.title:
                raise ValueError(f"Invalid arXiv category query: {url}")
            return feed
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            response = exc.response
            status = response.status_code if response is not None else None
            if status is not None and status not in {429, 500, 502, 503, 504}:
                raise
            wait = 15 * 2 ** attempt + uniform(0, 3)
            if response is not None:
                wait = max(wait, _retry_after(response.headers.get("Retry-After", "")))
            if attempt + 1 == FEED_MAX_ATTEMPTS or wait >= deadline - monotonic():
                logger.error(f"arXiv feed failed: HTTP {status or type(exc).__name__}; retry count or time budget exhausted")
                raise
            logger.warning(f"arXiv feed HTTP {status or type(exc).__name__}, retry {attempt + 1}/{FEED_MAX_ATTEMPTS} in {wait:.1f}s")
            sleep(wait)
    raise RuntimeError("arXiv feed request failed")


def _download_file(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)


def _run_in_subprocess(
    result_queue: Any,
    func: Callable[..., T | None],
    args: tuple[Any, ...],
) -> None:
    try:
        result_queue.put(("ok", func(*args)))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _run_with_hard_timeout(
    func: Callable[..., T | None],
    args: tuple[Any, ...],
    *,
    timeout: float,
    operation: str,
    paper_title: str,
) -> T | None:
    start_methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in start_methods else start_methods[0])
    result_queue = context.Queue()
    process = context.Process(target=_run_in_subprocess, args=(result_queue, func, args))
    process.start()

    try:
        status, payload = result_queue.get(timeout=timeout)
    except Empty:
        if process.is_alive():
            process.kill()
        process.join(5)
        result_queue.close()
        result_queue.join_thread()
        logger.warning(f"{operation} timed out for {paper_title} after {timeout} seconds")
        return None

    process.join(5)
    result_queue.close()
    result_queue.join_thread()

    if status == "ok":
        return payload

    logger.warning(f"{operation} failed for {paper_title}: {payload}")
    return None


def _extract_text_from_pdf_worker(pdf_url: str) -> str:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.pdf")
        _download_file(pdf_url, path)
        return extract_markdown_from_pdf(path)


def _extract_text_from_html_worker(html_url: str) -> str | None:
    import trafilatura

    downloaded = trafilatura.fetch_url(html_url)
    if downloaded is None:
        raise ValueError(f"Failed to download HTML from {html_url}")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    if not text:
        raise ValueError(f"No text extracted from {html_url}")
    return text


def _extract_text_from_tar_worker(source_url: str, paper_id: str, paper_title: str | None = None) -> str | None:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.tar.gz")
        _download_file(source_url, path)
        file_contents = extract_tex_code_from_tar(path, paper_id, paper_title=paper_title)
        if not file_contents or "all" not in file_contents:
            raise ValueError("Main tex file not found.")
        return file_contents["all"]


@register_retriever("arxiv")
class ArxivRetriever(BaseRetriever):
    conversion_delay = 0  # Metadata conversion is local; no per-paper network requests.

    def __init__(self, config):
        super().__init__(config)
        if self.config.source.arxiv.category is None:
            raise ValueError("category must be specified for arxiv.")

    def _retrieve_raw_papers(self) -> list[ArxivResult]:
        query = '+'.join(self.config.source.arxiv.category)
        include_cross_list = self.config.source.arxiv.get("include_cross_list", False)
        feed = _fetch_feed(f"https://rss.arxiv.org/atom/{query}")
        raw_papers = []
        allowed_announce_types = {"new", "cross"} if include_cross_list else {"new"}
        entries = [
            i for i in feed.entries
            if i.get("arxiv_announce_type", "new") in allowed_announce_types
        ]
        seen = set()
        skipped = 0
        for entry in entries:
            pid = entry.get("id", "").removeprefix("oai:arXiv.org:")
            if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})(?:v[1-9]\d*)?", pid):
                logger.warning(f"Skipping invalid arXiv ID: {pid!r}")
                skipped += 1
                continue
            key = re.sub(r"v\d+$", "", pid)
            if key in seen:
                continue
            title = _entry_text(entry, "title")
            abstract = _entry_text(entry, "summary")
            abstract = re.sub(r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*", "", abstract, count=1).strip()
            authors = [
                arxiv.Result.Author(name.strip())
                for author in entry.get("authors", [])
                for name in author.get("name", "").split(",")
                if name.strip()
            ]
            if not title or not abstract or not authors:
                logger.warning(f"Skipping arXiv {pid}: missing title, abstract or authors in feed")
                skipped += 1
                continue
            seen.add(key)
            raw_papers.append(ArxivResult(
                entry_id=f"https://arxiv.org/abs/{pid}",
                title=title,
                summary=abstract,
                authors=authors,
                links=[ArxivResult.Link(f"https://arxiv.org/pdf/{pid}", title="pdf")],
            ))
            if self.config.executor.debug and len(raw_papers) == 10:
                break
        logger.info(f"arXiv feed: {len(feed.entries)} entries, {len(raw_papers)} candidates, {skipped} invalid entries skipped")
        if entries and not raw_papers:
            raise ValueError("All eligible arXiv feed entries are invalid; refusing to report no new papers")
        return raw_papers

    def convert_to_paper(self, raw_paper: ArxivResult) -> Paper:
        title = raw_paper.title
        authors = [a.name for a in raw_paper.authors]
        abstract = raw_paper.summary
        pdf_url = raw_paper.pdf_url
        return Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=raw_paper.entry_id,
            pdf_url=pdf_url,
        )

    def enrich_paper(self, paper: Paper) -> None:
        if paper.full_text:
            return
        raw_paper = ArxivResult(
            entry_id=paper.url, title=paper.title,
            links=[ArxivResult.Link(paper.pdf_url, title="pdf")] if paper.pdf_url else [],
        )
        for extract in (extract_text_from_tar, extract_text_from_html, extract_text_from_pdf):
            paper.full_text = extract(raw_paper)
            if paper.full_text:
                return
        logger.warning(f"No full text for {paper.url}; generating TLDR from abstract")


def extract_text_from_html(paper: ArxivResult) -> str | None:
    html_url = paper.entry_id.replace("/abs/", "/html/")
    return _run_with_hard_timeout(
        _extract_text_from_html_worker,
        (html_url,),
        timeout=HTML_EXTRACT_TIMEOUT,
        operation="HTML extraction",
        paper_title=paper.title,
    )


def extract_text_from_pdf(paper: ArxivResult) -> str | None:
    if paper.pdf_url is None:
        logger.warning(f"No PDF URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_pdf_worker,
        (paper.pdf_url,),
        timeout=PDF_EXTRACT_TIMEOUT,
        operation="PDF extraction",
        paper_title=paper.title,
    )


def extract_text_from_tar(paper: ArxivResult) -> str | None:
    source_url = paper.source_url()
    if source_url is None:
        logger.warning(f"No source URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_tar_worker,
        (source_url, paper.entry_id, paper.title),
        timeout=TAR_EXTRACT_TIMEOUT,
        operation="Tar extraction",
        paper_title=paper.title,
    )
