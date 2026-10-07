"""Notifications: what a run report says, and how much of it."""

from __future__ import annotations

import asyncio

from ricercar.config import SearchTask
from ricercar.notify import LISTED_DEAD, LISTED_TASKS, Notifier, _listing


def test_a_short_run_is_listed_in_full() -> None:
    tasks = [SearchTask(text="Bach"), SearchTask(author="someone", category=794)]

    listing = _listing(tasks)

    assert listing == "- Bach\n- (no text) [author=someone, category=794]"


def test_a_long_run_is_summarised() -> None:
    # One configured task can be dozens of searches and a run thousands of them, so the
    # message lists the first few and counts the rest.
    tasks = [SearchTask(text=f"task {index}") for index in range(120)]

    listing = _listing(tasks)

    assert listing.count("\n") == LISTED_TASKS
    assert listing.endswith(f"- … and {120 - LISTED_TASKS} more")


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


def test_a_long_list_of_dead_tasks_is_summarised() -> None:
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
    assert f"… and {40 - LISTED_DEAD} more" in sent["body"]


def test_a_healthy_finish_says_nothing_about_tasks() -> None:
    notifier = Notifier()
    sent = _capture_into(notifier)

    asyncio.run(notifier.run_finished("rutracker", tasks=1, downloaded=1, stored=1))

    assert "found nothing" not in sent["body"]
