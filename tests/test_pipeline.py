"""The run loop: how a run plans its work, and one whole cycle driven offline.

The planning tests need no browser. The two ``integration`` tests drive a real one
over CDP, so they are skipped unless something answers on the configured endpoint —
start your browser as the README describes and they run too.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from ricercar.config import QuotaConfig, SearchTask, Settings, SourceSettings
from ricercar.history import TaskHistory
from ricercar.models import TorrentMetadata
from ricercar.pipeline import (
    PlannedSearch,
    Runner,
    RunOutcome,
    SourceOutcome,
    describe_task,
    run_sources,
)
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
    assert source_outcome.new_authors > 0

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
