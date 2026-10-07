"""What survives a run: the uploaders seen so far, and the tasks already finished."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ricercar.config import SearchTask
from ricercar.state import STATE_VERSION, SourceState, StateStore, task_key, today


@pytest.fixture
def file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def store(file: Path) -> StateStore:
    return StateStore(file)


def test_a_task_key_covers_the_whole_task() -> None:
    assert task_key(SearchTask()) == "text=|author=|category="
    assert task_key(SearchTask(text="Bach")) == "text=Bach|author=|category="
    assert (
        task_key(SearchTask(text="Bach", author="someone", category=5))
        == "text=Bach|author=someone|category=5"
    )


def test_today_is_the_local_date() -> None:
    assert today() == datetime.now().strftime("%Y-%m-%d")


# ── The uploaders ─────────────────────────────────────────────────────────


def test_only_the_new_uploaders_are_reported(store: StateStore) -> None:
    assert store.remember_authors("rutracker", ["a", "b"]) == {"a", "b"}
    assert store.remember_authors("rutracker", ["b", "c"]) == {"c"}
    assert store.remember_authors("rutracker", ["a", "b", "c"]) == set()  # nothing new

    assert store.known_authors("rutracker") == {"a", "b", "c"}


def test_an_empty_uploader_is_not_remembered(store: StateStore) -> None:
    assert store.remember_authors("rutracker", ["", "a"]) == {"a"}

    assert store.known_authors("rutracker") == {"a"}


# ── The completed tasks ───────────────────────────────────────────────────


def test_a_finished_task_is_remembered_for_today(store: StateStore) -> None:
    task = SearchTask(text="Bach")

    store.mark_completed("rutracker", task)

    assert store.completed_today("rutracker") == {task_key(task)}
    assert store.source("rutracker").last_run is not None


def test_a_task_finished_yesterday_does_not_count_as_done_today(store: StateStore) -> None:
    task = SearchTask(text="Bach")

    store.mark_completed("rutracker", task, when=_yesterday())

    assert store.completed_today("rutracker") == set()
    # …and it is dropped rather than kept around: only today's are ever consulted
    assert store.source("rutracker").completed == {}


def test_the_file_keeps_only_todays_finished_tasks(store: StateStore, file: Path) -> None:
    # A run can finish thousands of searches (every task in every configured category),
    # and only today's are ever consulted, so yesterday's are dropped when the file is
    # written rather than accumulating for ever.
    store.mark_completed("rutracker", SearchTask(text="today"))
    store.mark_completed("rutracker", SearchTask(text="yesterday"), when=_yesterday())

    store.save()

    payload = json.loads(file.read_text(encoding="utf-8"))
    kept = list(payload["sources"]["rutracker"]["completed"])

    assert kept == [task_key(SearchTask(text="today"))]


def test_many_tasks_do_not_grow_the_status_file_without_bound(
    store: StateStore, file: Path
) -> None:
    # mark_completed() writes the file every time, so pruning has to happen on each write
    # and not only at the end of a run.
    for index in range(50):
        store.mark_completed("rutracker", SearchTask(text=f"task {index}"))
        store.mark_completed("rutracker", SearchTask(text=f"old {index}"), when=_yesterday())

    payload = json.loads(file.read_text(encoding="utf-8"))
    completed = payload["sources"]["rutracker"]["completed"]

    assert len(completed) == 50
    assert all(key.startswith("text=task") for key in completed)


def test_the_state_survives_a_restart(store: StateStore, file: Path) -> None:
    task = SearchTask(text="Bach")
    store.remember_authors("rutracker", ["a"])
    store.mark_completed("rutracker", task)

    reopened = StateStore(file)

    assert reopened.known_authors("rutracker") == {"a"}
    assert reopened.completed_today("rutracker") == {task_key(task)}


def _yesterday() -> datetime:
    return datetime.now() - timedelta(days=1)


# ── The file ──────────────────────────────────────────────────────────────


def test_the_file_is_written_atomically(store: StateStore, file: Path, tmp_path: Path) -> None:
    store.remember_authors("rutracker", ["a"])
    store.save()

    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads(file.read_text(encoding="utf-8"))
    assert payload["version"] == STATE_VERSION
    assert payload["sources"]["rutracker"]["authors"] == ["a"]
    assert payload["sources"]["rutracker"]["completed"] == {}


def test_nothing_is_written_before_the_file_is_read(store: StateStore, file: Path) -> None:
    store.save()

    assert not file.exists()


def test_an_unreadable_file_is_ignored(store: StateStore, file: Path) -> None:
    file.write_text("{ this is not json", encoding="utf-8")

    assert store.known_authors("rutracker") == set()
    assert store.completed_today("rutracker") == set()


def test_a_file_from_another_version_is_ignored(store: StateStore, file: Path) -> None:
    file.write_text(
        json.dumps({"version": STATE_VERSION + 1, "sources": {"rutracker": {"authors": ["a"]}}}),
        encoding="utf-8",
    )

    assert store.known_authors("rutracker") == set()


def test_a_state_file_with_nonsense_in_it_is_ignored(store: StateStore, file: Path) -> None:
    file.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "sources": {"rutracker": {"authors": "nope", "completed": [], "last_run": 5}},
            }
        ),
        encoding="utf-8",
    )

    assert store.known_authors("rutracker") == set()
    assert store.completed_today("rutracker") == set()


def test_a_source_state_reads_itself_back() -> None:
    state = SourceState.from_json(
        {"authors": ["a", "a"], "completed": {"k": "2024-01-01"}, "last_run": "2024-01-01"}
    )

    assert state.authors == {"a"}
    assert state.completed == {"k": "2024-01-01"}
    assert state.last_run == "2024-01-01"
    assert SourceState.from_json(None).authors == set()
