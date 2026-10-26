"""The run loop: how a run plans its work, and one whole cycle driven offline.

The planning tests need no browser. The two ``integration`` tests drive a real one
over CDP, so they are skipped unless something answers on the configured endpoint —
start your browser as the README describes and they run too.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from rich.progress import Progress, TaskID

from ricercar.config import QuotaConfig, SearchTask, Settings, SourceSettings
from ricercar.history import TaskHistory
from ricercar.models import SearchHit, SearchPage, TorrentMetadata
from ricercar.parser.torrent import parse_file
from ricercar.pipeline import (
    PAUSE_TICK,
    PlannedSearch,
    Runner,
    RunOutcome,
    Shutdown,
    SourceOutcome,
    describe_task,
    run_sources,
    wait_between_pages,
)
from ricercar.progress import new_progress
from ricercar.repository import TorrentRepository
from ricercar.state import STATE_VERSION, task_key

ExtraConfig = Callable[[str, str], Settings]


class FakeSource:
    """A source that is only ever planned for — never driven.

    Its ``expand`` turns one configured task into as many searches as it is told to, which
    is what a tracker does when a task asks for every configured section. With the default
    of one it stands for a task that is already a single search and leaves it alone.
    """

    name = "fake"
    host = "fake.example"

    def __init__(self, settings: SourceSettings, *, per_task: int = 1) -> None:
        self.settings = settings
        self.per_task = per_task
        self.expanded = 0

    def expand(self, task: SearchTask) -> list[SearchTask]:
        self.expanded += 1
        if self.per_task == 1:
            return [task]
        return [task.model_copy(update={"category": index + 1}) for index in range(self.per_task)]


def _meta(url: str, md5: str) -> TorrentMetadata:
    return TorrentMetadata(url=url, name="Bach", size=1024, file_count=1, md5=md5)


def _hit(topic_id: str) -> SearchHit:
    return SearchHit(
        topic_id=topic_id,
        title=f"Bach {topic_id}",
        url=f"https://fake.example/t={topic_id}",
    )


def _settings(**kwargs: Any) -> SourceSettings:
    """Source settings for the planning tests: one section, so a task may name none."""
    kwargs.setdefault("categories", {"Only_Section": 1})
    return SourceSettings(**kwargs)


def _runner(cfg: Settings, source: FakeSource, **kwargs: object) -> Runner:
    return Runner(cfg, sources=[source], **kwargs)


def _budget(runner: Runner, source: FakeSource) -> int:
    with runner.repo:
        return runner._budget(source)  # planning is what is under test


def _tasks(runner: Runner, source: FakeSource) -> list[SearchTask]:
    """The searches a run would perform — planning is what is under test."""
    return [plan.task for plan in runner._tasks_for(source)]


def _plans(runner: Runner, source: FakeSource) -> list[PlannedSearch]:
    """The same, with the configured task each search came from."""
    return runner._tasks_for(source)


def test_task_reviews_include_unsampled_tasks_and_exclude_old_config(run_config: Settings) -> None:
    tasks = [SearchTask(text="Bach"), SearchTask(text="Mozart")]
    source = FakeSource(_settings(tasks=tasks))
    runner = _runner(run_config, source)
    runner.history.record(
        "fake", task_key(SearchTask(text="removed")), "removed", searches=1, hits=0, stored=0
    )
    outcome = SourceOutcome(source="fake", task_status={task_key(tasks[0]): "completed"})
    reviews = runner._task_reviews(source, outcome)
    assert [(task.text, status) for task, _, status in reviews] == [
        ("Bach", "completed"),
        ("Mozart", "not run"),
    ]


@pytest.mark.parametrize("category", [None, 1])
def test_resumed_searches_keep_their_configured_history_key(
    run_config: Settings,
    category: int | None,
) -> None:
    task = SearchTask(text="Bach", category=category)
    source = FakeSource(_settings(tasks=[task]), per_task=2)
    runner = _runner(run_config, source)
    runner.state.mark_allocated(source.name, {task_key(item) for item in source.expand(task)})
    plans = _plans(runner, source)
    assert {plan.origin for plan in plans} == {task_key(task)}
    assert {plan.label for plan in plans} == {describe_task(task)}


@pytest.mark.parametrize("days_ago", [0, 1])
def test_resume_keeps_only_unfinished_work(
    run_config: Settings, monkeypatch: pytest.MonkeyPatch, days_ago: int
) -> None:
    previous = datetime.now() - timedelta(days=days_ago)
    current_day = [previous.date().isoformat()]
    monkeypatch.setattr("ricercar.state.today", lambda: current_day[0])
    completed = SearchTask(text="completed yesterday")
    pending = SearchTask(text="still pending")
    fresh = SearchTask(text="newly configured")
    source = FakeSource(_settings(tasks=[fresh], must_complete_tasks=[fresh]))
    runner = _runner(run_config, source)
    runner.state.mark_allocated(source.name, {task_key(completed), task_key(pending)})
    runner.state.mark_completed(source.name, completed, when=previous)

    current_day[0] = date.today().isoformat()
    restarted = _runner(run_config, source)
    assert _tasks(restarted, source) == [pending]
    restarted.state.save()
    assert _tasks(_runner(run_config, source), source) == [pending]
    restarted.state.mark_completed(source.name, pending)
    assert _tasks(_runner(run_config, source), source) == [fresh]


@pytest.mark.parametrize("second", ["not run", "failed", "completed"])
def test_review_requires_all_categories_to_complete(run_config: Settings, second: str) -> None:
    task = SearchTask(text="Bach")
    source = FakeSource(_settings(tasks=[task]), per_task=2)
    runner = _runner(run_config, source)
    expanded = source.expand(task)
    outcome = SourceOutcome(
        source="fake",
        task_status={
            task_key(expanded[0]): "completed",
            task_key(expanded[1]): second,
        },
    )
    assert (
        runner._task_reviews(source, outcome)[0][2]
        == {
            "not run": "incomplete",
            "failed": "failed",
            "completed": "completed",
        }[second]
    )


def test_candidates_exclude_covered_authors_but_keep_uncovered_sections(
    run_config: Settings,
) -> None:
    source = FakeSource(
        _settings(
            tasks=[
                SearchTask(author="covered"),
                SearchTask(author="section", category=1),
                SearchTask(author="filtered", text="Bach"),
                SearchTask(text="Mozart"),
            ]
        )
    )
    runner = _runner(run_config, source)
    for author, section in [("COVERED", 1), ("section", 1), ("filtered", 1), ("new", 1)]:
        runner.discovered.record("fake", author, section=section)
    runner.discovered.record("other", "foreign", section=1)
    assert {u.author for u in runner._candidate_uploaders(source)} == {"filtered", "new"}
    runner.discovered.record("fake", "section", section=2)
    assert {u.author for u in runner._candidate_uploaders(source)} == {"section", "filtered", "new"}


@pytest.mark.parametrize("result", ["empty", "partial", "timeout", "download failure"])
def test_drive_distinguishes_empty_success_from_incomplete_or_failed_searches(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from playwright.async_api import TimeoutError as PlaywrightTimeout

    task = SearchTask(text="Bach")
    source = FakeSource(_settings(tasks=[task]))
    runner = _runner(run_config, source)
    session = MagicMock()
    session.context.new_page = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(runner, "_prepare", AsyncMock())
    monkeypatch.setattr(runner, "_diagnose", AsyncMock(return_value=()))
    monkeypatch.setattr(runner.recorder, "capture", AsyncMock())
    outcome = SourceOutcome(source="fake")

    async def run_task(*_args: object) -> tuple[bool, list[str]]:
        if result == "timeout":
            raise PlaywrightTimeout("timeout")
        if result == "download failure":
            outcome.failures += 1
        return result != "partial", []

    monkeypatch.setattr(runner, "_run_task", run_task)
    with new_progress() as progress, runner.repo:
        _, results = asyncio.run(
            runner._drive(
                source,
                session,
                _plans(runner, source),
                10,
                outcome,
                progress,
            )
        )
    assert (
        outcome.task_status[task_key(task)]
        == {
            "empty": "completed",
            "partial": "incomplete",
            "timeout": "failed",
            "download failure": "failed",
        }[result]
    )
    history = runner.history.yields("fake")
    if result == "empty":
        assert results == {"Bach": []}
        assert history[task_key(task)].runs == 1
    else:
        assert results == {}
        assert task_key(task) not in history


def test_drive_combines_downloads_from_all_categories(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    task = SearchTask(text="Bach")
    source = FakeSource(_settings(tasks=[task]), per_task=2)
    runner = _runner(run_config, source)
    session = MagicMock()
    session.context.new_page = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(runner, "_prepare", AsyncMock())
    monkeypatch.setattr(
        runner,
        "_run_task",
        AsyncMock(
            side_effect=[(True, ["First torrent"]), (True, ["Second torrent"])],
        ),
    )
    with new_progress() as progress, runner.repo:
        _, results = asyncio.run(
            runner._drive(
                source,
                session,
                _plans(runner, source),
                10,
                SourceOutcome(source="fake"),
                progress,
            )
        )
    assert results == {"Bach": ["First torrent", "Second torrent"]}
    assert runner.history.yields("fake")[task_key(task)].searches == 2


def test_failed_category_keeps_positive_history_without_successful_run(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from playwright.async_api import TimeoutError as PlaywrightTimeout

    task = SearchTask(text="Bach")
    source = FakeSource(_settings(tasks=[task]), per_task=2)
    runner = _runner(run_config, source)
    session = MagicMock()
    session.context.new_page = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(runner, "_prepare", AsyncMock())
    monkeypatch.setattr(runner, "_diagnose", AsyncMock(return_value=()))
    monkeypatch.setattr(runner.recorder, "capture", AsyncMock())
    outcome = SourceOutcome(source="fake")

    async def run_task(
        _source: object, plan: PlannedSearch, *_args: object
    ) -> tuple[bool, list[str]]:
        if plan.task.category == 2:
            raise PlaywrightTimeout("timeout")
        outcome.hits += 10
        outcome.stored += 1
        return True, ["Cantatas"]

    monkeypatch.setattr(runner, "_run_task", run_task)
    with new_progress() as progress, runner.repo:
        asyncio.run(
            runner._drive(
                source,
                session,
                _plans(runner, source),
                10,
                outcome,
                progress,
            )
        )
    stat = runner.history.yields("fake")[task_key(task)]
    assert stat.runs == 0
    assert stat.stored == 1
    assert stat.hits == 10
    assert stat.last_stored is not None
    assert stat.last_run is None
    assert runner._task_reviews(source, outcome)[0][2] == "failed"


def test_search_updates_known_uploader_observations(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    source = FakeSource(_settings(tasks=[SearchTask(text="Bach", category=1)]))
    runner = _runner(run_config, source)
    runner.state.remember_authors("fake", ["collector"])
    runner.discovered.record("fake", "collector", section=2, now="2020-01-01T00:00:00")

    async def search(*_args: object, **_kwargs: object) -> AsyncIterator[SearchPage]:
        yield SearchPage(
            hits=[
                SearchHit(
                    topic_id="1",
                    title="Bach",
                    url="https://fake.example/t=1",
                    author="collector",
                )
            ],
            number=1,
            total=1,
        )

    monkeypatch.setattr(source, "search", search, raising=False)
    monkeypatch.setattr(runner, "_pace", AsyncMock())
    monkeypatch.setattr(runner, "_download_hit", AsyncMock(return_value=None))
    outcome = SourceOutcome(source="fake")
    with runner.repo, new_progress() as progress:
        bar = progress.add_task("torrents", total=10)
        asyncio.run(
            runner._run_task(
                source,
                _plans(runner, source)[0],
                AsyncMock(),
                AsyncMock(),
                10,
                outcome,
                progress,
                bar,
            )
        )
    uploader = runner.discovered.unconfirmed("fake")[0]
    assert uploader.hits == 2
    assert set(uploader.sections_seen) == {1, 2}
    assert uploader.last_seen != "2020-01-01T00:00:00"
    assert outcome.new_authors == []


def test_quota_limited_downloads_leave_task_incomplete(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    source = FakeSource(_settings(tasks=[SearchTask(text="Bach", category=1)]))
    runner = _runner(run_config, source)

    async def search(*_args: object, **_kwargs: object) -> AsyncIterator[SearchPage]:
        yield SearchPage(hits=[_hit("one"), _hit("two")], number=1, total=1)

    monkeypatch.setattr(source, "search", search, raising=False)
    monkeypatch.setattr(runner, "_pace", AsyncMock())
    monkeypatch.setattr(runner, "_download_hit", AsyncMock(return_value="Bach"))
    with runner.repo, new_progress() as progress:
        bar = progress.add_task("torrents", total=1)
        finished, names = asyncio.run(
            runner._run_task(
                source,
                _plans(runner, source)[0],
                AsyncMock(),
                AsyncMock(),
                1,
                SourceOutcome(source="fake"),
                progress,
                bar,
            )
        )
    assert not finished
    assert names == ["Bach"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("login", "Login failed"),
        ("network", "Browser/network navigation failed"),
        ("browser", "Browser connection or run setup failed"),
    ],
)
def test_source_failures_are_reported_in_finish_email(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import apprise
    from playwright.async_api import Error as PlaywrightError

    from ricercar.notify import Notifier
    from ricercar.sources.base import SessionExpiredError

    errors = {
        "login": SessionExpiredError("no session"),
        "network": PlaywrightError("network unavailable"),
        "browser": RuntimeError("no browser"),
    }
    manager = MagicMock()
    manager.__aenter__.side_effect = errors[failure]
    monkeypatch.setattr("ricercar.pipeline.managed_browser", lambda *_args: manager)
    notifier = Notifier()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send", send)
    source = FakeSource(_settings(tasks=[SearchTask(text="Bach")], random_choice=1))
    runner = _runner(run_config, source, notifier=notifier)
    with runner.repo, new_progress() as progress:
        outcome = asyncio.run(runner._run_source(source, progress))

    assert outcome.aborted
    assert outcome.failures == 1
    body = send.call_args.args[1]
    assert "## Run Issues" in body
    assert expected in body
    assert "zero downloads does not mean" in body
    assert send.call_args.kwargs["notify_type"] == apprise.NotifyType.WARNING


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def run_config(extra_config: ExtraConfig, tmp_path: Path) -> Settings:
    """Settings whose database, state file and failures land under ``tmp_path``."""
    return extra_config(
        "run.yml",
        "\n".join(
            [
                "database:",
                f"  path: {(tmp_path / 'torrents.db').as_posix()}",
                "browser:",
                f"  downloads_dir: {(tmp_path / 'torrents').as_posix()}",
                "run:",
                f"  state_file: {(tmp_path / 'state.json').as_posix()}",
                f"  task_history_file: {(tmp_path / 'task_history.json').as_posix()}",
                f"  discovered_uploaders_file: {(tmp_path / 'discovered.json').as_posix()}",
                "  stale_task_runs: 2",
                "  stale_task_days: 30",
                "diagnostics:",
                f"  failures_dir: {(tmp_path / 'failures').as_posix()}",
                "  save_traces: true",
                "sources:",
                "  rutracker:",
                "    categories:",
                "      First_Section: 1",
                "      Second_Section: 2",
                "    tasks:",
                "      - text: Bach",
                "    must_complete_tasks: []",
                "    random_choice: 1",
                "    max_pages: 1",
                # The suite reads the developer's own config.local.yml on top of the
                # committed defaults, and a page delay there would make every test that
                # stores something wait it out. Tests that are *about* the delay layer
                # their own value on top of this (see the `paced` fixture).
                "    quota:",
                "      page_delay_seconds: 0",
                "",
            ]
        ),
    )


# ── Planning ──────────────────────────────────────────────────────────────


def test_tasks_are_the_mandatory_ones_plus_a_random_sample(run_config: Settings) -> None:
    source = FakeSource(
        _settings(
            must_complete_tasks=[SearchTask(text="always")],
            tasks=[SearchTask(text="a"), SearchTask(text="b"), SearchTask(text="c")],
            random_choice=2,
        )
    )

    tasks = _tasks(_runner(run_config, source), source)

    assert tasks[0] == SearchTask(text="always")
    assert len(tasks) == 3
    assert {task.text for task in tasks} <= {"always", "a", "b", "c"}


def test_a_task_that_is_mandatory_and_optional_runs_once(run_config: Settings) -> None:
    task = SearchTask(text="Bach")
    source = FakeSource(_settings(must_complete_tasks=[task], tasks=[task], random_choice=1))

    assert _tasks(_runner(run_config, source), source) == [task]


def test_a_task_can_stand_for_several_searches(run_config: Settings) -> None:
    source = FakeSource(
        SourceSettings(tasks=[SearchTask(text="Bach", category=1)], random_choice=1),
        per_task=3,
    )

    tasks = _tasks(_runner(run_config, source), source)

    assert len(tasks) == 3
    assert [task.category for task in tasks] == [1, 2, 3]
    assert {task.text for task in tasks} == {"Bach"}
    assert source.expanded == 1, "the expansion happens once per configured task"


def test_every_search_knows_the_task_it_came_from(run_config: Settings) -> None:
    # The history is per configured task — the line you would delete from the config —
    # while progress and the quota count the searches it turned into.
    source = FakeSource(
        _settings(tasks=[SearchTask(author="someone")], random_choice=1),
        per_task=2,
    )

    plans = _plans(_runner(run_config, source), source)

    assert [plan.task.category for plan in plans] == [1, 2]
    assert {plan.origin for plan in plans} == {task_key(SearchTask(author="someone"))}
    assert {plan.label for plan in plans} == {"(everything) by someone"}
    assert [plan.key for plan in plans] == [
        task_key(SearchTask(author="someone", category=1)),
        task_key(SearchTask(author="someone", category=2)),
    ]


def test_a_shared_search_belongs_to_the_task_that_asked_first(run_config: Settings) -> None:
    source = FakeSource(
        _settings(
            must_complete_tasks=[SearchTask(author="someone")],
            tasks=[SearchTask(author="someone", category=2)],
            random_choice=1,
        ),
        per_task=3,
    )

    plans = _plans(_runner(run_config, source), source)

    assert [plan.task.category for plan in plans] == [1, 2, 3]
    assert {plan.origin for plan in plans} == {task_key(SearchTask(author="someone"))}


def test_overlapping_expansions_are_collapsed(run_config: Settings) -> None:
    # One task for "every section" and one for a section inside it: the searches they
    # share run once.
    source = FakeSource(
        _settings(
            must_complete_tasks=[SearchTask(text="Bach")],
            tasks=[SearchTask(text="Bach", category=2)],
            random_choice=1,
        ),
        per_task=2,
    )

    tasks = _tasks(_runner(run_config, source), source)

    assert sorted(task.category for task in tasks) == [1, 2]


def test_resuming_skips_the_individual_searches_already_done(run_config: Settings) -> None:
    source = FakeSource(
        SourceSettings(tasks=[SearchTask(text="Bach", category=1)], random_choice=1),
        per_task=3,
    )
    runner = _runner(run_config, source)
    runner.state.mark_completed(source.name, SearchTask(text="Bach", category=1))
    runner.state.mark_completed(source.name, SearchTask(text="Bach", category=3))

    remaining = _tasks(runner, source)

    assert [task.category for task in remaining] == [2]


def test_resuming_skips_what_was_already_done_today(run_config: Settings) -> None:
    source = FakeSource(
        _settings(tasks=[SearchTask(text="a"), SearchTask(text="b")], random_choice=2)
    )
    runner = _runner(run_config, source)
    runner.state.mark_completed(source.name, SearchTask(text="a"))

    remaining = _tasks(runner, source)

    assert [task.text for task in remaining] == ["b"]


def test_without_resuming_every_task_runs_again(run_config: Settings) -> None:
    source = FakeSource(
        _settings(tasks=[SearchTask(text="a"), SearchTask(text="b")], random_choice=2)
    )
    runner = _runner(run_config, source, resume=False)
    runner.state.mark_completed(source.name, SearchTask(text="a"))

    assert len(_tasks(runner, source)) == 2


def test_the_budget_is_the_quota_or_the_limit_whichever_is_smaller(run_config: Settings) -> None:
    source = FakeSource(SourceSettings(quota=QuotaConfig(limit_torrents_one_day=5)))

    assert _budget(_runner(run_config, source), source) == 5
    assert _budget(_runner(run_config, source, limit=2), source) == 2


def test_the_budget_subtracts_what_this_tracker_already_gave_today(
    run_config: Settings,
) -> None:
    source = FakeSource(SourceSettings(quota=QuotaConfig(limit_torrents_one_day=5)))
    with TorrentRepository(run_config.db_path) as repo:
        repo.add(_meta("https://fake.example/viewtopic.php?t=1", "a" * 32), b"x")
        repo.add(_meta("https://elsewhere.example/viewtopic.php?t=2", "b" * 32), b"y")

    assert _budget(_runner(run_config, source), source) == 4


# ── Skipping what is already stored, and pacing what is not ───────────────

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ReplaySource:
    """A source that answers one task with pages of hits it was given.

    As much of the ``Source`` protocol as a task needs — which rows are skipped, and how
    fast the pages are walked — with no browser and no network: the settings it reads, a
    ``search`` that yields the pages, and a ``fetch_torrent`` that hands over a real
    saved ``.torrent`` so a store can genuinely happen.
    """

    name = "fake"
    host = "fake.example"

    def __init__(
        self,
        settings: SourceSettings,
        pages: list[list[SearchHit]],
        *,
        total: int | None = None,
    ) -> None:
        self.settings = settings
        self.pages = pages
        self.total = total
        """What the pager says; ``None`` stands for a source that does not say."""
        self.calls: list[tuple[object, SearchTask, int]] = []
        self.fetched: list[tuple[object, str]] = []
        self.logins: list[object] = []

    def expand(self, task: SearchTask) -> list[SearchTask]:
        return [task]

    async def ensure_logged_in(self, page: object) -> None:
        self.logins.append(page)

    def diagnose(self, _html: str) -> tuple[str, ...]:
        return ()

    def fixture_routes(self) -> dict[str, str]:
        return {"**/tracker.php*": "html/search/results.html"}

    async def search(
        self, page: object, task: SearchTask, *, max_pages: int
    ) -> AsyncIterator[SearchPage]:
        self.calls.append((page, task, max_pages))
        for number, hits in enumerate(self.pages, start=1):
            yield SearchPage(hits=list(hits), number=number, total=self.total)

    async def fetch_torrent(self, page: object, hit: SearchHit, directory: object) -> Any:
        """Hand over the saved fixture, as if it had been downloaded from *hit*."""
        from ricercar.models import TopicInfo

        self.fetched.append((page, hit.url))
        path = Path(str(directory)) / f"{hit.topic_id}.torrent"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((FIXTURES / "torrents" / "sample.torrent").read_bytes())
        return TopicInfo(url=hit.url, title=hit.title, downloadable=True), path


def _one_page(runner: Runner, source: ReplaySource, *, budget: int = 10) -> SourceOutcome:
    """Run one search through the runner and report what it made of the hits.

    The pages are ``None``: this source fetches nothing itself, so there is nothing to
    hand it.
    """
    outcome = SourceOutcome(source=source.name)
    plan = PlannedSearch(task=SearchTask(text="Bach"), origin="text: bach", label="text: Bach")
    with new_progress() as progress, runner.repo:
        asyncio.run(
            runner._run_task(
                source,
                plan,
                None,
                None,
                budget,
                outcome,
                progress,
                progress.add_task("torrents"),
            )
        )
    return outcome


@pytest.fixture
def browser_run(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Runner, ReplaySource, Any, list[Any]]:
    from unittest.mock import AsyncMock, MagicMock

    from ricercar.notify import Notifier

    source = ReplaySource(
        _settings(
            tasks=[SearchTask(text="Bach", category=1)],
            random_choice=1,
            quota=QuotaConfig(delay_between_actions=0, page_delay_seconds=0),
        ),
        [[_hit("one")]],
    )
    notifier = Notifier()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send", send)
    runner = _runner(run_config, source, notifier=notifier)
    pages = [AsyncMock(), AsyncMock(), AsyncMock()]
    session = MagicMock()
    session.context.new_page = AsyncMock(side_effect=pages)
    manager = MagicMock()
    manager.__aenter__.return_value = session
    monkeypatch.setattr("ricercar.pipeline.managed_browser", lambda *_args: manager)
    monkeypatch.setattr(
        "ricercar.sources.rutracker.categories.fetch_categories",
        AsyncMock(return_value={1: "section"}),
    )
    monkeypatch.setattr("ricercar.categories.load_snapshot", lambda: {1: "section"})
    monkeypatch.setattr(runner.recorder, "start", AsyncMock())
    monkeypatch.setattr(runner.recorder, "capture", AsyncMock())
    return runner, source, send, pages


@pytest.mark.parametrize("elapsed_hours", [0.5, 1.0, 2.0])
def test_temporary_artifacts_rotate_only_between_tasks_after_configured_interval(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    elapsed_hours: float,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    runner, source, _, pages = browser_run
    runner.cfg.browser.artifact_rotation_hours = 1
    source.settings.tasks.append(SearchTask(text="Mozart", category=1))
    source.settings.random_choice = 2
    source.pages = [[]]
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr("ricercar.pipeline.time", SimpleNamespace(monotonic=lambda: clock.now))
    original_run_task = runner._run_task
    session = MagicMock()
    session.context.new_page = AsyncMock(side_effect=pages)
    session.reconnect = AsyncMock()
    replacement_pages = [AsyncMock(), AsyncMock()]

    async def reconnect(*_args: object) -> None:
        runner.recorder.stop.assert_awaited_once()
        for page in pages[1:]:
            page.close.assert_awaited_once()
        session.context.new_page = AsyncMock(side_effect=replacement_pages)

    session.reconnect.side_effect = reconnect
    manager = MagicMock()
    manager.__aenter__.return_value = session
    monkeypatch.setattr("ricercar.pipeline.managed_browser", lambda *_args: manager)
    monkeypatch.setattr(runner.recorder, "stop", AsyncMock())

    async def run_task(*args: Any, **kwargs: Any) -> Any:
        result = await original_run_task(*args, **kwargs)
        clock.now += elapsed_hours * 3600
        return result

    monkeypatch.setattr(runner, "_run_task", run_task)
    outcome = asyncio.run(runner.run()).sources[0]
    assert outcome.done == 2
    assert outcome.failures == 0
    if elapsed_hours >= 1:
        session.reconnect.assert_awaited_once_with(source.settings.connect_url, runner.cfg)
        assert runner.recorder.start.await_count == 2
        assert runner.recorder.stop.await_count == 2
        for page in replacement_pages:
            page.close.assert_awaited_once()
        assert source.calls[0][0] is pages[1]
        assert source.calls[1][0] is replacement_pages[0]
    else:
        session.reconnect.assert_not_awaited()
        assert runner.recorder.start.await_count == 1
        assert runner.recorder.stop.await_count == 1


def test_whole_run_searches_stores_notifies_and_persists_without_browser(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
) -> None:
    runner, source, send, pages = browser_run
    outcome = asyncio.run(runner.run())
    assert outcome.stored == 1
    assert outcome.failures == 0
    assert outcome.sources[0].done == 1
    assert len(source.logins) == 2
    assert len(source.fetched) == 1
    assert send.await_count == 2
    assert "Going to start" in send.call_args_list[0].args[0]
    assert "Stored 1 new torrent(s) from 1 download attempt(s)" in send.call_args_list[1].args[1]
    assert "Task Adjustment Suggestions" in send.call_args_list[1].args[1]
    for page in pages:
        page.close.assert_awaited_once()
    assert runner.shutdown._previous == {}
    state = json.loads(Path(runner.cfg.run.state_file).read_text())
    assert state["sources"][source.name]
    assert Path(runner.cfg.run.task_history_file).is_file()
    assert Path(runner.cfg.run.discovered_uploaders_file).is_file()
    second = asyncio.run(runner.run())
    assert second.sources[0].skipped == "no tasks to run"
    assert send.await_count == 2


@pytest.mark.parametrize(
    ("limit", "expire_previous", "expected"),
    [(None, True, 2), (1, True, 1), (2, True, 2), (None, False, 1), (2, False, 1)],
)
def test_task_start_rechecks_rolling_quota_without_resetting_run_limit(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
    expire_previous: bool,
    expected: int,
) -> None:
    import sqlite3
    from unittest.mock import Mock

    runner, source, send, _ = browser_run
    runner.limit = limit
    source.settings.quota.limit_torrents_one_day = 2
    source.settings.tasks = []
    source.settings.must_complete_tasks = [
        SearchTask(text=name, category=1) for name in ("First", "Second")
    ]
    previous_url = "https://fake.example/viewtopic.php?t=previous"
    with TorrentRepository(runner.cfg.db_path) as repo:
        repo.add(_meta(previous_url, "a" * 32), b"previous")
    with sqlite3.connect(runner.cfg.db_path) as connection:
        connection.execute(
            "UPDATE url_table SET add_date = datetime('now','localtime','-23 hours') WHERE url = ?",
            (previous_url,),
        )
    allowances: list[int] = []
    queries = Mock(wraps=runner.repo.count_today_from)
    monkeypatch.setattr(runner.repo, "count_today_from", queries)

    async def run_task(
        _source: Any,
        plan: Any,
        _results: Any,
        _topic: Any,
        budget: int,
        outcome: SourceOutcome,
        *_args: Any,
    ) -> tuple[bool, list[str]]:
        allowances.append(budget)
        name = plan.task.text
        meta = _meta(f"https://fake.example/viewtopic.php?t={name}", str(len(allowances)) * 32)
        assert runner.repo.add(meta, name.encode())
        outcome.stored += 1
        if expire_previous:
            runner.repo._db.execute(
                "UPDATE url_table SET add_date = datetime('now','localtime','-25 hours') "
                "WHERE url = ?",
                (previous_url,),
            )
            runner.repo._db.commit()
        return True, [name]

    monkeypatch.setattr(runner, "_run_task", run_task)

    outcome = asyncio.run(runner.run())

    assert outcome.stored == expected
    assert outcome.sources[0].done == expected
    assert allowances == [1] * expected
    assert queries.call_count == 3
    assert outcome.sources[0].budget == expected
    assert (
        f"Download allowance: up to {expected} new torrent(s); room for 0 more."
        in send.call_args_list[-1].args[1]
    )


@pytest.mark.parametrize(("limit", "expected"), [(None, 4), (2, 2), (20, 4)])
def test_start_email_uses_remaining_daily_quota_capped_by_run_limit(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    limit: int | None,
    expected: int,
) -> None:
    runner, source, send, _ = browser_run
    source.settings.quota.limit_torrents_one_day = 5
    runner.limit = limit
    with TorrentRepository(runner.cfg.db_path) as repo:
        repo.add(_meta("https://fake.example/viewtopic.php?t=previous", "a" * 32), b"x")

    outcome = asyncio.run(runner.run())

    assert outcome.failures == 0
    assert f", up to {expected} torrent(s) at " in send.call_args_list[0].args[0]


def test_whole_run_explains_every_download_attempt_not_stored_in_mail_and_warning_log(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from ricercar.log import get_logger
    from ricercar.sources.base import TorrentUnavailableError

    runner, source, send, _ = browser_run
    source.pages = [[_hit("stored"), _hit("duplicate"), _hit("unavailable")]]
    original_fetch = source.fetch_torrent
    warning = Mock(wraps=get_logger().warning)
    monkeypatch.setattr(get_logger(), "warning", warning)

    async def fetch(page: object, hit: SearchHit, directory: object) -> Any:
        if hit.topic_id == "unavailable":
            raise TorrentUnavailableError("torrent removed by its uploader")
        return await original_fetch(page, hit, directory)

    monkeypatch.setattr(source, "fetch_torrent", fetch)

    outcome = asyncio.run(runner.run()).sources[0]

    assert outcome.downloaded == 3
    assert outcome.stored == 1
    assert len(outcome.not_stored_details) == outcome.downloaded - outcome.stored == 2
    body = send.call_args.args[1]
    assert "Stored 1 new torrent(s) from 3 download attempt(s)" in body
    assert "2 attempt(s) did not add a new torrent" in body
    assert "duplicate content" in body
    assert "torrent removed by its uploader" in body
    for detail in outcome.not_stored_details:
        assert detail in body
        warning.assert_any_call("Not stored: {}", detail)


@pytest.mark.parametrize("text", ["Bach", ""])
def test_periodic_mail_arrives_while_a_search_is_still_running(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    from types import SimpleNamespace

    from ricercar.notify.progress import ProgressReporter

    runner, source, send, _ = browser_run
    source.settings.tasks = []
    source.settings.must_complete_tasks = [SearchTask(text=text, author="someone")]
    source.settings.categories = {"First_Section": 1, "Second_Section": 2}
    monkeypatch.setattr(
        source,
        "expand",
        lambda task: [
            task.model_copy(update={"category": 1}),
            task.model_copy(update={"category": 2}),
        ],
    )
    settings = runner.cfg.model_copy(update={"sources": {source.name: source.settings}})
    monkeypatch.setattr("ricercar.notify.get_settings", lambda: settings)
    monkeypatch.setattr(type(runner.notifier), "enabled", property(lambda _self: True))
    clock = SimpleNamespace(now=0.0)
    reporters: list[ProgressReporter] = []

    def create(*args: Any, **kwargs: Any) -> ProgressReporter:
        reporter = ProgressReporter(*args, **kwargs, now=lambda: clock.now)
        reporters.append(reporter)
        return reporter

    monkeypatch.setattr("ricercar.pipeline.ProgressReporter", create)

    async def run() -> Any:
        received = asyncio.Event()

        async def delivered(title: str, _body: str, **_kwargs: Any) -> bool:
            if "Running" in title:
                received.set()
            return True

        async def search(
            _page: object, task: SearchTask, **_kwargs: Any
        ) -> AsyncIterator[SearchPage]:
            if task.category == 2:
                clock.now = 3600
                reporters[0]._wake.set()
                await asyncio.wait_for(received.wait(), timeout=2)
                yield SearchPage(hits=[], number=1)
            else:
                yield SearchPage(hits=[_hit("one")], number=1)

        send.side_effect = delivered
        monkeypatch.setattr(source, "search", search)
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.stored == 1
    assert outcome.sources[0].done == 2
    assert send.await_count == 3
    start_body = send.call_args_list[0].args[1]
    for category in ("First_Section", "Second_Section"):
        assert f"{text or '(no text)'} [author=someone, category={category}]" in start_body
    body = send.call_args_list[1].args[1]
    assert "1 task(s) completed; 1 download attempt(s); 1 new torrent(s) stored" in body
    task_label = f"{text or '(everything)'} by someone"
    assert f"Current task: {task_label} in category Second_Section" in body
    assert f"- {task_label} in category First_Section" in body
    assert "in category 1" not in body
    assert "in category 2" not in body
    assert f"### {task_label} (1 torrent(s))" in send.call_args_list[2].args[1]
    assert outcome.sources[0].stored_torrents[0] in body
    assert reporters[0]._task is None


def test_progress_task_is_stopped_when_source_setup_raises(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ricercar.notify.progress import ProgressReporter

    runner, _, _, _ = browser_run
    monkeypatch.setattr(type(runner.notifier), "enabled", property(lambda _self: True))
    reporters: list[ProgressReporter] = []

    def create(*args: Any, **kwargs: Any) -> ProgressReporter:
        reporter = ProgressReporter(*args, **kwargs)
        reporters.append(reporter)
        return reporter

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("unexpected setup error")

    monkeypatch.setattr("ricercar.pipeline.ProgressReporter", create)
    monkeypatch.setattr(runner, "_prepare", fail)
    with pytest.raises(ValueError, match="unexpected setup error"):
        asyncio.run(runner.run())
    assert len(reporters) == 1
    assert reporters[0]._task is None
    assert reporters[0]._token is None


def test_partial_category_success_then_login_failure_is_not_reported_as_empty_success(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ricercar.sources.base import SessionExpiredError

    runner, source, send, _ = browser_run
    task = SearchTask(text="Bach")
    source.settings.tasks = [task]
    monkeypatch.setattr(
        source,
        "expand",
        lambda task: [
            task.model_copy(update={"category": 1}),
            task.model_copy(update={"category": 2}),
        ],
    )

    async def search(
        _page: object, task: SearchTask, **_kwargs: object
    ) -> AsyncIterator[SearchPage]:
        if task.category == 2:
            raise SessionExpiredError("expired")
        yield SearchPage(hits=[], number=1)

    monkeypatch.setattr(source, "search", search)
    outcome = asyncio.run(runner.run()).sources[0]
    assert outcome.aborted
    assert outcome.done == 1
    assert outcome.task_results == {}
    assert sorted(outcome.task_status.values()) == ["blocked", "completed"]
    assert runner.history.yields(source.name)[task_key(task)].runs == 0
    body = send.call_args.args[1]
    assert "Needs Attention" in body
    assert "task(s) yielded nothing" not in body
    assert "Tasks With No Download This Run" not in body


def test_offline_preparation_refuses_unknown_requests_and_serves_fixtures_without_login(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    runner, source, _, pages = browser_run
    runner.offline = FIXTURES
    monkeypatch.setattr(
        source,
        "fixture_routes",
        lambda: {
            "**/tracker.php*": "html/search/results.html",
            "**/dl.php*": "torrents/sample.torrent",
        },
    )
    session = MagicMock()
    asyncio.run(runner._prepare(source, session, pages[1], pages[2]))
    runner.recorder.start.assert_awaited_once_with(session.context)
    assert source.logins == []
    for page in pages[1:]:
        routes = page.route.call_args_list
        assert [call.args[0] for call in routes] == ["**/*", "**/tracker.php*", "**/dl.php*"]
        blocked = AsyncMock()
        blocked.request.url = "https://outside.example/advert"
        asyncio.run(routes[0].args[1](blocked))
        blocked.abort.assert_awaited_once_with("blockedbyclient")
        for index, fixture in ((1, "html/search/results.html"), (2, "torrents/sample.torrent")):
            route = AsyncMock()
            asyncio.run(routes[index].args[1](route, object()))
            payload = route.fulfill.call_args.kwargs
            assert payload["body"] == (FIXTURES / fixture).read_bytes()
            if index == 2:
                assert payload["content_type"] == "application/x-bittorrent"
                assert "attachment" in payload["headers"]["Content-Disposition"]


def test_shutdown_in_worker_does_not_install_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    shutdown = Shutdown()
    handler = Mock()
    monkeypatch.setattr("ricercar.pipeline.threading.current_thread", lambda: object())
    monkeypatch.setattr("ricercar.pipeline.signal.signal", handler)
    shutdown.install()
    shutdown.restore()
    handler.assert_not_called()
    assert shutdown._previous == {}


def test_shutdown_restores_supported_handlers_and_keeps_first_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal
    from unittest.mock import Mock

    shutdown = Shutdown()
    handler = Mock(side_effect=[OSError("unsupported"), "old handler", None])
    monkeypatch.setattr("ricercar.pipeline.signal.signal", handler)
    shutdown.install()
    assert shutdown._previous == {signal.SIGTERM: "old handler"}
    shutdown._handle(signal.SIGTERM, None)
    shutdown.request("second request")
    assert shutdown.requested
    assert shutdown.reason == "SIGTERM"
    shutdown.restore()
    assert handler.call_args.args == (signal.SIGTERM, "old handler")
    assert shutdown._previous == {}


def test_interrupt_between_tasks_does_not_start_another_search(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    runner, source, _, _ = browser_run
    source.settings.tasks.append(SearchTask(text="Mozart", category=1))
    source.settings.random_choice = 2
    original_prepare = runner._prepare

    async def prepare(*args: Any) -> None:
        await original_prepare(*args)
        runner.shutdown.request("stop after preparing")

    search = AsyncMock()
    monkeypatch.setattr(runner, "_prepare", prepare)
    monkeypatch.setattr(runner, "_run_task", search)
    outcome = asyncio.run(runner.run())
    assert outcome.interrupted
    assert outcome.sources[0].done == 0
    assert set(outcome.sources[0].task_status.values()) == {"not run"}
    search.assert_not_awaited()


@pytest.mark.parametrize("failure", ["selectors", "maintenance", "timeout", "bad-timeout"])
def test_search_failure_policy_stops_only_repeated_structural_failures(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    from ricercar.retry import Policies
    from ricercar.sources.base import SelectorsBrokenError, SiteUnderMaintenanceError

    runner, source, _, pages = browser_run
    runner.policies = Policies.from_config(runner.cfg.retry.model_copy(update={"max_attempts": 1}))
    source.settings.tasks = [SearchTask(text=name, category=1) for name in ("a", "b", "c")]
    source.settings.random_choice = 3
    errors = {
        "selectors": SelectorsBrokenError(["missing table"]),
        "maintenance": SiteUnderMaintenanceError("maintenance"),
        "timeout": PlaywrightTimeout("slow network"),
        "bad-timeout": PlaywrightTimeout("bad page"),
    }
    attempted: list[str] = []

    async def search(
        _page: object, task: SearchTask, **_kwargs: object
    ) -> AsyncIterator[SearchPage]:
        attempted.append(task.text)
        if len(attempted) <= 2:
            raise errors[failure]
        yield SearchPage(hits=[], number=1)

    monkeypatch.setattr(source, "search", search)
    if failure == "bad-timeout":
        monkeypatch.setattr(source, "diagnose", lambda _html: ("missing table",))
    outcome = asyncio.run(runner.run()).sources[0]
    assert outcome.failures == 2
    assert runner.recorder.capture.await_count == 2
    if failure in ("selectors", "bad-timeout"):
        assert len(attempted) == 2
        assert outcome.done == 0
        assert outcome.skipped == "the site stopped matching the selectors"
        assert runner.history.yields(source.name) == {}
    else:
        assert len(attempted) == 3
        assert outcome.done == 1
        assert outcome.skipped == ""
        assert len(runner.history.yields(source.name)) == 1
    for page in pages:
        page.close.assert_awaited_once()


@pytest.mark.parametrize("recovers", [True, False])
def test_expired_session_relogs_once_then_recovers_or_aborts(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    recovers: bool,
) -> None:
    from ricercar.sources.base import SessionExpiredError

    runner, source, send, pages = browser_run
    attempts: list[str] = []

    async def search(
        _page: object, task: SearchTask, **_kwargs: object
    ) -> AsyncIterator[SearchPage]:
        attempts.append(task.text)
        if len(attempts) == 1 or not recovers:
            raise SessionExpiredError("expired")
        yield SearchPage(hits=[], number=1)

    monkeypatch.setattr(source, "search", search)
    outcome = asyncio.run(runner.run()).sources[0]
    assert len(attempts) == 2
    assert len(source.logins) == 3
    assert runner.recorder.capture.call_args.args[1] == "session-expired"
    assert outcome.aborted is not recovers
    assert outcome.done == int(recovers)
    assert outcome.failures == int(not recovers)
    if not recovers:
        assert "Login failed" in send.call_args.args[1]
        assert set(outcome.task_status.values()) == {"blocked"}
    for page in pages:
        page.close.assert_awaited_once()


@pytest.mark.parametrize("phase", ["search", "download"])
def test_interrupt_keeps_tasks_pending_and_stops_later_sources(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    runner, source, send, _ = browser_run
    other = ReplaySource(source.settings, [])
    monkeypatch.setattr(other, "name", "other")
    runner.sources.append(other)
    original_fetch = source.fetch_torrent
    if phase == "search":

        async def search(*_args: object, **_kwargs: object) -> AsyncIterator[SearchPage]:
            runner.shutdown.request("test interrupt")
            yield SearchPage(hits=[_hit("one")], number=1)

        monkeypatch.setattr(source, "search", search)
    else:
        source.pages = [[_hit("one"), _hit("two")]]

        async def fetch(*args: Any) -> Any:
            result = await original_fetch(*args)
            runner.shutdown.request("test interrupt")
            return result

        monkeypatch.setattr(source, "fetch_torrent", fetch)
    outcome = asyncio.run(runner.run())
    assert outcome.interrupted
    assert len(outcome.sources) == 1
    assert outcome.sources[0].done == 0
    assert runner.state.completed_today(source.name) == set()
    assert other.calls == []
    assert "Interrupted after" in send.call_args.args[1]
    if phase == "download":
        assert outcome.stored == 1
        assert len(source.fetched) == 1
        assert all(stat.runs == 0 for stat in runner.history.yields(source.name).values())


def test_quota_stops_remaining_tasks_without_marking_them_complete(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
) -> None:
    runner, source, send, _ = browser_run
    source.settings.tasks.append(SearchTask(text="Mozart", category=1))
    source.settings.random_choice = 2
    runner.limit = 1
    outcome = asyncio.run(runner.run()).sources[0]
    assert outcome.done == 1
    assert len(source.calls) == 1
    assert sorted(outcome.task_status.values()) == ["completed", "not run"]
    assert "Insufficient Observation" in send.call_args.args[1]


def test_stale_task_is_reported_in_whole_run_summary_and_email(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    log_messages: list[str],
) -> None:
    runner, source, send, _ = browser_run
    source.pages = [[]]
    task = source.settings.tasks[0]
    runner.history.record(
        source.name,
        task_key(task),
        describe_task(task),
        searches=1,
        hits=0,
        stored=0,
        day="2020-01-01",
    )
    outcome = asyncio.run(runner.run())
    assert outcome.stale == 1
    assert "Consider Removing" in send.call_args.args[1]
    assert any("review the task list" in message for message in log_messages)


@pytest.mark.parametrize("check", ["changed", "network", "login"])
def test_category_check_changes_skip_search_but_errors_are_reported_and_continue(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    check: str,
) -> None:
    from unittest.mock import AsyncMock

    from playwright.async_api import Error as PlaywrightError

    from ricercar.sources.base import SessionExpiredError

    runner, source, send, pages = browser_run
    if check == "changed":
        monkeypatch.setattr("ricercar.categories.load_snapshot", lambda: {1: "old section"})
    elif check == "network":
        monkeypatch.setattr(
            "ricercar.sources.rutracker.categories.fetch_categories",
            AsyncMock(side_effect=PlaywrightError("network")),
        )
    else:
        monkeypatch.setattr(
            source,
            "ensure_logged_in",
            AsyncMock(
                side_effect=[SessionExpiredError("no login"), None],
            ),
        )
    outcome = asyncio.run(runner.run()).sources[0]
    pages[0].close.assert_awaited_once()
    body = send.call_args.args[1]
    if check == "changed":
        assert outcome.category_diff is not None
        assert outcome.stored == 0
        assert source.calls == []
        assert "Forum Section Structure Changed" in body
        assert "Insufficient Observation" in body
    else:
        assert outcome.stored == 1
        assert "Category check failed" in body
        assert "Search was attempted anyway" in body


@pytest.mark.parametrize("reason", ["quota", "tasks", "shutdown"])
def test_whole_run_skip_paths_never_open_browser(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    runner, source, send, _ = browser_run
    attach = Mock(side_effect=AssertionError("browser must not be used"))
    monkeypatch.setattr("ricercar.pipeline.managed_browser", attach)
    if reason == "quota":
        source.settings.quota.limit_torrents_one_day = 0
    elif reason == "tasks":
        source.settings.tasks = []
    else:
        runner.shutdown.request("test")
    outcome = asyncio.run(runner.run())
    assert outcome.stored == 0
    assert outcome.interrupted is (reason == "shutdown")
    if reason == "shutdown":
        assert outcome.sources == []
    else:
        assert (
            outcome.sources[0].skipped
            == {
                "quota": "the daily quota is used up",
                "tasks": "no tasks to run",
            }[reason]
        )
    attach.assert_not_called()
    send.assert_not_awaited()


def test_unexpected_error_still_closes_pages_notifies_and_saves_state(
    browser_run: tuple[Runner, ReplaySource, Any, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    runner, source, send, pages = browser_run
    monkeypatch.setattr(source, "fetch_torrent", AsyncMock(side_effect=ValueError("unexpected")))
    with pytest.raises(ValueError, match="unexpected"):
        asyncio.run(runner.run())
    for page in pages:
        page.close.assert_awaited_once()
    assert send.await_count == 2
    assert Path(runner.cfg.run.state_file).is_file()
    assert Path(runner.cfg.run.task_history_file).is_file()
    assert runner.shutdown._previous == {}


def test_a_row_that_is_already_stored_is_logged_with_when_it_was_stored(
    run_config: Settings, log_messages: list[str]
) -> None:
    # The point of the line is to answer "why was this not downloaded?": it has to say
    # when the database got it, not just that it has it.
    url = "https://fake.example/viewtopic.php?t=1"
    with TorrentRepository(run_config.db_path) as repo:
        repo.add(_meta(url, "a" * 32), b"payload")
        stored = repo.stored(url)
    assert stored is not None
    source = ReplaySource(_settings(), [[SearchHit(topic_id="1", title="Bach", url=url)]])

    outcome = _one_page(_runner(run_config, source), source)

    assert outcome.known == 1
    assert outcome.downloaded == 0  # skipped before anything was fetched
    assert f"Already stored: {url} — added {stored.add_date}" in log_messages
    # The runner asked for one page of the planned task, and this source has nothing to
    # do with the page object it was handed.
    assert source.calls == [(None, SearchTask(text="Bach"), source.settings.max_pages)]


# ── Pacing what the tracker sees ──────────────────────────────────────────


@pytest.mark.parametrize("failure", ["unavailable", "selectors", "timeout", "torrent"])
def test_download_failures_record_the_right_outcome_and_evidence(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from playwright.async_api import TimeoutError as PlaywrightTimeout

    from ricercar.log import get_logger
    from ricercar.parser import TorrentError
    from ricercar.sources.base import SelectorsBrokenError, TorrentUnavailableError

    source = ReplaySource(_settings(), [[_hit("one")]])
    runner = _runner(run_config, source)
    capture = AsyncMock()
    monkeypatch.setattr(runner.recorder, "capture", capture)
    warning = Mock(wraps=get_logger().warning)
    monkeypatch.setattr(get_logger(), "warning", warning)
    if failure == "torrent":
        monkeypatch.setattr("ricercar.pipeline.parse_file", Mock(side_effect=TorrentError("bad")))
    else:
        errors = {
            "unavailable": TorrentUnavailableError("removed"),
            "selectors": SelectorsBrokenError(["missing download link"]),
            "timeout": PlaywrightTimeout("timeout"),
        }
        monkeypatch.setattr("ricercar.pipeline.call", AsyncMock(side_effect=errors[failure]))
    outcome = SourceOutcome(source=source.name)
    with runner.repo:
        result = asyncio.run(runner._download_hit(source, None, _hit("one"), outcome))
    assert result is None
    assert outcome.downloaded == 1
    assert outcome.stored == 0
    assert outcome.failures == (0 if failure == "unavailable" else 1)
    assert len(outcome.not_stored_details) == outcome.downloaded - outcome.stored == 1
    detail = outcome.not_stored_details[0]
    assert _hit("one").url in detail
    assert {
        "unavailable": "Torrent unavailable: removed",
        "selectors": "Page structure was not recognized: missing download link",
        "timeout": "Download timed out after retries",
        "torrent": "Downloaded torrent could not be parsed: bad",
    }[failure] in detail
    warning.assert_any_call("Not stored: {}", detail)
    if failure in ("selectors", "timeout"):
        assert (
            capture.call_args.args[1]
            == {
                "selectors": "topic-page",
                "timeout": "download-timeout",
            }[failure]
        )
        assert outcome.failure_details
    else:
        capture.assert_not_awaited()


@pytest.mark.parametrize("failure", ["session", "network"])
def test_incomplete_download_attempt_records_reason_before_propagating(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from playwright.async_api import Error as PlaywrightError

    from ricercar.log import get_logger
    from ricercar.sources.base import SessionExpiredError

    error = (
        SessionExpiredError("login expired") if failure == "session" else PlaywrightError("offline")
    )
    monkeypatch.setattr("ricercar.pipeline.call", AsyncMock(side_effect=error))
    warning = Mock(wraps=get_logger().warning)
    monkeypatch.setattr(get_logger(), "warning", warning)
    source = ReplaySource(_settings(), [[_hit("one")]])
    runner = _runner(run_config, source)
    outcome = SourceOutcome(source=source.name)

    with runner.repo, pytest.raises(type(error)):
        asyncio.run(runner._download_hit(source, None, _hit("one"), outcome))

    assert outcome.downloaded == 1
    assert outcome.stored == 0
    assert len(outcome.not_stored_details) == 1
    assert str(error) in outcome.not_stored_details[0]
    assert _hit("one").url in outcome.not_stored_details[0]
    warning.assert_any_call("Not stored: {}", outcome.not_stored_details[0])


def test_downloads_deduplicate_content_and_fall_back_to_topic_url(
    run_config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: list[str],
) -> None:
    from dataclasses import replace
    from unittest.mock import Mock

    from ricercar.log import get_logger

    warning = Mock(wraps=get_logger().warning)
    monkeypatch.setattr(get_logger(), "warning", warning)
    meta = parse_file(FIXTURES / "torrents" / "sample.torrent")
    monkeypatch.setattr(
        "ricercar.pipeline.parse_file",
        lambda _path: replace(meta, url="", issues=("missing comment",)),
    )
    source = ReplaySource(_settings(), [[_hit("first"), _hit("second")]])
    runner = _runner(run_config, source)
    outcome = _one_page(runner, source)
    assert (outcome.downloaded, outcome.stored, outcome.known) == (2, 1, 1)
    with runner.repo:
        assert runner.repo.count() == 1
        stored = runner.repo.stored(_hit("first").url)
        assert stored is not None
        assert runner.repo.stored(_hit("second").url) is None
    assert any("Already stored as" in message for message in log_messages)
    assert any("torrent issue: missing comment" in message for message in log_messages)
    assert len(outcome.not_stored_details) == outcome.downloaded - outcome.stored == 1
    detail = outcome.not_stored_details[0]
    assert _hit("second").url in detail
    assert _hit("first").url in detail
    assert stored.add_date in detail
    assert "duplicate content" in detail
    warning.assert_any_call("Not stored: {}", detail)


class FakeClock:
    """A clock that only moves when something sleeps on it.

    Waiting is the one thing a test cannot do for real at any interesting scale, so the
    wait takes its clock and its sleep from the caller (see
    :func:`ricercar.pipeline.wait_between_pages`) and this is what a test hands it.
    """

    def __init__(self) -> None:
        self.moment = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.moment += seconds


def test_the_wait_before_the_next_page_is_the_configured_delay() -> None:
    clock = FakeClock()
    ticks: list[float] = []

    waited = asyncio.run(
        wait_between_pages(
            30.0,
            stop=lambda: False,
            on_tick=ticks.append,
            sleep=clock.sleep,
            now=clock.now,
        )
    )

    assert waited == 30.0
    assert clock.slept == 30.0
    assert ticks == sorted(ticks, reverse=True)  # a countdown, not a count-up
    assert ticks[0] == 30.0
    assert ticks[-1] <= PAUSE_TICK


def test_a_stop_ends_the_wait_early() -> None:
    # Ctrl-C must not sit out a minute of someone's life.
    clock = FakeClock()

    waited = asyncio.run(
        wait_between_pages(
            60.0,
            stop=lambda: clock.now() >= 1.0,
            sleep=clock.sleep,
            now=clock.now,
        )
    )

    assert waited == 1.0
    assert clock.slept == 1.0


@pytest.fixture
def paced(run_config: Settings, extra_config: ExtraConfig) -> Callable[[float], Settings]:
    """`run_config` — temp database and all — with the given page delay layered on top."""

    def with_delay(seconds: float) -> Settings:
        settings = extra_config(
            f"paced-{seconds}.yml",
            f"sources:\n  rutracker:\n    quota:\n      delay_between_actions: {seconds}\n",
        )
        # The delay is what this layers; the temp database is what it must not disturb.
        assert settings.db_path == run_config.db_path
        return settings

    return with_delay


def test_a_page_that_stored_something_is_paced_before_the_next(
    paced: Callable[[float], Settings],
) -> None:
    # Two pages, same hit: page 1 stores it, page 2 finds it already known.
    # Every action that reaches the tracker is paced — download before page 1
    # (0.3 s), pagination after page 1 (0.3 s), pagination after page 2 (0.3 s).
    settings = paced(0.3)
    hit = SearchHit(topic_id="1", title="Bach", url="https://fake.example/t=1")
    source = ReplaySource(settings.sources["rutracker"], [[hit], [hit]])

    started = time.monotonic()
    outcome = _one_page(_runner(settings, source), source)
    elapsed = time.monotonic() - started

    assert outcome.stored == 1
    assert outcome.known == 1
    # Three paces of 0.3 s each ⇒ at least 0.9 s total.
    assert elapsed >= 0.9


def test_pagination_is_still_paced_when_all_hits_are_known(
    paced: Callable[[float], Settings],
) -> None:
    # All hits are already stored, so nothing is downloaded — but we still accessed the
    # tracker to fetch the page, and paginating to the next results page costs another
    # request.  Every pagination is paced regardless of whether any hit was stored.
    settings = paced(0.3)
    url = "https://fake.example/t=1"
    with TorrentRepository(settings.db_path) as repo:
        repo.add(_meta(url, "a" * 32), b"payload")
    source = ReplaySource(
        settings.sources["rutracker"], [[SearchHit(topic_id="1", title="Bach", url=url)]]
    )

    started = time.monotonic()
    outcome = _one_page(_runner(settings, source), source)

    assert outcome.known == 1
    # Pagination pace (0.3 s) after the one page that had the (known) hit.
    assert time.monotonic() - started >= 0.3


def test_a_page_of_rows_that_were_only_downloaded_is_still_paced(
    paced: Callable[[float], Settings],
) -> None:
    # The content is already here under another url, so neither row is stored — but both
    # still cost a request, and the request rate is the thing that gets an account
    # noticed. A page like this must not be the loophole in the delay.
    settings = paced(0.3)
    fixture = FIXTURES / "torrents" / "sample.torrent"
    with TorrentRepository(settings.db_path) as repo:
        repo.add(_meta("https://elsewhere.example/t=99", parse_file(fixture).md5), b"payload")
    source = ReplaySource(
        settings.sources["rutracker"],
        [
            [
                SearchHit(topic_id="1", title="Bach", url="https://fake.example/t=1"),
                SearchHit(topic_id="2", title="Bach again", url="https://fake.example/t=2"),
            ]
        ],
    )

    started = time.monotonic()
    outcome = _one_page(_runner(settings, source), source)

    assert outcome.stored == 0
    assert outcome.downloaded == 2
    assert outcome.known == 2
    assert time.monotonic() - started >= 0.3


# ── How many pages the bar counts to ──────────────────────────────────────


def _bar_totals(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Every total the run puts on a progress bar, in order.

    The bar is gone by the time the run returns — the runner removes it — so how it was
    sized can only be seen while it is being sized.
    """
    totals: list[int | None] = []
    original = Progress.update

    def record(self: Progress, task_id: TaskID, **kwargs: Any) -> None:
        if "total" in kwargs:
            totals.append(kwargs["total"])
        original(self, task_id, **kwargs)

    monkeypatch.setattr(Progress, "update", record)
    return totals


def test_the_pages_bar_counts_the_pagers_pages_and_not_the_ceiling(
    run_config: Settings, extra_config: ExtraConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A broad rutracker search is ten pages long whatever the configuration says — a bar
    # sized by `max_pages` promises 50 and stops at ten, which is what makes a ceiling
    # look like a lie.
    settings = extra_config("tall-bar.yml", "sources:\n  rutracker:\n    max_pages: 50\n")
    # The temp database `run_config` laid down has to survive the override.
    assert settings.db_path == run_config.db_path
    source = ReplaySource(settings.sources["rutracker"], [[_hit("1")], [_hit("2")]], total=10)
    totals = _bar_totals(monkeypatch)

    _one_page(_runner(settings, source), source)

    assert 10 in totals
    assert 50 not in totals


def test_the_pages_bar_is_capped_by_max_pages_when_the_search_is_longer(
    run_config: Settings, extra_config: ExtraConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pager may promise more pages than the run is allowed to walk; what the bar
    # counts to is what will actually be walked.
    settings = extra_config("short-bar.yml", "sources:\n  rutracker:\n    max_pages: 3\n")
    # The temp database `run_config` laid down has to survive the override.
    assert settings.db_path == run_config.db_path
    source = ReplaySource(settings.sources["rutracker"], [[_hit("1")]], total=10)
    totals = _bar_totals(monkeypatch)

    _one_page(_runner(settings, source), source)

    assert 3 in totals


def test_a_search_nobody_counts_leaves_the_bar_indeterminate(
    run_config: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not every source states a page count, and a guess dressed up as a total is worse
    # than a spinner.
    source = ReplaySource(run_config.sources["rutracker"], [[_hit("1")]])
    totals = _bar_totals(monkeypatch)

    _one_page(_runner(run_config, source), source)

    assert totals == []


# ── Reporting ─────────────────────────────────────────────────────────────


def test_a_task_is_described_in_one_line() -> None:
    assert describe_task(SearchTask()) == "(everything)"
    assert describe_task(SearchTask(text="Bach")) == "Bach"
    assert (
        describe_task(SearchTask(text="Bach", author="someone", category=5))
        == "Bach by someone in category 5"
    )


def test_an_outcome_describes_a_source() -> None:
    outcome = SourceOutcome(source="rutracker", tasks=3, done=2, stored=1, downloaded=1, hits=50)

    assert "2/3 task(s)" in outcome.describe()
    assert "1 stored" in outcome.describe()
    assert "50 seen" in outcome.describe()
    assert (
        SourceOutcome(source="rutracker", skipped="the daily quota is used up").describe()
        == "rutracker: skipped — the daily quota is used up"
    )


def test_a_run_reports_whether_a_source_could_not_be_driven() -> None:
    stored = SourceOutcome(source="rutracker", stored=2, failures=1)
    aborted = SourceOutcome(source="other", aborted=True)

    run = RunOutcome(sources=[stored, aborted], elapsed=12.0)

    assert run.stored == 2
    assert run.failures == 1
    assert run.failed is True
    assert "interrupted" not in run.describe()
    assert RunOutcome(sources=[stored], interrupted=True).describe().startswith("interrupted,")


# ── One cycle, offline, through a real browser ────────────────────────────


def _cdp_ready(connect_url: str, *, timeout: float = 2.0) -> bool:
    """Whether a browser is answering on *connect_url*."""
    try:
        with urlopen(f"{connect_url.rstrip('/')}/json/version", timeout=timeout) as response:
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


@pytest.fixture
def browser(run_config: Settings) -> str:
    """Skip the integration tests unless the configured browser is attached."""
    url = run_config.sources["rutracker"].connect_url
    if not _cdp_ready(url):
        pytest.skip(f"no browser is listening on {url} — see the README's bootstrap section")
    return url


@pytest.mark.integration
def test_an_offline_run_downloads_stores_and_remembers(
    run_config: Settings, browser: str, fixtures_root: Path, sample_torrent: bytes
) -> None:
    assert browser.startswith("http")

    # One configured task and no category: the run is one search per configured section,
    # and the quota stops it between them. The repository's own config contributes its
    # sections too, so the expected number comes from the effective settings.
    categories = run_config.sources["rutracker"].categories
    assert len(categories) >= 2, "the test config declares two sections"
    outcome = run_sources(cfg=run_config, offline=fixtures_root, limit=1)

    assert outcome.failed is False
    assert outcome.interrupted is False
    assert outcome.stored == 1
    (source_outcome,) = outcome.sources
    assert source_outcome.tasks == len(categories)
    assert source_outcome.done == 1
    # two hits seen: the one that was stored, and the next one, after which the quota
    # stopped the loop
    assert source_outcome.hits == 2
    assert source_outcome.downloaded == 1
    assert source_outcome.failures == 0
    assert len(source_outcome.new_authors) > 0

    with TorrentRepository(run_config.db_path) as repo:
        assert repo.count() == 1
        assert repo.count_today_from("rutracker.org") == 1
        # the fixture is the torrent the captured topic page offers
        assert repo.blob("https://rutracker.org/forum/viewtopic.php?t=1580744") == sample_torrent

    state = json.loads(Path(run_config.run.state_file).read_text(encoding="utf-8"))
    assert state["version"] == STATE_VERSION
    # only the first search of the expanded task counted as done, and it is one of the
    # configured sections
    completed = state["sources"]["rutracker"]["completed"]
    assert len(completed) == 1
    assert set(completed) <= {
        task_key(SearchTask(text="Bach", category=id)) for id in categories.values()
    }

    # …and the task's yield is remembered per configured task, not per search: one run so
    # far, of the one search the quota allowed.
    history = TaskHistory(Path(run_config.run.task_history_file)).load()
    stat = history.yields("rutracker")[task_key(SearchTask(text="Bach"))]
    assert (stat.runs, stat.searches, stat.hits, stat.stored) == (1, 1, 2, 1)
    assert stat.last_run == date.today().isoformat()
    assert stat.last_hit == stat.last_stored == date.today().isoformat()
    assert stat.description == "Bach"
    # nothing has run often enough yet for anything to look dead
    assert source_outcome.stale == 0
    assert outcome.stale == 0


@pytest.mark.integration
def test_a_task_that_has_found_nothing_for_months_is_reported(
    run_config: Settings, browser: str, fixtures_root: Path
) -> None:
    assert browser.startswith("http")

    # The first task fills the quota on its first search, so the second one — which has
    # months of history without a single result row — never runs and stays as quiet as it
    # was. That is exactly the task this feature exists to point out.
    run_config.sources["rutracker"].must_complete_tasks = [
        SearchTask(text="Bach"),
        SearchTask(author="ghost"),
    ]
    dead = SearchTask(author="ghost")
    history = TaskHistory(Path(run_config.run.task_history_file))
    for months in (5, 4, 3, 2, 1):
        history.record(
            "rutracker",
            task_key(dead),
            describe_task(dead),
            searches=len(run_config.sources["rutracker"].categories),
            hits=0,
            stored=0,
            day=(date.today() - timedelta(days=30 * months)).isoformat(),
        )
    history.save()

    outcome = run_sources(cfg=run_config, offline=fixtures_root, limit=1)

    (source_outcome,) = outcome.sources
    assert source_outcome.stored == 1
    assert source_outcome.stale == 1
    assert outcome.stale == 1

    reopened = TaskHistory(Path(run_config.run.task_history_file)).load()
    stale = reopened.stale("rutracker", min_runs=2, min_days=30)
    assert [stat.key for stat in stale] == [task_key(dead)]
    # the task that did run has today's numbers; the quiet one only grew older
    assert reopened.yields("rutracker")[task_key(SearchTask(text="Bach"))].hits == 2
    assert reopened.yields("rutracker")[task_key(dead)].runs == 5


@pytest.mark.integration
def test_a_broken_selector_saves_the_page_and_skips_the_source(
    run_config: Settings, tmp_path: Path, browser: str, fixtures_root: Path
) -> None:
    assert browser.startswith("http")
    selectors = tmp_path / "selectors.yml"
    selectors.write_text("result_link: 'a.not-a-thing'\n", encoding="utf-8")
    run_config.sources["rutracker"].selectors_file = str(selectors)

    outcome = run_sources(cfg=run_config, offline=fixtures_root, limit=1)

    assert outcome.stored == 0
    assert outcome.failures == 2
    (source_outcome,) = outcome.sources
    assert source_outcome.hits == 0
    assert source_outcome.skipped == "the site stopped matching the selectors"

    with TorrentRepository(run_config.db_path) as repo:
        assert repo.count() == 0

    evidence = sorted(Path(run_config.diagnostics.failures_dir).iterdir())
    assert evidence
    for directory in evidence:
        assert "selectors" in directory.name
        assert (directory / "page.html").is_file()
        assert (directory / "page.png").is_file()
        assert (directory / "trace.zip").is_file()
        payload = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
        assert any("no row matches" in issue for issue in payload["issues"])
        assert payload["trace"] == "trace.zip"

    # A search that failed before it produced a page is not the task's fault, so it is not
    # held against the task that asked for it.
    assert TaskHistory(Path(run_config.run.task_history_file)).load().yields("rutracker") == {}


@pytest.mark.integration
def test_a_maintenance_page_increases_failures_and_continues(
    run_config: Settings, tmp_path: Path, browser: str
) -> None:
    """A maintenance page raises SiteUnderMaintenanceError, which _drive catches,
    increments failures, captures evidence, and moves on to the next task."""
    assert browser.startswith("http")

    # Create a temporary fixture root with maintenance HTML for searches.
    fixture_dir = tmp_path / "fixtures"
    search_dir = fixture_dir / "html" / "search"
    search_dir.mkdir(parents=True)
    (search_dir / "results.html").write_text(
        "<html><body><p>the site is under maintenance, please come back later</p></body></html>",
        encoding="utf-8",
    )
    # Also create dummy fixtures for the other routes so FixtureRouter.register()
    # does not raise FileNotFoundError — they will never be navigated to.
    topic_dir = fixture_dir / "html" / "topic"
    topic_dir.mkdir(parents=True)
    (topic_dir / "topic.html").write_text("<html><body></body></html>", encoding="utf-8")
    torrent_dir = fixture_dir / "torrents"
    torrent_dir.mkdir(parents=True)
    (torrent_dir / "sample.torrent").write_bytes(
        (FIXTURES / "torrents" / "sample.torrent").read_bytes(),
    )

    outcome = run_sources(cfg=run_config, offline=fixture_dir, limit=1)

    # Maintenance is counted as a failure but does NOT skip the source.
    assert outcome.stored == 0
    assert outcome.failures == 1
    (source_outcome,) = outcome.sources
    assert source_outcome.hits == 0
    # The source should NOT be skipped — maintenance is transient.
    assert source_outcome.skipped is None

    with TorrentRepository(run_config.db_path) as repo:
        assert repo.count() == 0

    # The recorder captured the maintenance evidence.
    evidence = sorted(Path(run_config.diagnostics.failures_dir).iterdir())
    assert evidence
    for directory in evidence:
        assert "maintenance" in directory.name
        assert (directory / "page.html").is_file()
        assert (directory / "page.png").is_file()
        payload = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
        assert payload["trace"] == "trace.zip"
        assert any("maintenance" in issue for issue in payload["issues"])
