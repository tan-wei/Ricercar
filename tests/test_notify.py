"""Notifications: what a run report says, and how much of it."""

from __future__ import annotations

import asyncio

import pytest

from ricercar.config import SearchTask, Settings, SourceSettings
from ricercar.history import TaskYield
from ricercar.notify import Notifier, _listing


def test_a_short_run_is_listed_in_full() -> None:
    tasks = [SearchTask(text="Bach"), SearchTask(author="someone", category=794)]

    listing = _listing(tasks)

    assert listing == "- Bach\n- (no text) [author=someone, category=794]"


@pytest.mark.parametrize(
    ("status", "hits", "last_stored", "expected"),
    [
        ("completed", 0, None, "Consider Removing"),
        ("completed", 20, None, "Consider Lower Frequency"),
        ("failed", 0, None, "Needs Attention"),
        ("blocked", 0, None, "Needs Attention"),
        ("incomplete", 0, None, "Needs Attention"),
        ("not run", 0, None, "Insufficient Observation"),
        ("completed", 20, "recent", "Worth Keeping"),
    ],
)
def test_task_adjustment_groups(
    status: str,
    hits: int,
    last_stored: str | None,
    expected: str,
) -> None:
    from datetime import date

    notifier = Notifier()
    sent = _capture_into(notifier)
    stat = TaskYield(
        key="task",
        first_run="2020-01-01",
        runs=10,
        hits=hits,
        last_hit=date.today().isoformat() if hits else None,
        last_stored=date.today().isoformat() if last_stored else None,
    )
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=1,
            downloaded=0,
            stored=0,
            task_reviews=[(SearchTask(text="Bach"), stat, status)],
            stale_days=60,
        )
    )
    assert f"### {expected} (1)" in sent["body"]
    assert "Suggestions do not change the configuration" in sent["body"]
    if status != "completed":
        assert "### Consider Removing" not in sent["body"]


def test_zero_downloads_show_only_successfully_completed_tasks() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=2,
            downloaded=0,
            stored=0,
            task_results={"completed": []},
            task_history={"completed": TaskYield(key="done"), "failed": TaskYield(key="failed")},
        )
    )
    assert "1 task(s) yielded nothing" in sent["body"]
    assert "| completed | never |" in sent["body"]
    assert "| failed |" not in sent["body"]


def test_fresh_task_does_not_trigger_removal() -> None:
    from datetime import date

    notifier = Notifier()
    sent = _capture_into(notifier)
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=1,
            downloaded=0,
            stored=0,
            task_reviews=[
                (
                    SearchTask(text="new"),
                    TaskYield(
                        key="new",
                        first_run=date.today().isoformat(),
                        runs=1,
                    ),
                    "completed",
                )
            ],
        )
    )
    assert "### Insufficient Observation (1)" in sent["body"]
    assert "### Consider Removing" not in sent["body"]


def test_candidate_email_shows_evidence_names_and_omitted_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ricercar.discovery import DiscoveredUploader

    settings = Settings(sources={"rutracker": SourceSettings(categories={"Opera_Lossless": 794})})
    monkeypatch.setattr("ricercar.notify.get_settings", lambda: settings)
    notifier = Notifier()
    sent = _capture_into(notifier)
    candidates = [
        DiscoveredUploader(
            author=f"candidate_{number}",
            source="rutracker",
            first_seen="2026-01-01",
            last_seen="2026-10-20",
            sections_seen=[794],
            hits=3,
        )
        for number in range(21)
    ]
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=1,
            downloaded=0,
            stored=0,
            candidate_uploaders=candidates,
        )
    )
    assert "### Consider Adding (21)" in sent["body"]
    assert "sections=Opera_Lossless" in sent["body"]
    assert "first seen=2026-01-01; last seen=2026-10-20" in sent["body"]
    assert "observed result rows=3 (not unique torrents)" in sent["body"]
    assert "1 more candidate(s) omitted" in sent["body"]


def test_all_tasks_are_listed() -> None:
    tasks = [SearchTask(text=f"task {index}") for index in range(120)]

    listing = _listing(tasks)

    assert len(listing.splitlines()) == 120
    assert "more" not in listing


def test_a_run_without_tasks_says_so() -> None:
    assert _listing([]) == "- (no tasks configured)"


def test_no_url_configured_means_no_notification() -> None:
    notifier = Notifier()

    assert notifier.enabled is False
    assert notifier.targets == 0


def _capture_into(notifier: Notifier) -> dict[str, str]:
    """Replace the delivery with a recorder, and hand back where it writes."""
    sent: dict[str, str] = {}

    async def capture(title: str, body: str, **_: object) -> bool:
        sent["title"], sent["body"] = title, body
        return True

    notifier.send = capture  # type: ignore[method-assign]
    return sent


@pytest.mark.parametrize(
    ("source", "category", "expected"),
    [
        ("rutracker", 794, "Opera_Lossless"),
        ("rutracker", 999, "999"),
        ("unknown", 794, "794"),
    ],
)
def test_start_notification_names_categories(
    monkeypatch: pytest.MonkeyPatch, source: str, category: int, expected: str
) -> None:
    settings = Settings(sources={"rutracker": SourceSettings(categories={"Opera_Lossless": 794})})
    monkeypatch.setattr("ricercar.notify.get_settings", lambda: settings)
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(notifier.run_started(source, [SearchTask(author="someone", category=category)]))

    assert sent["body"] == (
        f"## {source}\n\n**1 task(s)**\n\n- (no text) [author=someone, category={expected}]"
    )


@pytest.mark.parametrize("budget", [None, 0, 20])
def test_start_notification_title_includes_available_download_budget(budget: int | None) -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(notifier.run_started("rutracker", [SearchTask(text="Bach")], budget=budget))

    assert "[rutracker] Going to start 1 task(s)" in sent["title"]
    if budget is None:
        assert "up to" not in sent["title"]
    else:
        assert f", up to {budget} torrent(s) at " in sent["title"]


def test_a_finish_notification_names_the_tasks_worth_removing() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=28,
            downloaded=3,
            stored=2,
            stale=["Bach", "ghost"],
            stale_days=60,
        )
    )

    assert "2 task(s) have found nothing for 60 day(s)" in sent["body"]
    assert "Bach, ghost" in sent["body"]


def test_all_dead_tasks_are_listed() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=1,
            downloaded=0,
            stored=0,
            stale=[f"task {index}" for index in range(40)],
            stale_days=60,
        )
    )

    assert "40 task(s)" in sent["body"]
    for i in range(40):
        assert f"task {i}" in sent["body"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_finish_explains_run_limit_and_every_attempt_not_stored(interrupted: bool) -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)
    details = [
        "Topic B (https://tracker.example/topic/2): Already stored as 'Bach'; "
        "added 2026-10-20: https://tracker.example/topic/1 (duplicate content)",
        "Topic D (https://tracker.example/topic/4): Download timed out after retries.",
    ]

    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=3,
            downloaded=4,
            stored=2,
            budget=68,
            interrupted=interrupted,
            not_stored_details=details,
        )
    )

    head = "Interrupted after storing" if interrupted else "Stored"
    assert f"{head} 2 new torrent(s) from 4 download attempt(s)" in sent["body"]
    assert "Run limit: at most 68 new torrent(s) may be stored this run." in sent["body"]
    assert "### Download Attempts Not Stored" in sent["body"]
    assert "2 attempt(s) did not add a new torrent" in sent["body"]
    for detail in details:
        assert f"- {detail}" in sent["body"]
    assert "4 downloaded" not in sent["body"]
    assert " / 68" not in sent["body"]


def test_progress_mail_lists_interval_work_budget_and_issues() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(
        notifier.run_progress(
            "rutracker",
            elapsed=21600,
            period_elapsed=3600,
            completed=["Bach", "Mozart"],
            tasks_done=12,
            tasks=30,
            attempted=4,
            stored=2,
            stored_total=18,
            budget=68,
            torrents=["Cantatas", "Requiem"],
            not_stored=["Topic B: duplicate content"],
            issues=["Topic D: download timed out"],
            phase="Downloading torrents",
            current_task="Bach",
            idle_seconds=120,
        )
    )

    assert "Running 6.0h" in sent["title"]
    assert "2 task(s) completed; 4 download attempt(s); 2 new torrent(s) stored" in sent["body"]
    assert "at most 68 new torrent(s)" in sent["body"]
    assert "room for 50 more" in sent["body"]
    for detail in ("Bach", "Mozart", "Cantatas", "Requiem", "duplicate content", "timed out"):
        assert detail in sent["body"]


def test_progress_mail_explains_empty_period_without_claiming_no_available_torrents() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(
        notifier.run_progress(
            "rutracker",
            elapsed=3600,
            period_elapsed=3600,
            completed=[],
            tasks_done=0,
            tasks=2,
            attempted=0,
            stored=0,
            stored_total=0,
            budget=5,
            torrents=[],
            not_stored=[],
            issues=[],
            phase="Waiting for manual login",
        )
    )

    assert "0 task(s) completed; 0 download attempt(s)" in sent["body"]
    assert "Waiting for manual login" in sent["body"]
    assert sent["body"].count("- None.") == 4


def test_run_alert_uses_warning_delivery_type(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    import apprise

    settings = Settings(sources={"rutracker": SourceSettings(categories={"Opera_Lossless": 794})})
    monkeypatch.setattr("ricercar.notify.get_settings", lambda: settings)
    notifier = Notifier()
    notifier.send = AsyncMock(return_value=True)
    asyncio.run(
        notifier.run_alert(
            "rutracker",
            alerts=["Complete Cloudflare verification", "Login required"],
            phase="Waiting for manual login",
            current_task="Bach by someone in category 794",
            elapsed=7200,
        )
    )
    assert notifier.send.call_args.kwargs["notify_type"] == apprise.NotifyType.WARNING
    assert "Complete Cloudflare verification" in notifier.send.call_args.args[1]
    assert "Login required" in notifier.send.call_args.args[1]
    assert (
        "Current task: Bach by someone in category Opera_Lossless"
        in notifier.send.call_args.args[1]
    )


def test_a_healthy_finish_says_nothing_about_tasks() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(notifier.run_finished("rutracker", tasks=1, downloaded=1, stored=1))

    assert "found nothing" not in sent["body"]
    assert "Download Attempts Not Stored" not in sent["body"]
    assert "Run limit" not in sent["body"]


@pytest.mark.parametrize("count", [20, 21, 25])
def test_new_uploaders_report_omitted_count(count: int) -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=1,
            downloaded=0,
            stored=0,
            new_authors=[f"uploader_{number}" for number in range(count)],
        )
    )
    assert "``uploader_19``" in sent["body"]
    assert "``uploader_20``" not in sent["body"]
    if count > 20:
        assert f"{count - 20} more uploader(s) omitted" in sent["body"]
    else:
        assert "omitted" not in sent["body"]


def test_finish_notification_reports_failures() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=1,
            downloaded=0,
            stored=0,
            failures=1,
            failure_details=["Login failed: no usable session."],
        )
    )
    assert "## Run Issues" in sent["body"]
    assert "1 failure(s)" in sent["body"]
    assert "Login failed: no usable session." in sent["body"]
    assert "zero downloads does not mean" in sent["body"]


def test_finish_notification_names_categories_in_all_task_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ricercar.history import TaskYield

    settings = Settings(sources={"rutracker": SourceSettings(categories={"Opera_Lossless": 794})})
    monkeypatch.setattr("ricercar.notify.get_settings", lambda: settings)
    notifier = Notifier()
    sent = _capture_into(notifier)
    asyncio.run(
        notifier.run_finished(
            "rutracker",
            tasks=2,
            downloaded=1,
            stored=1,
            task_results={"Bach in category 794": ["Cantatas"], "Mozart in category 794": []},
            task_history={"Mozart in category 794": TaskYield(key="quiet")},
            stale=["Ghost in category 794", "Unknown in category 999"],
            stale_days=60,
            failure_details=["Search timed out: Bach in category 794."],
        )
    )
    assert "Bach in category Opera_Lossless" in sent["body"]
    assert "Mozart in category Opera_Lossless" in sent["body"]
    assert "Ghost in category Opera_Lossless" in sent["body"]
    assert "Unknown in category 999" in sent["body"]
    assert "in category 794" not in sent["body"]
    assert "A preliminary check failed." in sent["body"]
