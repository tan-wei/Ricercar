"""The contract a tracker has to fulfil to be monitored.

Adding a tracker means writing one class that satisfies :class:`Source` and
registering it in :mod:`ricercar.sources`; nothing else in the project changes.
The things that actually differ between trackers are the members below — how a
session is kept, how a search is expressed, and how a torrent is taken from a
topic — while parsing, storage, deduplication, notifications and scheduling are
shared and site-agnostic.

The protocol is deliberately about *behaviour*, not about selectors: a tracker
whose search is a JSON API and whose downloads are plain links fits it as well as
one that is all HTML forms, without pretending both can be described by the same
CSS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from playwright.async_api import Page

from ricercar.browser import MANUAL_LOGIN_TIMEOUT
from ricercar.config import SearchTask, SourceSettings
from ricercar.models import SearchHit, SearchPage, TopicInfo


class UnknownSourceError(LookupError):
    """No source is known under that name, or it has no configuration section."""


class SessionExpiredError(RuntimeError):
    """The tracker's session is gone.

    Not a failure to retry: the caller has to log in again (see
    :meth:`Source.ensure_logged_in`) and only then repeat the work.
    """


class TorrentUnavailableError(RuntimeError):
    """The topic exists but its torrent cannot be fetched.

    Also not retried: the tracker is saying no.
    """


class SelectorsBrokenError(RuntimeError):
    """The page does not match the source's selectors any more.

    Raised before the page is parsed, so a site redesign fails with an explanation
    — and, in the pipeline, with the page saved — instead of a confusing empty
    result set.
    """

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues) or "the page does not look like one we know")


class SiteUnderMaintenanceError(RuntimeError):
    """The tracker responded but is not usable — maintenance, Cloudflare, etc.

    A transient failure that should be retried with exponential back-off after a delay,
    rather than being reported as a permanent failure.  The caller gives up after the
    configured number of attempts.
    """


@runtime_checkable
class Source(Protocol):
    """One monitorable tracker."""

    name: ClassVar[str]
    """Registry key, and the section name under ``sources:`` in the config."""

    host: ClassVar[str]
    """The site's hostname. Stored torrents are attributed to a source by their URL,
    which carries the host, and that is what the per-source daily quota counts."""

    settings: SourceSettings
    """This tracker's slice of the configuration — including which browser to
    drive (``connect_url``). How a browser is obtained is not a per-source choice:
    every source is driven through a browser the user launched (see
    :mod:`ricercar.browser`)."""

    def diagnose(self, html: str) -> tuple[str, ...]:
        """What is wrong with *html*, as far as this source can tell.

        Empty means "usable". A non-empty result is the explanation that goes with
        a saved failure, so it should name what was expected and what was found —
        a challenge page, a login form, a results header without its table. Pure
        (HTML in, strings out), which also makes it testable without a browser.
        """
        ...

    def fixture_routes(self) -> Mapping[str, str]:
        """URL pattern → fixture path, for the offline mode (see :mod:`ricercar.testing`).

        Paths are relative to the fixture root given to the offline mode.
        """
        ...

    def expand(self, task: SearchTask) -> list[SearchTask]:
        """The concrete searches *task* stands for, in the order to run them.

        A tracker's search vocabulary can be coarser than its search requests: one
        configured line saying "every section I listed" is one search per section on
        the wire. Expanding here rather than looping inside :meth:`search` keeps the
        run's bookkeeping exact — the progress bars, the resume state and the per-source
        quota all count what actually runs, down to the section.

        Returning ``[task]`` unchanged is the right answer for a tracker whose tasks
        map one-to-one onto searches.
        """
        ...

    async def ensure_logged_in(self, page: Page, *, timeout: float = MANUAL_LOGIN_TIMEOUT) -> None:
        """Leave *page* holding a session usable for this tracker.

        Called once per run, before searching. A session that cannot be created
        automatically (because the site blocks it) is a normal event: wait for a
        human instead of failing.

        Raises:
            SessionExpiredError: no usable session appeared within *timeout*.
        """
        ...

    def search(
        self,
        page: Page,
        task: SearchTask,
        *,
        max_pages: int,
    ) -> AsyncIterator[SearchPage]:
        """Yield the result pages of *task*, one page of hits at a time.

        An async generator, so that a caller can stop early and so that the whole
        dance a site requires — opening the search, applying filters, walking the
        pager — stays inside the source. Reading only: downloads belong to
        :meth:`fetch_torrent`, which the caller drives on a page of its own.

        *max_pages* is the ceiling on how far to follow the pager; a source that knows
        from the page how many there really are reports that on every
        :class:`~ricercar.models.SearchPage`, which is what the caller shows and stops by.
        """
        ...

    async def fetch_torrent(
        self,
        page: Page,
        hit: SearchHit,
        out_dir: Path,
    ) -> tuple[TopicInfo, Path]:
        """Download the ``.torrent`` behind *hit* into *out_dir*.

        Returns where the file landed, so the caller can parse and store it.

        Raises:
            TorrentUnavailableError: the topic does not offer its torrent.
            SelectorsBrokenError: the topic page does not look like a topic page.
        """
        ...


SourceFactory = Callable[[SourceSettings], Source]
"""Builds a source from its configuration section."""


__all__ = [
    "SelectorsBrokenError",
    "SessionExpiredError",
    "Source",
    "SourceFactory",
    "TorrentUnavailableError",
    "UnknownSourceError",
]
