"""Notifications: what a run report says, and how much of it."""

from __future__ import annotations

import asyncio

import pytest

from ricercar.config import SearchTask, Settings, SourceSettings
from ricercar.notify import Notifier, _listing


def test_a_short_run_is_listed_in_full() -> None:
    tasks = [SearchTask(text="Bach"), SearchTask(author="someone", category=794)]

    listing = _listing(tasks)

    assert listing == "- Bach\n- (no text) [author=someone, category=794]"


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


def test_a_healthy_finish_says_nothing_about_tasks() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(notifier.run_finished("rutracker", tasks=1, downloaded=1, stored=1))

    assert "found nothing" not in sent["body"]


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
