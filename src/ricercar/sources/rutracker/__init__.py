"""rutracker.org — the tracker this project was written for.

The site is behind Cloudflare, so the pages can only be reached through a browser
you launched yourself (see ``ricercar.browser`` for the measurements and
``README.md`` for the bootstrap). Everything else about the site — URLs,
selectors, literals — is in :mod:`ricercar.sources.rutracker.selectors`, and the
flow itself in :mod:`ricercar.sources.rutracker.search`, ``topic`` and
``session``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from playwright.async_api import Page

from ricercar.browser import MANUAL_LOGIN_TIMEOUT
from ricercar.config import SearchTask, SourceSettings
from ricercar.models import SearchHit, SearchPage, TopicInfo
from ricercar.sources.rutracker.diagnose import diagnose, diagnose_results, diagnose_topic
from ricercar.sources.rutracker.search import (
    apply_author_filter,
    iter_result_pages,
    next_page_url,
    page_count,
    parse_results,
    start_search,
    uploaders,
    wait_for_results,
)
from ricercar.sources.rutracker.selectors import (
    DEFAULT_SELECTORS,
    Selectors,
    load_selectors,
)
from ricercar.sources.rutracker.session import (
    LoginOutcome,
    ensure_logged_in,
    is_logged_in,
    log_in,
    login_page_state,
)
from ricercar.sources.rutracker.topic import (
    DOWNLOAD_TIMEOUT,
    fetch_torrent,
    open_topic,
    parse_topic,
)


class RutrackerSource:
    """The rutracker.org adapter."""

    name: ClassVar[str] = "rutracker"
    """Registry key, and the config section this tracker reads."""

    host: ClassVar[str] = "rutracker.org"
    """The site's hostname; stored torrents are attributed to this source by it."""

    def __init__(self, settings: SourceSettings) -> None:
        self.settings = settings
        self.selectors: Selectors = load_selectors(settings)

    async def ensure_logged_in(self, page: Page, *, timeout: float = MANUAL_LOGIN_TIMEOUT) -> None:
        await ensure_logged_in(
            page,
            self.settings.login.username,
            self.settings.login.password,
            timeout=timeout,
            selectors=self.selectors,
        )

    async def search(
        self,
        page: Page,
        task: SearchTask,
        *,
        max_pages: int,
    ) -> AsyncIterator[SearchPage]:
        if not isinstance(task.category, int):
            # expand() resolves "every section" into concrete searches and the runner
            # always expands before it drives a source, so this is a programming error:
            # there is no URL for "no section in particular" any more.
            msg = f"the task {task!r} was not expanded before searching"
            raise ValueError(msg)
        await start_search(page, task.text, task.category, selectors=self.selectors)
        if task.author:
            # The uploader filter is a POSTed form on the results page, so it has to
            # be applied before the pager starts moving.
            await apply_author_filter(page, task.author, selectors=self.selectors)

        async for results in iter_result_pages(page, max_pages=max_pages, selectors=self.selectors):
            yield results

    async def fetch_torrent(
        self,
        page: Page,
        hit: SearchHit,
        out_dir: Path,
    ) -> tuple[TopicInfo, Path]:
        return await fetch_torrent(page, hit.url, out_dir, selectors=self.selectors)

    def diagnose(self, html: str) -> tuple[str, ...]:
        return diagnose(html, self.selectors)

    def expand(self, task: SearchTask) -> list[SearchTask]:
        """One search per configured section when the task does not name one.

        That is the legacy behaviour — it walked its section list for every task — and
        it is what makes an uploader-only task mean "in the sections I care about"
        rather than "everywhere on the tracker".
        """
        if isinstance(task.category, int):
            return [task]

        sections = list(self.settings.categories.items())
        if not sections:
            # Config validation refuses this, so reaching it means the settings were
            # built by hand — say so instead of silently searching nothing.
            msg = (
                "no sections configured, so 'every category' has no meaning — add "
                "`categories:` to this source"
            )
            raise ValueError(msg)

        return [task.model_copy(update={"category": section}) for _, section in sections]

    def fixture_routes(self) -> dict[str, str]:
        """Where the offline mode finds its pages and its torrent, under the fixture root."""
        return {
            "**/tracker.php*": "html/search/results.html",
            "**/viewtopic.php*": "html/topic/topic.html",
            "**/dl.php*": "torrents/sample.torrent",
        }


__all__ = [
    "DEFAULT_SELECTORS",
    "DOWNLOAD_TIMEOUT",
    "LoginOutcome",
    "RutrackerSource",
    "Selectors",
    "apply_author_filter",
    "diagnose",
    "diagnose_results",
    "diagnose_topic",
    "ensure_logged_in",
    "fetch_torrent",
    "is_logged_in",
    "iter_result_pages",
    "load_selectors",
    "log_in",
    "login_page_state",
    "next_page_url",
    "open_topic",
    "page_count",
    "parse_results",
    "parse_topic",
    "start_search",
    "uploaders",
    "wait_for_results",
]
