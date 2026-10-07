"""Task history: what is remembered about a task's yield, and when it looks dead."""

from __future__ import annotations

from pathlib import Path

import pytest

from ricercar.config import SearchTask, SourceSettings
from ricercar.history import HISTORY_VERSION, TaskHistory, TaskYield, configured_task_keys
from ricercar.state import task_key


@pytest.fixture
def history(tmp_path: Path) -> TaskHistory:
    return TaskHistory(tmp_path / "task_history.json")


def _record(
    history: TaskHistory,
    key: str = "text=Bach|author=|category=",
    *,
    day: str = "2026-03-01",
    searches: int = 1,
    hits: int = 0,
    stored: int = 0,
    description: str = "Bach",
) -> TaskYield:
    return history.record(
        "rutracker",
        key,
        description,
        searches=searches,
        hits=hits,
        stored=stored,
        day=day,
    )


# ── What one run adds ─────────────────────────────────────────────────────


def test_one_run_is_counted_and_dated(history: TaskHistory) -> None:
    stat = _record(history, searches=28, hits=140, stored=2, day="2026-03-01")

    assert (stat.runs, stat.searches, stat.hits, stat.stored) == (1, 28, 140, 2)
    assert stat.first_run == stat.last_run == "2026-03-01"
    assert stat.last_hit == "2026-03-01"
    assert stat.last_stored == "2026-03-01"


def test_runs_accumulate_and_the_dates_follow(history: TaskHistory) -> None:
    _record(history, searches=28, hits=5, day="2026-03-01")
    stat = _record(history, searches=28, day="2026-03-02")

    assert stat.runs == 2
    assert stat.searches == 56
    assert stat.hits == 5
    assert stat.first_run == "2026-03-01"
    assert stat.last_run == "2026-03-02"
    # the last *hit* is what staleness turns on, so it must not move with a quiet run
    assert stat.last_hit == "2026-03-01"


def test_a_task_without_hits_never_claims_one(history: TaskHistory) -> None:
    stat = _record(history, searches=28)

    assert stat.hits == 0
    assert stat.last_hit is None
    assert stat.last_stored is None
    assert stat.days_without_hit("2026-03-10") is None


# ── When a task looks dead ────────────────────────────────────────────────


def test_a_fresh_task_is_not_judged(history: TaskHistory) -> None:
    # No history is not a bad history: a task that has barely run has not had a chance.
    for index in range(2):
        stat = _record(history, searches=28, day=f"2026-03-0{index + 1}")

    assert stat.runs == 2
    assert stat.stale(min_runs=5, min_days=60, day="2026-06-01") is False
    assert stat.verdict(min_runs=5, min_days=60, day="2026-06-01") == "ok"


def test_a_long_quiet_task_is_reported(history: TaskHistory) -> None:
    for day in ("2026-01-01", "2026-01-02", "2026-02-01", "2026-03-01", "2026-03-02"):
        stat = _record(history, searches=28, day=day)

    assert stat.runs == 5
    # young yet: five runs in six weeks is not enough evidence to call anything dead
    assert stat.stale(min_runs=5, min_days=60, day="2026-02-15") is False
    assert stat.verdict(min_runs=5, min_days=60, day="2026-02-15") == "ok"
    # …but once the same silence has lasted two months, the task has had its chance
    assert stat.stale(min_runs=5, min_days=60, day="2026-06-01") is True
    assert stat.verdict(min_runs=5, min_days=60, day="2026-06-01") == "remove?"


def test_one_recent_hit_keeps_a_task_alive(history: TaskHistory) -> None:
    for day in ("2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"):
        _record(history, searches=28, day=day)
    stat = _record(history, searches=28, hits=3, day="2026-05-30")

    assert stat.runs == 5
    assert stat.stale(min_runs=5, min_days=60, day="2026-06-01") is False
    assert stat.days_without_hit("2026-06-01") == 2


def test_a_task_that_only_finds_known_rows_is_reported_separately(history: TaskHistory) -> None:
    # Rows are coming back, so the task is not dead — but it has brought nothing new,
    # which is worth seeing (it may be redundant with another task).
    for day in ("2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"):
        stat = _record(history, searches=28, hits=50, day=day)

    assert stat.stale(min_runs=5, min_days=60, day="2026-06-01") is False
    assert stat.unrewarding(min_runs=5, min_days=60, day="2026-06-01") is True
    assert stat.verdict(min_runs=5, min_days=60, day="2026-06-01") == "nothing new"


def test_a_task_that_never_ran_is_not_stale() -> None:
    stat = TaskYield(key="text=ghost|author=|category=")

    assert stat.verdict(min_runs=5, min_days=60, day="2026-06-01") == "never run"
    assert stat.stale(min_runs=5, min_days=60, day="2026-06-01") is False


def test_the_stale_list_is_ordered_and_can_be_asked_for_all(history: TaskHistory) -> None:
    for day in ("2026-01-01", "2026-02-01", "2026-03-01"):
        _record(history, "text=quiet|author=|category=", searches=28, day=day)
        _record(history, "text=finds|author=|category=", searches=28, hits=9, day=day)
    # one of them found something this week, the other has not been around long enough
    _record(history, "text=finds|author=|category=", searches=28, hits=2, day="2026-05-30")
    for day in ("2026-05-01", "2026-05-02", "2026-05-03"):
        _record(history, "text=newer|author=|category=", searches=28, day=day)

    stale = history.stale("rutracker", min_runs=3, min_days=60, day="2026-06-01")

    assert [stat.key for stat in stale] == ["text=quiet|author=|category="]
    assert history.stale("rutracker", min_runs=9, min_days=60, day="2026-06-01") == []


# ── The file ──────────────────────────────────────────────────────────────


def test_the_file_survives_a_restart(history: TaskHistory, tmp_path: Path) -> None:
    _record(history, searches=28, hits=4, stored=1)
    history.save()

    reopened = TaskHistory(tmp_path / "task_history.json")

    assert reopened.yields("rutracker")["text=Bach|author=|category="].hits == 4
    assert reopened.names() == ["rutracker"]


def test_the_file_is_written_atomically(history: TaskHistory, tmp_path: Path) -> None:
    _record(history, searches=1)
    history.save()

    assert not (tmp_path / "task_history.json.tmp").exists()
    assert (tmp_path / "task_history.json").is_file()


def test_history_is_not_pruned_to_today(history: TaskHistory, tmp_path: Path) -> None:
    # The state file forgets yesterday because only today's runs matter; the whole point
    # here is the long view, so nothing is allowed to expire.
    _record(history, "text=old|author=|category=", searches=28, day="2020-01-01")
    _record(history, "text=recent|author=|category=", searches=28, day="2026-10-07")
    history.save()

    reopened = TaskHistory(tmp_path / "task_history.json")

    assert set(reopened.yields("rutracker")) == {
        "text=old|author=|category=",
        "text=recent|author=|category=",
    }


def test_an_unreadable_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "task_history.json"
    path.write_text("{ not json", encoding="utf-8")

    assert TaskHistory(path).load().yields("rutracker") == {}


def test_an_unknown_version_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "task_history.json"
    path.write_text('{"version": 99, "sources": {}}', encoding="utf-8")

    assert TaskHistory(path).load().names() == []


def test_the_version_is_written(tmp_path: Path) -> None:
    import json

    path = tmp_path / "task_history.json"
    history = TaskHistory(path)
    _record(history, searches=1)
    history.save()

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == HISTORY_VERSION


def test_a_missing_file_is_an_empty_history(tmp_path: Path) -> None:
    history = TaskHistory(tmp_path / "nothing-here.json")

    assert history.load().yields("rutracker") == {}


def test_in_memory_history_writes_nothing(tmp_path: Path) -> None:
    history = TaskHistory(None)
    _record(history, searches=1)

    assert history.yields("rutracker")["text=Bach|author=|category="].runs == 1
    history.save()
    assert list(tmp_path.iterdir()) == []


def test_forget_drops_the_tasks_that_are_gone(history: TaskHistory) -> None:
    _record(history, "text=kept|author=|category=", searches=1)
    _record(history, "text=gone|author=|category=", searches=1)

    assert history.forget("rutracker", ["text=gone|author=|category=", "text=unknown|x"]) == 1
    assert set(history.yields("rutracker")) == {"text=kept|author=|category="}


# ── Which tasks the config lists ──────────────────────────────────────────


def test_configured_keys_are_the_tasks_as_written() -> None:
    # Before expansion: a task stands for its searches, but the history is per task.
    settings = SourceSettings(
        categories={"A": 1, "B": 2},
        tasks=[SearchTask(author="someone")],
        must_complete_tasks=[SearchTask(category="*")],
    )

    assert configured_task_keys(settings) == {
        task_key(SearchTask(author="someone")),
        task_key(SearchTask(category="*")),
    }


# ── The report ────────────────────────────────────────────────────────────


def test_the_report_shows_the_worst_tasks_first(history: TaskHistory) -> None:
    from ricercar.cli import _select_tasks, _task_row

    for day in ("2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"):
        _record(history, "text=dead|author=|category=", searches=28, day=day, description="dead")
        _record(
            history,
            "text=known|author=|category=",
            searches=28,
            hits=40,
            day=day,
            description="known rows only",
        )
    _record(
        history,
        "text=alive|author=|category=",
        searches=28,
        hits=7,
        day="2026-05-30",
        description="alive",
    )
    _record(history, "text=idle|author=|category=", searches=28, description="never sampled")

    stats = _select_tasks(history, "rutracker", min_runs=5, min_days=60, day="2026-06-01")
    rows = [
        _task_row(
            stat,
            configured={"text=alive|author=|category="},
            min_runs=5,
            min_days=60,
            day="2026-06-01",
        )
        for stat in stats
    ]

    assert [row[0] for row in rows] == ["dead", "known rows only", "never sampled", "alive"]
    assert rows[0][-1] == "remove? (not in the config)"
    assert rows[1][-1] == "nothing new (not in the config)"
    assert rows[3][-1] == "ok"
    assert rows[0][:6] == ("dead", "5", "140", "0", "0", "never")


def test_the_report_can_be_asked_for_only_the_stale_ones(history: TaskHistory) -> None:
    from ricercar.cli import _select_tasks

    for day in ("2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"):
        _record(history, "text=dead|author=|category=", searches=28, day=day)
    _record(history, "text=alive|author=|category=", searches=28, hits=3, day="2026-05-30")

    stale_only = _select_tasks(
        history,
        "rutracker",
        min_runs=5,
        min_days=60,
        stale_only=True,
        day="2026-06-01",
    )

    assert [stat.key for stat in stale_only] == ["text=dead|author=|category="]
