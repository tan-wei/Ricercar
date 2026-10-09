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
from datetime import date, timedelta
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
