"""What the site looks like when it is not what we expected.

A page can fail in ways a stack trace cannot explain: Cloudflare decides this client
is a robot, the session quietly expired, or the layout changed and no row matches
``a[data-topic_id]`` any more. These functions read the HTML and say so in words,
which is what gets saved next to the page when a failure is captured (see
:mod:`ricercar.diagnostics`).

Pure functions: HTML in, problems out — no browser, no I/O, so a saved fixture is
enough to check what a diagnosis would have said.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors

CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl-",
    "checking your browser",
    "attention required",
)
"""Text that only appears on an interstitial rather than on a forum page."""

MAINTENANCE_MARKERS = (
    "maintenance",
    "under maintenance",
    "temporarily unavailable",
    "the site is currently unavailable",
    "site is under maintenance",
)
"""Text suggesting the tracker is undergoing maintenance."""


def diagnose_results(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> tuple[str, ...]:
    """Problems that stop a search-results page from being usable."""
    soup = BeautifulSoup(html, "lxml")
    issues = list(_page_level(soup, html, selectors))

    header = re.search(selectors.results_count, soup.get_text(" ", strip=True))
    if header is None:
        if not issues:
            issues.append(
                f"the results header matching {selectors.results_count!r} is missing — "
                f"the search did not return a results page"
            )
        return tuple(issues)

    announced = int(header.group(1))
    rows = len(soup.select(selectors.result_link))
    if announced and not rows:
        issues.append(
            f"the header announces {announced} result(s) but no row matches "
            f"{selectors.result_link!r}"
        )
    return tuple(issues)


def diagnose_topic(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> tuple[str, ...]:
    """Problems that stop a topic page from being usable."""
    soup = BeautifulSoup(html, "lxml")
    issues = list(_page_level(soup, html, selectors))

    if soup.select_one(selectors.download_link) is not None:
        return tuple(issues)

    # rutracker withholds the download link for closed topics, so a topic page
    # without one is normal — unless it is not a topic page at all.
    if _looks_like_topic(html):
        return tuple(issues)

    issues.append(
        f"the page is not a topic page: no {selectors.download_link!r} and no topic markers"
    )
    return tuple(issues)


def diagnose(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> tuple[str, ...]:
    """Problems with *html*, whichever kind of page it was meant to be."""
    if re.search(selectors.results_count, BeautifulSoup(html, "lxml").get_text(" ", strip=True)):
        return diagnose_results(html, selectors)
    if _looks_like_topic(html):
        return diagnose_topic(html, selectors)
    return diagnose_results(html, selectors)


# ── Internals ─────────────────────────────────────────────────────────────


def _page_level(
    soup: BeautifulSoup,
    html: str,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> tuple[str, ...]:
    """Problems that make the page unusable whatever it was supposed to be."""
    text = soup.get_text(" ", strip=True).lower()
    issues: list[str] = []

    for marker in CHALLENGE_MARKERS:
        if marker in text or marker in html.lower():
            issues.append(
                "the page is a Cloudflare challenge, not the tracker — solve it in your "
                "browser, then retry"
            )
            break

    if not issues:
        for marker in MAINTENANCE_MARKERS:
            if marker in text:
                issues.append(
                    f"the tracker appears to be under maintenance (text contains "
                    f"{marker!r}) — will retry with a longer back-off"
                )
                break

    if soup.select_one(selectors.login_username) is not None:
        issues.append("the page is the login form, so the session is gone")

    return tuple(issues)


def _looks_like_topic(html: str) -> bool:
    """Whether the document carries the markers only a topic page has."""
    lowered = html.lower()
    return "viewtopic" in lowered or "скачать .torrent" in lowered


__all__ = [
    "CHALLENGE_MARKERS",
    "MAINTENANCE_MARKERS",
    "diagnose",
    "diagnose_results",
    "diagnose_topic",
]
