"""What the retrieval tools do with the pages of a crawl job: rank their passages, and extract
structured data from them.

The crawl itself is the job manager's. Pages are read back through `get_document`, so a page that
was just crawled comes from the cache and costs no request. A page that cannot be read again (the
cache is off, or its entry is gone and the site now refuses) is counted and left out.
"""

import logging
from typing import Any

from weft.extract.llm import llm_extract
from weft.extract.schema import DataTooLarge, Extractor
from weft.fetch.errors import FetchError
from weft.fetch.page import Document, RenderMode, get_document, off_loop
from weft.jobs.crawl import CrawlDeps
from weft.models import ExtractItem, ExtractResult, JobResult, SearchHit, SearchResult
from weft.obs.logging import safe_url
from weft.providers import EmbeddingProvider, LlmProvider, ProviderError
from weft.rank.passages import rank_passages, snippet
from weft.rank.rerank import RERANK_CANDIDATES, rerank

log = logging.getLogger("weft.retrieval")

# One extract call asks the model about at most this many pages, however large the job: each ask
# is a billed call that can take up to provider_timeout_s, and the call waits for it.
MAX_LLM_PAGES = 10


async def _document(
    deps: CrawlDeps, url: str, render: RenderMode, with_html: bool
) -> Document | None:
    try:
        return await get_document(
            deps.fetcher, deps.settings, url, render, deps.browser, deps.cache, with_html=with_html
        )
    except FetchError as exc:
        log.info("page unreadable", extra={"url": safe_url(url), "code": exc.code})
        return None


async def search(
    deps: CrawlDeps,
    embedder: EmbeddingProvider | None,
    query: str,
    limit: int,
    result: JobResult,
) -> SearchResult:
    """The best passages for `query` among the pages `result` lists, with the source of each."""
    render = result.job.params.render
    docs = [d for p in result.pages if (d := await _document(deps, p.url, render, False))]
    corpus = [(d.page.final_url, d.page.title, d.page.markdown) for d in docs]
    hits, passages = await off_loop(deps.settings, lambda: rank_passages(query, corpus))
    reranked = False
    if embedder is not None and hits:
        try:
            # The tool caps `limit` at RERANK_CANDIDATES, so every hit it returns is reranked.
            hits = await rerank(embedder, query, hits[:RERANK_CANDIDATES])
            reranked = True
        except ProviderError as exc:  # the keyword order is still an answer
            log.warning("rerank skipped", extra={"code": exc.code})
    return SearchResult(
        query=query,
        hits=[
            SearchHit(
                url=h.url,
                title=h.title,
                heading=h.heading,
                snippet=snippet(h.text, query),
                passage=h.text,
                score=round(h.score, 6),
            )
            for h in hits[:limit]
        ],
        pages_searched=len(docs),
        pages_unreadable=len(result.pages) - len(docs),
        passages_searched=passages,
        reranked=reranked,
        complete=result.job.status == "done",
        job=result.job,
    )


def _nothing(data: Any) -> bool:
    return data is None or (isinstance(data, (dict, list)) and not data)


class _Extraction:
    """One schema applied to pages, with the model's share of this call counted down."""

    def __init__(self, deps: CrawlDeps, llm: LlmProvider | None, extractor: Extractor) -> None:
        self._deps = deps
        self._llm = llm
        self._ex = extractor
        self._model_schema = extractor.prompt_schema()
        self._llm_left = MAX_LLM_PAGES if llm is not None else 0

    async def page(self, doc: Document) -> ExtractItem:
        """The rules first. The model only when they fall short and a call is left, and its
        answer replaces theirs only if it satisfies the schema."""
        s, ex = self._deps.settings, self._ex
        html, url = doc.html or "", doc.page.final_url
        data: Any = None
        if ex.has_rules:

            def read() -> tuple[Any, list[str]]:
                found = ex.run(html, url, s.max_content_chars)
                return found, ex.errors(found)

            try:
                data, errors = await off_loop(s, read)
            except DataTooLarge as exc:
                errors = [str(exc)]
        else:
            errors = [
                "the schema has no x-css or x-xpath rules, so there is nothing to read by rule"
                + ("" if self._llm is not None else "; model extraction is off")
            ]
        llm = self._llm
        if not errors or llm is None or self._llm_left <= 0:
            return ExtractItem(url=url, data=data, valid=not errors, errors=errors, method="rules")
        if doc.page.status >= 400:  # an error page holds nothing worth a billed call
            skipped = f"llm: skipped, the page answered with status {doc.page.status}"
            return ExtractItem(
                url=url, data=data, valid=False, errors=[*errors, skipped], method="rules"
            )
        self._llm_left -= 1
        answer, problem = await self._ask(llm, doc)
        if problem is not None:
            return ExtractItem(
                url=url, data=data, valid=False, errors=[*errors, problem], method="rules"
            )
        return ExtractItem(url=url, data=answer, valid=True, errors=[], method="llm")

    async def _ask(self, llm: LlmProvider, doc: Document) -> tuple[Any, str | None]:
        """The model's answer and None, or None and why there is no answer."""
        page, ex = doc.page, self._ex
        try:
            answer = await llm_extract(
                llm,
                self._model_schema,
                page.final_url,
                page.markdown,
                self._deps.settings.llm_max_input_chars,
            )
        except ProviderError as exc:
            log.warning("model extraction failed", extra={"code": exc.code})
            return None, f"llm: {exc.code}"
        if await off_loop(self._deps.settings, lambda: ex.errors(answer)):
            return None, "llm: its answer did not satisfy the schema"
        return answer, None


async def extract_url(
    deps: CrawlDeps, llm: LlmProvider | None, extractor: Extractor, url: str
) -> ExtractResult:
    """Extract from one page. A page that cannot be read raises FetchError."""
    doc = await get_document(
        deps.fetcher, deps.settings, url, "auto", deps.browser, deps.cache, with_html=True
    )
    item = await _Extraction(deps, llm, extractor).page(doc)
    return ExtractResult(
        source="url",
        job_id=None,
        job_status=None,
        items=[item],
        pages_total=1,
        pages_checked=1,
        pages_matched=int(item.valid and not _nothing(item.data)),
        pages_unreadable=0,
    )


async def extract_job(
    deps: CrawlDeps,
    llm: LlmProvider | None,
    extractor: Extractor,
    result: JobResult,
    limit: int,
) -> ExtractResult:
    """Extract from a job's pages, most relevant first, until `limit` of them match. Only the
    pages that match are returned: on a site of mixed pages most are not the kind asked for."""
    run = _Extraction(deps, llm, extractor)
    render = result.job.params.render
    items: list[ExtractItem] = []
    checked = unreadable = 0
    for p in result.pages:
        if len(items) >= limit:
            break
        checked += 1
        doc = await _document(deps, p.url, render, True)
        try:
            item = None if doc is None else await run.page(doc)
        except FetchError as exc:  # extraction_timeout
            log.info("page unreadable", extra={"url": safe_url(p.url), "code": exc.code})
            item = None
        if item is None:
            unreadable += 1
        elif item.valid and not _nothing(item.data):
            items.append(item)
    return ExtractResult(
        source="job",
        job_id=result.job.id,
        job_status=result.job.status,
        items=items,
        pages_total=result.pages_total,
        pages_checked=checked,
        pages_matched=len(items),
        pages_unreadable=unreadable,
    )
