"""Interval run reports and globally rate-limited exception alerts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ricercar.log import get_logger

if TYPE_CHECKING:
    from ricercar.config import NotifyConfig
    from ricercar.notify import Notifier


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    completed: tuple[str, ...] = ()
    attempted: int = 0
    stored: int = 0
    torrents: tuple[str, ...] = ()
    not_stored: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    budget: int | None = None


_ACTIVE: ContextVar[ProgressReporter | None] = ContextVar("run_progress", default=None)


def report_activity(
    phase: str,
    *,
    task: str | None = None,
    wait_seconds: float = 0,
    issue: str | None = None,
) -> None:
    """Update the current run without changing browser or retry function signatures."""
    if (reporter := _ACTIVE.get()) is not None:
        reporter.activity(phase, task=task, wait_seconds=wait_seconds, issue=issue)


class ProgressReporter:
    """Observe a running source independently of search completion."""

    def __init__(
        self,
        notifier: Notifier,
        config: NotifyConfig,
        source: str,
        *,
        tasks: int,
        budget: int,
        snapshot: Callable[[], ProgressSnapshot],
        run_limit: int | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.notifier = notifier
        self.config = config
        self.source = source
        self.tasks = tasks
        self.budget = budget
        self.run_limit = run_limit
        self.snapshot = snapshot
        self.now = now
        self.started = now()
        self.phase = "Connecting to browser"
        self.current_task = ""
        self._last_activity = self.started
        self._wait_until = self.started
        self._stalled = False
        self._interval = config.progress_interval_minutes * 60
        self._cooldown = config.alert_cooldown_minutes * 60
        self._stall = config.stall_after_minutes * 60
        self._next_periodic = self.started + self._interval if self._interval else float("inf")
        self._period_start = self.started
        self._baseline = snapshot()
        self._period_issue_start = 0
        self._seen_issues = 0
        self._issues: list[str] = []
        self._pending_alerts: dict[str, str] = {}
        self._last_alert: float | None = None
        self._observed = self._baseline
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._token: Token[ProgressReporter | None] | None = None

    def start(self) -> None:
        if not self.notifier.enabled or (not self._interval and not self.config.alerts_enabled):
            return
        self._token = _ACTIVE.set(self)
        self._task = asyncio.create_task(self._run(), name=f"{self.source}-progress")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None

    def _alert(self, key: str, detail: str) -> None:
        self._issues.append(detail)
        if self.config.alerts_enabled:
            self._pending_alerts[key] = detail
            self._wake.set()

    def activity(
        self,
        phase: str,
        *,
        task: str | None = None,
        wait_seconds: float = 0,
        issue: str | None = None,
    ) -> None:
        self.phase = phase
        if task is not None:
            self.current_task = task
        self._last_activity = self.now()
        self._wait_until = self._last_activity + max(wait_seconds, 0)
        if self._stalled:
            self._stalled = False
            self._alert("recovered", "Activity resumed after an inactivity alert.")
        if issue:
            self._alert(issue, issue)

    async def _send(self, operation: Awaitable[bool]) -> bool:
        try:
            return await asyncio.wait_for(operation, timeout=30)
        except Exception as exc:
            get_logger().warning("{}: progress notification failed: {}", self.source, exc)
            return False

    def _consume_alerts(self, sent: dict[str, str]) -> None:
        for key, detail in sent.items():
            if self._pending_alerts.get(key) == detail:
                del self._pending_alerts[key]

    async def poll(self) -> None:
        """Inspect state once; injected time makes intervals and cooldowns testable."""
        now = self.now()
        state = self.snapshot()
        for issue in state.issues[self._seen_issues :]:
            self._alert(issue, issue)
        self._seen_issues = len(state.issues)
        if (state.completed, state.attempted, state.stored) != (
            self._observed.completed,
            self._observed.attempted,
            self._observed.stored,
        ):
            self._last_activity = now
            if self._stalled:
                self._stalled = False
                self._alert("recovered", "Activity resumed after an inactivity alert.")
        self._observed = state
        if (
            self._stall
            and not self._stalled
            and now - max(self._last_activity, self._wait_until) >= self._stall
        ):
            self._stalled = True
            detail = (
                f"No activity for {self._stall / 60:g} minute(s) outside known waits. "
                f"Current phase: {self.phase}."
            )
            get_logger().warning("{}: {}", self.source, detail)
            self._alert("inactive", detail)

        if now >= self._next_periodic:
            issue_end = len(self._issues)
            pending = dict(self._pending_alerts)
            delivered = await self._send(
                self.notifier.run_progress(
                    self.source,
                    elapsed=now - self.started,
                    period_elapsed=now - self._period_start,
                    completed=state.completed[len(self._baseline.completed) :],
                    tasks_done=len(state.completed),
                    tasks=self.tasks,
                    attempted=state.attempted - self._baseline.attempted,
                    stored=state.stored - self._baseline.stored,
                    stored_total=state.stored,
                    budget=state.budget if state.budget is not None else self.budget,
                    run_limit=self.run_limit,
                    torrents=state.torrents[len(self._baseline.torrents) :],
                    not_stored=state.not_stored[len(self._baseline.not_stored) :],
                    issues=tuple(self._issues[self._period_issue_start : issue_end]),
                    phase=self.phase,
                    current_task=self.current_task,
                    idle_seconds=now - self._last_activity,
                )
            )
            self._next_periodic = self.now() + self._interval
            if delivered:
                self._baseline = state
                self._period_start = now
                self._period_issue_start = issue_end
                self._consume_alerts(pending)
                if pending:
                    self._last_alert = now
            return

        if self._pending_alerts and (
            self._last_alert is None or now - self._last_alert >= self._cooldown
        ):
            pending = dict(self._pending_alerts)
            self._last_alert = now
            if await self._send(
                self.notifier.run_alert(
                    self.source,
                    alerts=tuple(pending.values()),
                    phase=self.phase,
                    current_task=self.current_task,
                    elapsed=now - self.started,
                )
            ):
                self._consume_alerts(pending)

    async def _run(self) -> None:
        tick = min(30, self._interval or 30, self._cooldown, self._stall or 30)
        while True:
            self._wake.clear()
            try:
                await self.poll()
            except Exception as exc:
                get_logger().warning("{}: progress reporting failed: {}", self.source, exc)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=tick)
