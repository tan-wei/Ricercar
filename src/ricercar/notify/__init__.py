"""Notifications, delivered through apprise (email, Discord, Telegram, Slack, …).

The legacy tool sent two messages per run — one when it started, one when it
finished — and that shape is kept here, with the tracker named in the subject
instead of a hardcoded ``[Rutracker]``. Anything apprise understands
(``mailto://``, ``discord://``, ``telegram://``, ``slack://``, ``json://`` …)
works by putting its URL in ``notify.urls``.

No URL configured means notifications are off, not an error. URLs are secrets —
they carry webhook tokens — so they are never logged: only how many there are, and
the scheme of one apprise rejects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

import apprise

from ricercar.config import NotifyConfig, SearchTask, get_settings
from ricercar.log import get_logger


def _stamp(when: datetime | None) -> str:
    """Human-readable moment for a message subject."""
    return (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


LISTED_TASKS = 10
"""How many tasks a start notification lists before it stops counting them out.

One configured task can stand for dozens of searches — one per configured section — and
a run can be thousands of them: an email that enumerates them all is a wall of text
nobody reads.
"""

LISTED_DEAD = 5
"""How many dead-looking tasks a finish notification names.

The interesting ones are few and the list can be hundreds long (the real configuration
has thousands of tasks, many of which have not found anything in years).
"""


def _describe(task: SearchTask) -> str:
    """One line per search task, the way the legacy notification listed them."""
    filters = []
    if task.author:
        filters.append(f"author={task.author}")
    if task.category is not None:
        filters.append(f"category={task.category}")
    text = task.text or "(no text)"
    return f"{text} [{', '.join(filters)}]" if filters else text


def _listing(tasks: Sequence[SearchTask]) -> str:
    """The searches a run will do, trimmed to something worth reading."""
    lines = [f"- {_describe(task)}" for task in tasks[:LISTED_TASKS]]
    if len(tasks) > LISTED_TASKS:
        lines.append(f"- … and {len(tasks) - LISTED_TASKS} more")
    return "\n".join(lines) or "- (no tasks configured)"


class Notifier:
    """Sends run notifications to the configured apprise URLs."""

    def __init__(self, urls: Sequence[str] = ()) -> None:
        self._urls = tuple(url for url in urls if url)
        self._client = apprise.Apprise()
        for url in self._urls:
            if not self._accepts(url):
                get_logger().warning(
                    "Ignoring a notification URL apprise does not accept: {}://… "
                    "(see the apprise README for the URL formats)",
                    url.split("://", 1)[0],
                )

    def _accepts(self, url: str) -> bool:
        """Whether apprise can deliver to *url* — malformed URLs raise rather than return."""
        try:
            return bool(self._client.add(url))
        except Exception:
            return False

    @classmethod
    def from_config(cls, cfg: NotifyConfig | None = None) -> Notifier:
        """Build a notifier from ``notify.urls``."""
        cfg = cfg if cfg is not None else get_settings().notify
        return cls(cfg.urls)

    @property
    def enabled(self) -> bool:
        """Whether at least one URL was accepted."""
        return len(self._client) > 0

    @property
    def targets(self) -> int:
        """How many delivery targets apprise registered (the URLs are secrets).

        Counts what apprise will actually notify, so a URL it rejected is not
        counted — and one URL may expand to several targets.
        """
        return len(self._client)

    async def send(
        self,
        title: str,
        body: str,
        *,
        notify_type: apprise.NotifyType = apprise.NotifyType.INFO,
    ) -> bool:
        """Deliver one notification; ``False`` when it is off or failed.

        apprise talks to remote services synchronously, so the call is moved off
        the event loop: a slow mail server must never stall the browser. Per-service
        timeouts go in the URL, e.g. ``mailto://…?timeout=15``.
        """
        log = get_logger()
        if not self.enabled:
            log.debug("Notifications are off — not sending {!r}", title)
            return False

        delivered = await asyncio.to_thread(self._client.notify, body, title, notify_type)
        if delivered:
            log.info("Notified {} target(s): {}", self.targets, title)
        else:
            log.warning(
                "Notification failed for at least one of {} target(s): {}",
                self.targets,
                title,
            )
        return bool(delivered)

    async def run_started(
        self,
        source: str,
        tasks: Sequence[SearchTask],
        *,
        when: datetime | None = None,
    ) -> bool:
        """Announce a run and what it is about to search for."""
        return await self.send(
            f"[{source}] Going to start {len(tasks)} task(s) at {_stamp(when)}",
            f"Start {len(tasks)} task(s):\n{_listing(tasks)}",
        )

    async def run_finished(
        self,
        source: str,
        *,
        tasks: int,
        downloaded: int,
        stored: int,
        new_authors: int = 0,
        elapsed: float = 0.0,
        interrupted: bool = False,
        stale: Sequence[str] = (),
        stale_days: int = 0,
        when: datetime | None = None,
    ) -> bool:
        """Report what a run did — including what it did not get to.

        *stale* are tasks that have found nothing for *stale_days* day(s) and are worth
        removing from the config (see :mod:`ricercar.history`); they are named at the end,
        because a task list can go stale for months without anyone noticing.
        """
        head = "Interrupted after" if interrupted else "Downloaded"
        authors = f", {new_authors} new uploader(s)" if new_authors else ""
        body = (
            f"{head} {stored} new torrent(s) ({downloaded} downloaded) during {tasks} task(s), "
            f"in {elapsed:.0f}s{authors}."
        )
        if stale:
            shown = ", ".join(stale[:LISTED_DEAD])
            more = f" … and {len(stale) - LISTED_DEAD} more" if len(stale) > LISTED_DEAD else ""
            body += (
                f"\n{len(stale)} task(s) have found nothing for {stale_days} day(s) and can "
                f"probably be removed: {shown}{more}"
            )
        return await self.send(
            f"[{source}] Finish {stored} torrent(s) at {_stamp(when)}",
            body,
            notify_type=apprise.NotifyType.WARNING if interrupted else apprise.NotifyType.SUCCESS,
        )


__all__ = ["Notifier"]
