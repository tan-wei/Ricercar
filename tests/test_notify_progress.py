"""Progress reporting with a virtual clock and no delivery or browser."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ricercar.config import NotifyConfig
from ricercar.notify.progress import ProgressReporter, ProgressSnapshot, report_activity


@pytest.fixture
def reporting() -> Any:
    state = SimpleNamespace(now=0.0, snapshot=ProgressSnapshot())
    notifier = SimpleNamespace(
        enabled=True,
        run_progress=AsyncMock(return_value=True),
        run_alert=AsyncMock(return_value=True),
    )
    reporter = ProgressReporter(
        notifier,
        NotifyConfig(
            progress_interval_minutes=60, alert_cooldown_minutes=30, stall_after_minutes=0
        ),
        "fake",
        tasks=10,
        budget=68,
        snapshot=lambda: state.snapshot,
        now=lambda: state.now,
    )
    return reporter, state, notifier


def test_periods_report_only_new_work_and_alerts_do_not_consume_it(reporting: Any) -> None:
    reporter, state, notifier = reporting
    state.snapshot = ProgressSnapshot(
        completed=("Bach",),
        attempted=2,
        stored=1,
        torrents=("Cantatas",),
        not_stored=("duplicate",),
        issues=("download timed out",),
        budget=50,
    )
    asyncio.run(reporter.poll())
    notifier.run_alert.assert_awaited_once()
    state.now = 3600
    asyncio.run(reporter.poll())
    first = notifier.run_progress.call_args.kwargs
    assert first["completed"] == ("Bach",)
    assert first["torrents"] == ("Cantatas",)
    assert first["not_stored"] == ("duplicate",)
    assert first["issues"] == ("download timed out",)
    assert first["budget"] == 50
    state.snapshot = replace(
        state.snapshot,
        completed=("Bach", "Mozart"),
        attempted=3,
        stored=2,
        torrents=("Cantatas", "Requiem"),
        budget=68,
    )
    state.now = 7200
    asyncio.run(reporter.poll())
    second = notifier.run_progress.call_args.kwargs
    assert second["completed"] == ("Mozart",)
    assert second["torrents"] == ("Requiem",)
    assert second["attempted"] == second["stored"] == 1
    assert second["not_stored"] == second["issues"] == ()
    assert second["stored_total"] == 2
    assert second["budget"] == 68


@pytest.mark.parametrize(
    "failure", [False, RuntimeError("mail offline"), TimeoutError("mail slow")]
)
def test_failed_report_preserves_unsent_work_without_fast_retry(
    reporting: Any, failure: Any
) -> None:
    reporter, state, notifier = reporting
    notifier.run_progress.side_effect = [failure, True]
    state.snapshot = ProgressSnapshot(attempted=1, stored=1, torrents=("Bach",))
    state.now = 3600
    asyncio.run(reporter.poll())
    state.now = 3601
    asyncio.run(reporter.poll())
    assert notifier.run_progress.await_count == 1
    state.now = 7200
    asyncio.run(reporter.poll())
    assert notifier.run_progress.call_args.kwargs["torrents"] == ("Bach",)
    assert notifier.run_progress.call_args.kwargs["period_elapsed"] == 7200


def test_exception_alerts_have_global_cooldown_and_batch_new_reasons(reporting: Any) -> None:
    reporter, state, notifier = reporting
    reporter.activity("Login", issue="Complete login")
    asyncio.run(reporter.poll())
    for reason in ("CF verification", "Timeout", "Timeout"):
        reporter.activity("Waiting", issue=reason)
        state.now = 1
        asyncio.run(reporter.poll())
    assert notifier.run_alert.await_count == 1
    state.now = 1800
    asyncio.run(reporter.poll())
    assert notifier.run_alert.await_count == 2
    assert notifier.run_alert.call_args.kwargs["alerts"] == ("CF verification", "Timeout")


def test_distinct_failures_are_included_in_one_alert(reporting: Any) -> None:
    reporter, state, notifier = reporting
    state.snapshot = ProgressSnapshot(issues=("Timeout on Bach", "Missing table on Mozart"))

    asyncio.run(reporter.poll())

    notifier.run_alert.assert_awaited_once()
    assert notifier.run_alert.call_args.kwargs["alerts"] == state.snapshot.issues


def test_work_arriving_during_delivery_is_kept_for_the_next_period(reporting: Any) -> None:
    reporter, state, notifier = reporting
    state.snapshot = ProgressSnapshot(attempted=1, stored=1, torrents=("Bach",))
    state.now = 3600

    async def deliver(*_args: Any, **_kwargs: Any) -> bool:
        state.snapshot = replace(state.snapshot, attempted=2, stored=2, torrents=("Bach", "Mozart"))
        reporter.activity("Downloading", issue="A new error during delivery")
        return True

    notifier.run_progress.side_effect = deliver
    asyncio.run(reporter.poll())
    notifier.run_progress.side_effect = None
    state.now = 7200
    asyncio.run(reporter.poll())
    assert notifier.run_progress.call_args.kwargs["torrents"] == ("Mozart",)
    assert notifier.run_progress.call_args.kwargs["issues"] == ("A new error during delivery",)


def test_failed_alert_also_obeys_cooldown(reporting: Any) -> None:
    reporter, state, notifier = reporting
    notifier.run_alert.return_value = False
    reporter.activity("Login", issue="Sign in")
    asyncio.run(reporter.poll())
    state.now = 1
    asyncio.run(reporter.poll())
    assert notifier.run_alert.await_count == 1
    state.now = 1800
    asyncio.run(reporter.poll())
    assert notifier.run_alert.await_count == 2


def test_known_wait_is_not_a_stall_and_recovery_is_reported_once(reporting: Any) -> None:
    reporter, state, notifier = reporting
    reporter._stall = 60
    reporter.activity("Rate limit", wait_seconds=3600)
    state.now = 3659
    asyncio.run(reporter.poll())
    notifier.run_alert.assert_not_awaited()
    state.now = 3660
    asyncio.run(reporter.poll())
    assert "No activity" in notifier.run_alert.call_args.kwargs["alerts"][0]
    state.now = 4000
    asyncio.run(reporter.poll())
    assert notifier.run_alert.await_count == 1
    reporter.activity("Downloading")
    state.now = 5460
    reporter.activity("Downloading")
    asyncio.run(reporter.poll())
    assert notifier.run_alert.await_count == 2
    assert notifier.run_alert.call_args.kwargs["alerts"] == (
        "Activity resumed after an inactivity alert.",
    )


def test_periodic_report_includes_zero_progress_even_with_alerts_disabled(reporting: Any) -> None:
    reporter, state, notifier = reporting
    reporter.config.alerts_enabled = False
    reporter.activity("Waiting for login", issue="Sign in")
    asyncio.run(reporter.poll())
    notifier.run_alert.assert_not_awaited()
    state.now = 3600
    asyncio.run(reporter.poll())
    assert notifier.run_progress.call_args.kwargs["issues"] == ("Sign in",)


def test_stop_cancels_pending_delivery_and_clears_context(reporting: Any) -> None:
    reporter, state, notifier = reporting

    async def run() -> None:
        entered = asyncio.Event()

        async def delivery(*_args: Any, **_kwargs: Any) -> bool:
            entered.set()
            await asyncio.Future()
            return True

        notifier.run_progress.side_effect = delivery
        state.now = 3600
        reporter.start()
        await entered.wait()
        task = reporter._task
        await reporter.stop()
        assert task is not None and task.done()
        phase = reporter.phase
        report_activity("Must not reach stopped reporter")
        assert reporter.phase == phase

    asyncio.run(run())


def test_disabled_notifier_creates_no_background_task(reporting: Any) -> None:
    reporter, _, notifier = reporting
    notifier.enabled = False

    async def run() -> None:
        reporter.start()
        assert reporter._task is None
        await reporter.stop()

    asyncio.run(run())
