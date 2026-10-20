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
import re
from collections.abc import Sequence
from datetime import datetime

import apprise

from ricercar.categories import CategoryDiff
from ricercar.config import NotifyConfig, SearchTask, get_settings
from ricercar.history import TaskYield
from ricercar.log import get_logger


def _stamp(when: datetime | None) -> str:
    """Human-readable moment for a message subject."""
    return (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


LISTED_TASKS = 9999
"""How many tasks a start notification lists before it stops counting them out.

One configured task can stand for dozens of searches — one per configured section — and
a run can be thousands of them: an email that enumerates them all is a wall of text
nobody reads.
"""

LISTED_DEAD = 9999
"""How many dead-looking tasks a finish notification names.

The interesting ones are few and the list can be hundreds long (the real configuration
has thousands of tasks, many of which have not found anything in years).
"""

LISTED_TORRENTS = 9999
"""How many torrent names a per-task listing shows before truncating.

A task can yield dozens of new torrents in one run — enough names to be informative,
but stopping well short of a wall of text.
"""


def _category_name(task: SearchTask, source: str) -> str:
    """The human-readable section name for a task's category, or the id as a fallback."""
    if task.category is None or not source:
        return str(task.category) if task.category is not None else ""
    cfg = get_settings().sources.get(source)
    if cfg is None:
        return str(task.category)
    # categories is dict[str, int]; reverse lookup.
    for name, cat_id in cfg.categories.items():
        if cat_id == task.category:
            return name
    return str(task.category)


def _describe(task: SearchTask, source: str = "") -> str:
    """One line per search task, the way the legacy notification listed them."""
    filters = []
    if task.author:
        filters.append(f"author={task.author}")
    if task.category is not None:
        cat = _category_name(task, source)
        filters.append(f"category={cat}")
    text = task.text or "(no text)"
    return f"{text} [{', '.join(filters)}]" if filters else text


def _listing(tasks: Sequence[SearchTask], source: str = "") -> str:
    """The searches a run will do, trimmed to something worth reading."""
    lines = [f"- {_describe(task, source)}" for task in tasks[:LISTED_TASKS]]
    if len(tasks) > LISTED_TASKS:
        lines.append(f"- … and {len(tasks) - LISTED_TASKS} more")
    return "\n".join(lines) or "- (no tasks configured)"


def _display_label(label: str, source: str) -> str:
    return re.sub(
        r"\bin category (\d+)\b",
        lambda match: (
            "in category " + _category_name(SearchTask(category=int(match.group(1))), source)
        ),
        label,
    )


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

        Messages are formatted as Markdown — apprise converts it to HTML for email
        and renders it natively for Discord, Slack, Telegram, etc.
        """
        log = get_logger()
        if not self.enabled:
            log.debug("Notifications are off — not sending {!r}", title)
            return False

        delivered = await asyncio.to_thread(
            self._client.notify,
            body,
            title,
            notify_type,
            body_format=apprise.NotifyFormat.MARKDOWN,
        )
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
            f"## {source}\n\n**{len(tasks)} task(s)**\n\n{_listing(tasks, source)}",
        )

    async def run_finished(
        self,
        source: str,
        *,
        tasks: int,
        downloaded: int,
        stored: int,
        new_authors: Sequence[str] = (),
        budget: int = 0,
        category_diff: CategoryDiff | None = None,
        elapsed: float = 0.0,
        interrupted: bool = False,
        failures: int = 0,
        failure_details: Sequence[str] = (),
        stale: Sequence[str] = (),
        stale_days: int = 0,
        task_results: dict[str, list[str]] | None = None,
        task_history: dict[str, TaskYield] | None = None,
        when: datetime | None = None,
    ) -> bool:
        """Report what a run did — including what it did not get to.

        *stale* are tasks that have found nothing for *stale_days* day(s) and are worth
        removing from the config (see :mod:`ricercar.history`); they are named at the end,
        because a task list can go stale for months without anyone noticing.

        *task_results* maps a task label to the names of torrents it downloaded.  The
        names are the ``TorrentMetadata.name`` values — the ``info.name`` from each
        ``.torrent`` file, **not** the topic title.  The first few are listed per task
        and a summary of how many tasks did / did not find anything is shown.

        *task_history* maps a task label to its long-term :class:`TaskYield` stats.
        Only tasks that **did not** download anything this run are shown here, along
        with their time since last hit — so you can spot tasks that have gone quiet.

        *category_diff* describes how the live site's forum-section list diverges from
        the bundled snapshot.  When present the pipeline skipped the search and reports
        the difference here.  Run ``ricercar categories check`` for a full table, or
        ``ricercar categories refresh`` to update the snapshot after manually adjusting
        the config.
        """
        head = "Interrupted after" if interrupted else "Downloaded"
        lines: list[str] = []

        if failures or failure_details:
            lines.append("## Run Issues\n")
            status = f"{failures} failure(s)." if failures else "A preliminary check failed."
            lines.append(
                f"**{status} Results may be incomplete; "
                "zero downloads does not mean no new torrents were available.**\n"
            )
            lines.extend(f"- {_display_label(detail, source)}" for detail in failure_details)
            lines.append("\n---\n")

        # ── 1. Category changes banner (top of message, highlighted) ────
        if category_diff and category_diff.has_changes:
            lines.append("## ⚠️ Forum Section Structure Changed\n")
            lines.append(
                "The live site's category list differs from the bundled snapshot. "
                "The pipeline skipped the search so you can review the changes before "
                "continuing.\n"
            )
            lines.append("| Change | ID | Name |")
            lines.append("|--------|----|------|")
            for fid, name in sorted(category_diff.added.items()):
                lines.append(f"| Added | {fid} | {name} |")
            for fid, name in sorted(category_diff.removed.items()):
                lines.append(f"| Removed | {fid} | {name} |")
            for fid, (old, new) in sorted(category_diff.changed.items()):
                lines.append(f"| Renamed | {fid} | '{old}' → '{new}' |")
            lines.append("")
            lines.append(
                "Run ``ricercar categories check`` for details, or "
                "``ricercar categories refresh`` to update the snapshot after reviewing.\n"
            )
            lines.append("---\n")

        # ── 2. New uploaders banner ─────────────────────────────────────
        if new_authors:
            shown = list(new_authors[:20])
            rest = len(new_authors) - 20
            more = f" … and {rest} more" if rest > 0 else ""
            lines.append("## 👤 New Uploaders\n")
            for author in shown:
                lines.append(f"- ``{author}``")
            if rest > 0:
                lines.append(f"- ... and {rest} more uploader(s) omitted")
            lines.append("")
            lines.append(
                "Run ``ricercar tasks --add-discovered`` to turn them into tasks, "
                "or ``ricercar tasks --discovered`` to see the full list.\n"
            )
            lines.append("---\n")

        # ── 3. Source summary ───────────────────────────────────────────
        lines.append(f"## {source}")
        lines.append("")
        quota = f" / {budget}" if budget else ""
        lines.append(
            f"**{head} {stored} torrent(s){quota} ({downloaded} downloaded) "
            f"during {tasks} task(s), in {elapsed:.0f}s.**"
        )

        # ── 4. Per-task download list ───────────────────────────────────
        if task_results:
            _tasks_with = 0
            _tasks_without = 0
            task_sections = []
            for label, names in sorted(task_results.items(), key=lambda kv: -len(kv[1])):
                if not names:
                    _tasks_without += 1
                    continue
                _tasks_with += 1
                shown = names[:LISTED_TORRENTS]
                rest = len(names) - LISTED_TORRENTS
                more = f"\n*… and {rest} more*" if rest > 0 else ""
                task_sections.append(
                    f"\n### {_display_label(label, source)} ({len(names)} torrent(s))\n"
                )
                task_sections.append("\n".join(f"- {n}" for n in shown) + more + "\n")
            summary_parts = []
            if _tasks_with:
                summary_parts.append(f"{_tasks_with} task(s) found new torrents")
            if _tasks_without:
                summary_parts.append(f"{_tasks_without} task(s) yielded nothing")
            if summary_parts:
                lines.append("")
                lines.append("**" + ", ".join(summary_parts) + ".**")
            lines.extend(task_sections)
            lines.append("")

        # ── 5. Tasks with no download this run (show last_hit age) ──────
        if task_history and task_results:
            quiet: list[tuple[str, TaskYield]] = []
            for label, ys in sorted(task_history.items()):
                if label not in task_results or not task_results[label]:
                    quiet.append((label, ys))
            if quiet:
                lines.append("### ⏳ Tasks With No Download This Run\n")
                lines.append("| Task | Last Hit |")
                lines.append("|------|----------|")
                for label, ys in quiet:
                    last = f"{ys.days_without_hit() or 0}d ago" if ys.last_hit else "never"
                    lines.append(f"| {_display_label(label, source)} | {last} |")
                lines.append("")

        # ── 6. Stale tasks ──────────────────────────────────────────────
        if stale:
            stale_shown = ", ".join(_display_label(label, source) for label in stale[:LISTED_DEAD])
            stale_rest = len(stale) - LISTED_DEAD
            stale_more = f" … and {stale_rest} more" if stale_rest > 0 else ""
            lines.append("")
            lines.append(
                f"{len(stale)} task(s) have found nothing for {stale_days} day(s) and can "
                f"probably be removed: {stale_shown}{stale_more}"
            )

        return await self.send(
            f"[{source}] Finish {stored} torrent(s) at {_stamp(when)}",
            "\n".join(lines),
            notify_type=(
                apprise.NotifyType.WARNING
                if interrupted or failures or failure_details
                else apprise.NotifyType.SUCCESS
            ),
        )


__all__ = ["Notifier"]
