"""One run: every configured source, its tasks, and what came out of them.

The shape follows the legacy tool — take the mandatory tasks plus a random sample,
search each one, skip whatever the database already knows, download what is new, and
stop at the daily quota — with three things done properly:

* the work goes through the :class:`~ricercar.sources.base.Source` protocol, so
  nothing here knows which tracker it is talking to;
* failures are graded rather than retried blindly (see :mod:`ricercar.retry`): a
  timeout is retried, an expired session is logged in again, and a page whose
  selectors no longer match is **saved** (page, screenshot, trace — see
  :mod:`ricercar.diagnostics`) and the task is skipped;
* an interrupt (Ctrl-C, SIGTERM) finishes the torrent in hand, records the state,
  says so in the notification, and stops — the next run resumes where this one
  stopped (see :mod:`ricercar.state`).
"""

from __future__ import annotations

import asyncio
import math
import random
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import FrameType
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from rich.progress import Progress, TaskID

from ricercar.browser import BrowserSession, managed_browser
from ricercar.config import SearchTask, Settings, get_settings
from ricercar.diagnostics import Recorder
from ricercar.history import TaskHistory, TaskYield, configured_task_keys
from ricercar.log import get_logger
from ricercar.models import SearchHit
from ricercar.notify import Notifier
from ricercar.parser import TorrentError, parse_file
from ricercar.progress import new_progress
from ricercar.repository import TorrentRepository
from ricercar.retry import Policies, call
from ricercar.sources import Source, enabled_sources
from ricercar.sources.base import (
    SelectorsBrokenError,
    SessionExpiredError,
    TorrentUnavailableError,
)
from ricercar.state import StateStore, task_key
from ricercar.testing import FixtureRouter

MAX_TASK_FAILURES = 2
"""How many tasks may fail in a row before the rest of a source is skipped.

One broken page is a bad page; two in a row mean the site changed, and continuing
would only produce more copies of the same evidence.
"""

LISTED_STALE = 5
"""How many dead-looking tasks the end-of-run warning names before it says "… and N more".

The list is 5,969 tasks long in the real configuration, so a warning that named all of
them would be a wall of text; ``ricercar tasks --stale`` prints every one of them.
"""

PAUSE_TICK = 0.5
"""Seconds between checks while waiting out ``quota.page_delay_seconds``.

Short enough that an interrupt is noticed at once, long enough that the countdown is not
redrawn thousands of times.
"""


async def wait_between_pages(
    delay: float,
    *,
    stop: Callable[[], bool],
    on_tick: Callable[[float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> float:
    """Wait out *delay* seconds, unless *stop* asks to give up first.

    Returns the seconds actually waited. *on_tick* is handed the time left on every tick,
    which is what puts a countdown on the progress line: a minute of silence looks like a
    hang, and waiting out a minute of Ctrl-C is worse than the thing being guarded
    against.

    ``sleep`` and ``now`` are injectable so the wait can be tested without waiting (the
    same shape :func:`ricercar.retry.call` uses for its backoff).
    """
    started = now()
    deadline = started + delay
    while (remaining := deadline - now()) > 0:
        if stop():
            break
        if on_tick is not None:
            on_tick(remaining)
        await sleep(min(remaining, PAUSE_TICK))
    return now() - started


def describe_task(task: SearchTask) -> str:
    """A search task in one line, for logs and progress bars."""
    parts = [task.text or "(everything)"]
    if task.author:
        parts.append(f"by {task.author}")
    if task.category is not None:
        parts.append(f"in category {task.category}")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class PlannedSearch:
    """One search to run, and the configured task it came from.

    A configured task can stand for many searches (see :meth:`Source.expand`), and the
    history is kept per *configured* task — that is the thing you would delete from the
    config — while progress, resume and the quota deal in searches. So each search keeps
    both: itself, and the task that asked for it.
    """

    task: SearchTask
    origin: str
    """Task key of the configured task."""
    label: str
    """That task in one line, for the history and the report."""

    @property
    def key(self) -> str:
        """Task key of the search itself, as the resume state knows it."""
        return task_key(self.task)


@dataclass(slots=True)
class _TaskRun:
    """What one configured task contributed to the run in progress."""

    label: str
    searches: int = 0
    hits: int = 0
    stored: int = 0


class Shutdown:
    """A flag the signal handlers set, so a run can stop at the next safe point."""

    def __init__(self) -> None:
        self.requested = False
        self.reason = ""
        self._previous: dict[int, Any] = {}

    def request(self, reason: str) -> None:
        """Ask the run to stop (also the way a test or the scheduler asks)."""
        if not self.requested:
            self.reason = reason
        self.requested = True

    def install(self) -> None:
        """Catch SIGINT/SIGTERM for the duration of a run.

        Signals can only be handled from the main thread (the scheduler runs a run in
        a worker), and whatever was installed before — apscheduler's own handlers, for
        instance — is put back by :meth:`restore`.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError, AttributeError):
                continue

    def restore(self) -> None:
        """Put the previous handlers back."""
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous.clear()

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        try:
            self.request(signal.Signals(signum).name)
        except ValueError:  # pragma: no cover - a signal without a name
            self.request(str(signum))


@dataclass(slots=True)
class SourceOutcome:
    """What one source contributed to a run."""

    source: str
    tasks: int = 0
    done: int = 0
    hits: int = 0
    """Result rows seen."""
    known: int = 0
    """Rows the database already had (URL) or content it already had (MD5)."""
    downloaded: int = 0
    """Torrents actually fetched."""
    stored: int = 0
    """New rows written."""
    new_authors: int = 0
    failures: int = 0
    stale: int = 0
    """Tasks that have found nothing for long enough to be worth removing."""
    elapsed: float = 0.0
    skipped: str = ""
    """Why the source did nothing: a used-up quota, no tasks, or a failure."""
    aborted: bool = False
    """Whether the source could not be driven at all (no browser, no session)."""

    def describe(self) -> str:
        if self.skipped:
            return f"{self.source}: skipped — {self.skipped}"
        dead = f", {self.stale} task(s) look dead" if self.stale else ""
        return (
            f"{self.source}: {self.done}/{self.tasks} task(s), {self.stored} stored "
            f"({self.downloaded} downloaded, {self.known} already known, {self.hits} seen), "
            f"{self.new_authors} new uploader(s), {self.failures} failure(s){dead}, "
            f"{self.elapsed:.0f}s"
        )


@dataclass(slots=True)
class RunOutcome:
    """What a whole run did."""

    sources: list[SourceOutcome] = field(default_factory=list)
    interrupted: bool = False
    elapsed: float = 0.0

    @property
    def stored(self) -> int:
        return sum(outcome.stored for outcome in self.sources)

    @property
    def failures(self) -> int:
        return sum(outcome.failures for outcome in self.sources)

    @property
    def stale(self) -> int:
        """How many tasks look dead across every source (see :mod:`ricercar.history`)."""
        return sum(outcome.stale for outcome in self.sources)

    @property
    def failed(self) -> bool:
        """Whether a source could not be driven at all (as opposed to having nothing to do)."""
        return any(outcome.aborted for outcome in self.sources)

    def describe(self) -> str:
        head = "interrupted, " if self.interrupted else ""
        return (
            f"{head}{self.stored} new torrent(s) in {self.elapsed:.0f}s "
            f"across {len(self.sources)} source(s), {self.failures} failure(s)"
        )


class Runner:
    """Runs the configured sources once.

    Everything it needs is injectable, so a run can be driven offline, limited to a
    few torrents, or against a fake source without touching the configuration.
    """

    def __init__(
        self,
        cfg: Settings | None = None,
        *,
        sources: Sequence[Source] | None = None,
        offline: Path | None = None,
        limit: int | None = None,
        resume: bool | None = None,
        shutdown: Shutdown | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.cfg = cfg or get_settings()
        self.sources = list(sources) if sources is not None else enabled_sources(self.cfg)
        self.offline = offline
        self.limit = limit
        self.resume = self.cfg.run.resume if resume is None else resume
        self.shutdown = shutdown or Shutdown()
        self.policies = Policies.from_config(self.cfg.retry)
        self.state = StateStore(Path(self.cfg.run.state_file))
        self.history = TaskHistory(Path(self.cfg.run.task_history_file))
        self.recorder = Recorder(
            Path(self.cfg.diagnostics.failures_dir),
            save_traces=self.cfg.diagnostics.save_traces,
        )
        self.notifier = notifier if notifier is not None else Notifier.from_config(self.cfg.notify)
        self.repo = TorrentRepository(self.cfg.db_path)

    # ── Entry point ─────────────────────────────────────────────────────

    async def run(self) -> RunOutcome:
        """Search every configured source once and report what happened."""
        log = get_logger()
        started = time.monotonic()
        outcome = RunOutcome()
        self.shutdown.install()
        try:
            with self.repo, new_progress() as progress:
                try:
                    for source in self.sources:
                        if self.shutdown.requested:
                            log.warning(
                                "Interrupted ({}) — not starting {}",
                                self.shutdown.reason,
                                source.name,
                            )
                            break
                        outcome.sources.append(await self._run_source(source, progress))
                finally:
                    self.state.save()
                    self.history.save()
        finally:
            self.shutdown.restore()

        outcome.interrupted = self.shutdown.requested
        outcome.elapsed = time.monotonic() - started
        log.info("Run finished: {}", outcome.describe())
        if outcome.stale:
            log.warning(
                "{} task(s) have found nothing for at least {} day(s) — review the task "
                "list with `ricercar tasks`",
                outcome.stale,
                self.cfg.run.stale_task_days,
            )
        return outcome

    # ── One source ──────────────────────────────────────────────────────

    async def _run_source(self, source: Source, progress: Progress) -> SourceOutcome:
        log = get_logger()
        started = time.monotonic()
        outcome = SourceOutcome(source=source.name)

        budget = self._budget(source)
        tasks = self._tasks_for(source)
        outcome.tasks = len(tasks)

        if not budget:
            outcome.skipped = "the daily quota is used up"
            log.warning(
                "{}: not running — {} torrent(s) of today's {} are already stored",
                source.name,
                self.repo.count_today_from(source.host),
                source.settings.quota.limit_torrents_one_day,
            )
            return outcome
        if not tasks:
            outcome.skipped = "no tasks to run"
            log.warning("{}: not running — no tasks left to do", source.name)
            return outcome

        log.info(
            "{}: {} task(s), room for {} more torrent(s) today",
            source.name,
            len(tasks),
            budget,
        )
        await self.notifier.run_started(source.name, [plan.task for plan in tasks])

        stale: list[TaskYield] = []
        try:
            async with managed_browser(source, self.cfg) as session:
                stale = await self._drive(source, session, tasks, budget, outcome, progress)
        except SessionExpiredError as exc:
            outcome.failures += 1
            outcome.aborted = True
            outcome.skipped = "no usable session"
            log.error("{}: {}", source.name, exc)
        except RuntimeError as exc:
            # The browser is not running, or it has no tab open.
            outcome.failures += 1
            outcome.aborted = True
            outcome.skipped = str(exc).splitlines()[0]
            log.error("{}: {}", source.name, exc)
        finally:
            outcome.stale = len(stale)
            outcome.elapsed = time.monotonic() - started
            await self.notifier.run_finished(
                source.name,
                tasks=len(tasks),
                downloaded=outcome.downloaded,
                stored=outcome.stored,
                new_authors=outcome.new_authors,
                elapsed=outcome.elapsed,
                interrupted=self.shutdown.requested,
                stale=[stat.description or stat.key for stat in stale],
                stale_days=self.cfg.run.stale_task_days,
            )
        return outcome

    async def _drive(
        self,
        source: Source,
        session: BrowserSession,
        plans: Sequence[PlannedSearch],
        budget: int,
        outcome: SourceOutcome,
        progress: Progress,
    ) -> list[TaskYield]:
        """Work through the searches in two tabs: results, and downloads.

        Returns the tasks that look dead after this run (see :mod:`ricercar.history`);
        an empty list when the source aborted, since a run that did not finish is no
        evidence about anything.
        """
        log = get_logger()
        results_page = await session.context.new_page()
        topic_page = await session.context.new_page()
        tasks_bar = progress.add_task(f"{source.name}: tasks", total=len(plans))
        torrents_bar = progress.add_task(f"{source.name}: torrents", total=budget)
        failures_in_a_row = 0
        run_yields: dict[str, _TaskRun] = {}
        try:
            await self._prepare(source, session, results_page, topic_page)

            for plan in plans:
                task = plan.task
                if self.shutdown.requested:
                    log.warning(
                        "Interrupted ({}) — stopping after this torrent", self.shutdown.reason
                    )
                    break
                remaining = budget - outcome.stored
                if remaining <= 0:
                    log.info("{}: the daily quota is used up", source.name)
                    break

                seen_before = outcome.hits
                stored_before = outcome.stored
                try:
                    finished = await self._run_task(
                        source,
                        plan,
                        results_page,
                        topic_page,
                        remaining,
                        outcome,
                        progress,
                        torrents_bar,
                    )
                except SelectorsBrokenError as exc:
                    failures_in_a_row += 1
                    outcome.failures += 1
                    await self.recorder.capture(results_page, "selectors", issues=exc.issues)
                    log.error(
                        "{}: the page does not match the selectors any more — {}",
                        source.name,
                        "; ".join(exc.issues),
                    )
                    if failures_in_a_row >= MAX_TASK_FAILURES:
                        outcome.skipped = "the site stopped matching the selectors"
                        log.error(
                            "{}: {} task(s) failed in a row; skipping the rest of this source",
                            source.name,
                            failures_in_a_row,
                        )
                        break
                    continue
                except PlaywrightTimeout as exc:
                    issues = await self._diagnose(source, results_page)
                    outcome.failures += 1
                    await self.recorder.capture(
                        results_page,
                        "selectors" if issues else "timeout",
                        issues=issues,
                        note=str(exc),
                    )
                    if issues:
                        failures_in_a_row += 1
                        log.error(
                            "{}: giving up on {} — {}",
                            source.name,
                            describe_task(task),
                            "; ".join(issues),
                        )
                        if failures_in_a_row >= MAX_TASK_FAILURES:
                            outcome.skipped = "the site stopped matching the selectors"
                            break
                    else:
                        failures_in_a_row = 0
                        log.warning(
                            "{}: {} timed out, moving on to the next task",
                            source.name,
                            describe_task(task),
                        )
                    continue
                else:
                    failures_in_a_row = 0
                    # The search produced a page, so it counts towards the task's yield —
                    # a search that failed above does not, and must not look like "found
                    # nothing".
                    counted = run_yields.setdefault(plan.origin, _TaskRun(plan.label))
                    counted.searches += 1
                    counted.hits += outcome.hits - seen_before
                    counted.stored += outcome.stored - stored_before
                    if not finished:
                        log.warning(
                            "{}: {} was cut short — it stays on the list for the next run",
                            source.name,
                            describe_task(task),
                        )
                        continue
                    outcome.done += 1
                    self.state.mark_completed(source.name, task)
                    progress.advance(tasks_bar)
        finally:
            self._record_yields(source, run_yields)
            progress.remove_task(tasks_bar)
            progress.remove_task(torrents_bar)
            await self.recorder.stop()
            await results_page.close()
            await topic_page.close()

        return self._report_stale(
            source,
            self.history.stale(
                source.name,
                min_runs=self.cfg.run.stale_task_runs,
                min_days=self.cfg.run.stale_task_days,
            ),
        )

    def _record_yields(self, source: Source, run_yields: dict[str, _TaskRun]) -> None:
        """Add this run's numbers to the history of each configured task."""
        for origin, counted in run_yields.items():
            self.history.record(
                source.name,
                origin,
                counted.label,
                searches=counted.searches,
                hits=counted.hits,
                stored=counted.stored,
            )

    def _report_stale(self, source: Source, stale: list[TaskYield]) -> list[TaskYield]:
        """Say which tasks have stopped finding anything, and return them.

        Only tasks the config still lists: warning about one that was already removed
        would be noise, and the report is where leftovers belong.
        """
        configured = configured_task_keys(source.settings)
        stale = [stat for stat in stale if stat.key in configured]
        if not stale:
            return stale

        log = get_logger()
        days = self.cfg.run.stale_task_days
        shown = stale[:LISTED_STALE]
        more = len(stale) - len(shown)
        log.warning(
            "{}: {} task(s) have found nothing in the last {} day(s) — consider removing "
            "them (`ricercar tasks`, or `ricercar tasks --stale` for the whole list): {}",
            source.name,
            len(stale),
            days,
            "; ".join(stat.description or stat.key for stat in shown)
            + (f"; … and {more} more" if more else ""),
        )
        return stale

    # ── One task ────────────────────────────────────────────────────────

    async def _run_task(
        self,
        source: Source,
        plan: PlannedSearch,
        results_page: Page,
        topic_page: Page,
        budget: int,
        outcome: SourceOutcome,
        progress: Progress,
        torrents_bar: TaskID,
    ) -> bool:
        """Search one task and store whatever is new.

        Returns whether the task ran to the end. A task cut short by an interrupt or
        by the quota is *not* finished, so it is not recorded in the state and the next
        run repeats it — cheaply, because what was already stored is skipped by URL.

        Raises:
            SelectorsBrokenError: the results page is not what the source expects.
            PlaywrightTimeout: the search never produced a usable page.
            SessionExpiredError: no session could be established even after logging
                in again.
        """
        log = get_logger()
        task = plan.task
        stored = 0
        logged_in_again = False
        pages_bar_label = f"  {describe_task(task)}"
        pages_bar = progress.add_task(pages_bar_label, total=source.settings.max_pages)

        async def search_and_download() -> bool:
            nonlocal stored
            stored = 0
            progress.update(pages_bar, completed=0)
            pages = source.search(results_page, task, max_pages=source.settings.max_pages)
            page_index = 0
            label = pages_bar_label

            while True:
                page_index += 1
                asked_on_page = 0
                # One chunk per results page: a page that fails keeps a trace that
                # contains exactly that page, and a healthy page's trace (megabytes,
                # with snapshots) is dropped instead of piling up for the whole task.
                async with self.recorder.chunk(f"{describe_task(task)} — page {page_index}"):
                    try:
                        hits = await pages.__anext__()
                    except StopAsyncIteration:
                        return True

                    progress.advance(pages_bar)
                    outcome.new_authors += len(
                        self.state.remember_authors(source.name, [hit.author for hit in hits])
                    )
                    for hit in hits:
                        outcome.hits += 1
                        if self.shutdown.requested:
                            # Cut short by a signal: this task has to be retried.
                            return False
                        if stored >= budget:
                            # The quota (or --limit) said stop — that is a planned end,
                            # not an interrupted one.
                            return True
                        if (known := self.repo.stored(hit.url)) is not None:
                            outcome.known += 1
                            log.debug("Already stored: {} — added {}", hit.url, known.add_date)
                            continue
                        progress.update(torrents_bar, description=f"  {hit.title[:48]}")
                        asked_on_page += 1
                        if await self._download_hit(source, topic_page, hit, outcome):
                            stored += 1
                            progress.advance(torrents_bar)

                if asked_on_page:
                    # This page asked the tracker for something, so pace what comes next —
                    # the next page of this search, or the first page of the next task, both
                    # of which are fetched where the loop starts again. A rejected download
                    # counts: the request was made, and it is the request rate that gets an
                    # account noticed. A hit we already have never reaches this counter.
                    await self._wait_for_the_next_page(source, label, progress, pages_bar)

        try:
            while True:
                try:
                    return await call(
                        describe_task(task), search_and_download, policies=self.policies
                    )
                except SessionExpiredError as exc:
                    if logged_in_again:
                        raise
                    logged_in_again = True
                    log.warning("{}: {} — logging in again", source.name, exc)
                    await self.recorder.capture(results_page, "session-expired", note=str(exc))
                    await call(
                        f"{source.name} login",
                        lambda: source.ensure_logged_in(results_page),
                        policies=self.policies,
                    )
        finally:
            progress.remove_task(pages_bar)

    async def _wait_for_the_next_page(
        self,
        source: Source,
        label: str,
        progress: Progress,
        pages_bar: TaskID,
    ) -> None:
        """Wait out ``quota.page_delay_seconds``, showing the countdown on the page bar.

        Called after a page that asked the tracker for at least one torrent — see the
        comment at the call site for why only that page waits. The countdown goes on the
        pages bar because that is the line that would otherwise sit still, and a still
        line for a minute looks like a hang. Ctrl-C ends the wait rather than sitting it
        out.
        """
        delay = source.settings.quota.page_delay_seconds
        if delay <= 0:
            return

        log = get_logger()
        log.info("Waiting {}s before the next page (quota.page_delay_seconds)", delay)

        def countdown(left: float) -> None:
            progress.update(pages_bar, description=f"{label} — waiting {math.ceil(left)}s")

        try:
            await wait_between_pages(
                delay,
                stop=lambda: self.shutdown.requested,
                on_tick=countdown,
            )
        finally:
            progress.update(pages_bar, description=label)

    async def _download_hit(
        self,
        source: Source,
        topic_page: Page,
        hit: SearchHit,
        outcome: SourceOutcome,
    ) -> bool:
        """Fetch, parse and store one torrent. Returns whether it was stored."""
        log = get_logger()
        outcome.downloaded += 1
        try:
            info, path = await call(
                f"torrent {hit.topic_id}",
                lambda: source.fetch_torrent(topic_page, hit, self.cfg.download_dir),
                policies=self.policies,
            )
        except TorrentUnavailableError as exc:
            log.info("Skipping {}: {}", hit.url, exc)
            return False
        except SelectorsBrokenError as exc:
            outcome.failures += 1
            await self.recorder.capture(topic_page, "topic-page", issues=exc.issues, note=hit.url)
            log.error("{}: the topic page does not look right — {}", hit.url, "; ".join(exc.issues))
            return False
        except PlaywrightTimeout as exc:
            outcome.failures += 1
            await self.recorder.capture(topic_page, "download-timeout", note=f"{hit.url}: {exc}")
            log.warning("Giving up on {}: {}", hit.url, exc)
            return False

        try:
            meta = parse_file(path)
        except TorrentError as exc:
            outcome.failures += 1
            log.error("Unreadable torrent from {}: {}", hit.url, exc)
            return False

        log.debug("  topic: {} (downloadable={})", info.title[:70], info.downloadable)
        for issue in meta.issues:
            log.warning("  torrent issue: {}", issue)
        if not meta.url:
            # Not every torrent carries a `comment`; fall back to where we got it.
            meta = replace(meta, url=hit.url)

        if not self.repo.add(meta, path.read_bytes()):
            outcome.known += 1
            # The same content under a different topic is the case worth naming: the row
            # that won is not the one this download came from, and when it was added is
            # the answer to "why was this not stored?".
            same = self.repo.stored_content(meta.md5)
            if same is None:  # pragma: no cover - only when another writer won the race
                log.info("Already stored (same content): {!r}", meta.name)
            else:
                log.info(
                    "Already stored as {!r} — added {}: {}",
                    same.name,
                    same.add_date,
                    same.url,
                )
            return False

        outcome.stored += 1
        log.info(
            "Stored {!r} — {} bytes, {} file(s), md5 {}",
            meta.name,
            meta.size,
            meta.file_count,
            meta.md5,
        )
        return True

    # ── Preparation ─────────────────────────────────────────────────────

    async def _prepare(
        self,
        source: Source,
        session: BrowserSession,
        results_page: Page,
        topic_page: Page,
    ) -> None:
        """Start tracing, install the offline routes, and make sure we are logged in."""
        await self.recorder.start(session.context)

        if self.offline is not None:
            await self._go_offline(source, results_page, topic_page, offline=self.offline)
            return

        await call(
            f"{source.name} login",
            lambda: source.ensure_logged_in(results_page),
            policies=self.policies,
        )

    async def _go_offline(
        self,
        source: Source,
        results_page: Page,
        topic_page: Page,
        *,
        offline: Path,
    ) -> None:
        """Serve the source's pages from fixtures and refuse everything else.

        The catch-all is registered *before* the fixture routes (Playwright matches
        the most recently added route first), so anything the fixtures do not cover —
        an image, an advert, a redirect to the tracker itself — is aborted instead of
        quietly going online. That is what makes "offline" a fact rather than a hope.
        """
        log = get_logger()
        router = FixtureRouter(offline)
        for pattern, fixture in source.fixture_routes().items():
            router.register(pattern, fixture)

        async def refuse(route: Any) -> None:
            log.debug("Offline: refusing {}", route.request.url[:100])
            await route.abort("blockedbyclient")

        for page in (results_page, topic_page):
            await page.route("**/*", refuse)
            await router.apply(page)

        log.warning(
            "Offline mode: {} route(s) from {} — everything else is refused, and no "
            "login is attempted",
            len(router.patterns()),
            offline,
        )

    # ── Planning helpers ────────────────────────────────────────────────

    def _budget(self, source: Source) -> int:
        """How many torrents this source may still store today."""
        room = max(
            source.settings.quota.limit_torrents_one_day - self.repo.count_today_from(source.host),
            0,
        )
        return room if self.limit is None else min(room, self.limit)

    def _tasks_for(self, source: Source) -> list[PlannedSearch]:
        """The searches to run for this source, and the task each of them came from.

        A configured task may stand for several searches (see :meth:`Source.expand`), so
        what comes back is the concrete ones — what the progress bars, the resume state
        and the quota then count. Tasks finished earlier today are dropped when resuming,
        which is what makes an interrupted run pick up where it stopped.
        """
        log = get_logger()
        chosen = list(source.settings.must_complete_tasks)
        pool = list(source.settings.tasks)
        if pool:
            chosen.extend(random.sample(pool, min(source.settings.random_choice, len(pool))))

        # A configured task can stand for several searches (see Source.expand), so the
        # bookkeeping downstream — progress, resume state, quota, history — deals in the
        # concrete ones. Expanding before deduplicating also collapses the overlap between
        # a task for "every section" and one that names a section inside it; when two
        # tasks share a search, it counts towards the one that asked for it first.
        plans: dict[str, PlannedSearch] = {}
        for task in chosen:
            origin, label = task_key(task), describe_task(task)
            for concrete in source.expand(task):
                plans.setdefault(task_key(concrete), PlannedSearch(concrete, origin, label))
        tasks = list(plans.values())
        if len(tasks) != len(chosen):
            log.info(
                "{}: {} configured task(s) are {} search(es) to run",
                source.name,
                len(chosen),
                len(tasks),
            )

        if self.resume:
            done = self.state.completed_today(source.name)
            remaining = [plan for plan in tasks if plan.key not in done]
            if len(remaining) != len(tasks):
                log.info(
                    "{}: resuming — {} of {} task(s) were already done today",
                    source.name,
                    len(tasks) - len(remaining),
                    len(tasks),
                )
            tasks = remaining
        return tasks

    async def _diagnose(self, source: Source, page: Page) -> tuple[str, ...]:
        """Ask the source what is wrong with *page* (empty when nothing is)."""
        try:
            html = await page.content()
        except PlaywrightError as exc:  # pragma: no cover - the page is gone
            get_logger().debug("Could not read the page for a diagnosis: {}", exc)
            return ()
        return source.diagnose(html)


def run_sources(*, cfg: Settings | None = None, **kwargs: Any) -> RunOutcome:
    """Run once from a synchronous context (the CLI and the scheduler both do)."""
    return asyncio.run(Runner(cfg, **kwargs).run())


__all__ = [
    "MAX_TASK_FAILURES",
    "RunOutcome",
    "Runner",
    "Shutdown",
    "SourceOutcome",
    "describe_task",
    "run_sources",
]
