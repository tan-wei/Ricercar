"""Search: URL generation, result parsing, pagination and the uploader filter.

Pages are always *followed* through the pager instead of being built by hand:
rutracker keeps the search server-side, so a page link carries a ``search_id``
that a hand-made ``start=`` URL does not have.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from playwright.async_api import Page

from ricercar.browser.navigation import NAVIGATION_TIMEOUT, goto
from ricercar.log import get_logger
from ricercar.models import SearchHit, SearchPage
from ricercar.sources.base import SelectorsBrokenError, SiteUnderMaintenanceError
from ricercar.sources.rutracker.diagnose import diagnose
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors

SIZE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:[KMGT]i?B)")
"""Matches a human-readable size such as ``285 MB`` or ``2.41 GB``."""


def parse_results(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> list[SearchHit]:
    """Parse the unique results out of a search-results page."""
    soup = BeautifulSoup(html, "lxml")
    hits: dict[str, SearchHit] = {}

    for link in soup.select(selectors.result_link):
        topic_id = _attr(link, "data-topic_id")
        href = _attr(link, "href")
        if topic_id is None or href is None or topic_id in hits:
            continue

        row = link.find_parent("tr")
        author = ""
        size = ""
        if row is not None:
            # The title link carries the same ``med ts-text`` classes as the uploader
            # link — which is how legacy ended up mixing titles into its "authors" set.
            # Match the cell instead.
            uploader = row.select_one(selectors.result_uploader)
            author = uploader.get_text(strip=True) if uploader else ""
            match = SIZE_RE.search(row.get_text(" ", strip=True))
            size = match.group(0) if match else ""

        hits[topic_id] = SearchHit(
            topic_id=topic_id,
            title=link.get_text(strip=True),
            url=selectors.absolute(href),
            author=author,
            size=size,
        )

    return list(hits.values())


def uploaders(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> set[str]:
    """Return the uploaders present on a results page."""
    soup = BeautifulSoup(html, "lxml")
    return {cell.get_text(strip=True) for cell in soup.select(selectors.result_uploader)}


def page_count(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> tuple[int, int] | None:
    """Return ``(this page, how many pages)`` as the pager states them.

    ``None`` when the page does not carry the line at all — a results page with no
    hits has no pager, and it is better to say "unknown" than to invent a number.
    """
    text = BeautifulSoup(html, "lxml").get_text(" ")
    match = re.search(selectors.results_pages, text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def next_page_url(
    html: str,
    current_url: str,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> str | None:
    """Return the URL of the page after *current_url*, if the pager offers one.

    The pager only shows a window of page numbers, so the next page is found by
    taking the smallest ``start`` offset greater than the current one.
    """
    current_start = _start_of(current_url)
    candidates: list[tuple[int, str]] = []

    for anchor in BeautifulSoup(html, "lxml").select(selectors.pager_link):
        href = _attr(anchor, "href")
        if href is None:
            continue
        url = selectors.absolute(href)
        start = _start_of(url)
        if start > current_start:
            candidates.append((start, url))

    return min(candidates)[1] if candidates else None


def _start_of(url: str) -> int:
    """Return the ``start`` offset of a results URL (0 when absent)."""
    raw = parse_qs(urlparse(url).query).get("start", ["0"])[0]
    return int(raw) if raw.isdigit() else 0


def _attr(element: Tag, name: str) -> str | None:
    """Return an attribute as ``str`` when present (bs4 may hand back a list)."""
    value = element.get(name)
    return value if isinstance(value, str) else None


# ── Flow ──────────────────────────────────────────────────────────────────


async def wait_for_results(
    page: Page,
    selectors: Selectors = DEFAULT_SELECTORS,
    timeout: float = NAVIGATION_TIMEOUT,
) -> None:
    """Wait until a results page has rendered — even when it has no hits.

    The header count proves the response really is a results page, and asking for
    rows whenever that count is non-zero keeps a half-rendered document from
    passing as an empty result set.

    Only proves that *a* results page is on screen; it cannot tell a fresh one
    from the page already in front of you. Callers that submit a form must
    therefore await the resulting navigation themselves.
    """
    await page.wait_for_function(
        """({ count, rows }) => {
            const body = document.body;
            if (!body) return false;
            const found = new RegExp(count).exec(body.innerText);
            if (!found) return false;
            return Number(found[1]) === 0 || document.querySelectorAll(rows).length > 0;
        }""",
        arg={"count": selectors.results_count, "rows": selectors.result_link},
        timeout=timeout,
    )


async def start_search(
    page: Page,
    text: str = "",
    category: int | None = None,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> None:
    """Open the search-results page for *text* / *category*.

    Raises:
        SelectorsBrokenError: the page that came back is not a results page — a
            challenge, a login form, or a layout the selectors no longer match.
            Raised *before* waiting, so a redesign fails in a second with an
            explanation instead of after a timeout with none.
    """
    url = selectors.search_url(text, category)
    get_logger().info("Searching: {}", url)
    await goto(page, url)
    _raise_if_unusable(await page.content(), selectors)
    await wait_for_results(page, selectors)


def _raise_if_unusable(html: str, selectors: Selectors) -> None:
    """Refuse a page the source cannot make sense of, with the reasons attached."""
    issues = diagnose(html, selectors)
    if issues:
        # Maintenance issues are transient — retry with exponential back-off.
        if any("maintenance" in issue for issue in issues):
            raise SiteUnderMaintenanceError("; ".join(issues))
        raise SelectorsBrokenError(issues)


async def iter_result_pages(
    page: Page,
    *,
    max_pages: int,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> AsyncIterator[SearchPage]:
    """Yield the current results page and its successors, up to *max_pages*.

    Call :func:`start_search` (and optionally :func:`apply_author_filter`) first.
    The page must stay on the results while iterating — reading only, so a caller
    can drive downloads in a second tab.

    Every yielded page carries the pager's own numbering — "Страница 1 из 10" for a broad
    search — which is a fact about the search, unlike ``max_pages``. The walk ends where
    that numbering says it ends rather than trusting a "next" link to be absent: the
    pager's count is the authority, and a link that points past it is not followed.
    """
    log = get_logger()
    page_index = 0
    total: int | None = None
    url: str | None = page.url

    while url is not None and page_index < max_pages:
        page_index += 1
        html = await page.content()
        hits = parse_results(html, selectors)
        counted = page_count(html, selectors)
        if counted is not None:
            total = counted[1]
            if counted[0] != page_index:
                # The site counts pages itself; a disagreement means the walk is not
                # where it thinks it is, and the next link may belong to another page.
                log.warning("The pager says page {} of {}, we are on page {}", *counted, page_index)
        log.info(
            "{} result(s) on page {}{}",
            len(hits),
            page_index,
            f" of {total}" if total else "",
        )
        yield SearchPage(hits=hits, number=page_index, total=total)

        if page_index >= max_pages:
            log.info("Stopping at max_pages ({})", max_pages)
            return
        if total is not None and page_index >= total:
            log.info("That was the last page ({})", total)
            return
        url = next_page_url(html, page.url, selectors)
        if url is None:
            log.info("No further pages")
            return
        log.info("Next page: {}", url)
        await goto(page, url)
        _raise_if_unusable(await page.content(), selectors)
        await wait_for_results(page, selectors)


async def apply_author_filter(
    page: Page,
    author: str,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> None:
    """Restrict the current results to one uploader.

    The filter form is POSTed, so it has to be driven on the results page itself
    rather than expressed as a URL.
    """
    log = get_logger()
    log.info("Filtering by author '{}'", author)

    await page.fill(selectors.author_filter_field, author)
    # The filter form POSTs and reloads the results, so wait for that navigation:
    # `wait_for_results` cannot help here, since the page in front of us already
    # satisfies it.
    async with page.expect_navigation(wait_until="domcontentloaded"):
        await page.click(selectors.author_filter_submit)
    await wait_for_results(page, selectors)

    unexpected = uploaders(await page.content(), selectors) - {author}
    if unexpected:
        # Not fatal: callers can still filter on SearchHit.author themselves.
        log.warning(
            "Filter by '{}' left other uploaders in the results: {}",
            author,
            sorted(unexpected)[:5],
        )


__all__ = [
    "SIZE_RE",
    "apply_author_filter",
    "iter_result_pages",
    "next_page_url",
    "page_count",
    "parse_results",
    "start_search",
    "uploaders",
    "wait_for_results",
]
