"""Configuration: the defaults worth knowing, how layers combine, and the registry."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from ricercar.config import (
    PROJECT_ROOT,
    BrowserConfig,
    DatabaseConfig,
    DiagnosticsConfig,
    LogConfig,
    LoginConfig,
    NotifyConfig,
    QuotaConfig,
    RetryConfig,
    RunConfig,
    ScheduleConfig,
    SearchTask,
    Settings,
    SourceSettings,
)
from ricercar.sources import enabled_sources, get_source
from ricercar.sources.base import UnknownSourceError
from ricercar.sources.rutracker import RutrackerSource

ExtraConfig = Callable[[str, str], Settings]


# ── Defaults ──────────────────────────────────────────────────────────────


def test_source_defaults() -> None:
    settings = SourceSettings()

    assert settings.connect_url == "http://localhost:9222"
    assert settings.selectors_file is None
    assert settings.tasks == []
    assert settings.must_complete_tasks == []
    assert settings.random_choice == 3
    assert settings.max_pages == 50
    assert settings.quota.limit_torrents_one_day == 50
    assert settings.login.username == ""
    assert settings.login.password == ""


def test_run_defaults() -> None:
    assert RunConfig().resume is True
    assert RunConfig().stale_task_days == 60
    assert RunConfig().stale_task_runs == 5

    schedule = ScheduleConfig()
    assert schedule.interval_minutes == 60
    assert schedule.run_on_start is True

    assert DiagnosticsConfig().save_traces is True
    assert BrowserConfig().navigation_timeout == 30_000
    assert RetryConfig().max_attempts == 3


def test_out_of_range_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceSettings(random_choice=-1)
    with pytest.raises(ValidationError):
        SourceSettings(max_pages=0)
    with pytest.raises(ValidationError):
        RetryConfig(max_attempts=99)
    with pytest.raises(ValidationError):
        BrowserConfig(navigation_timeout=1_000)
    with pytest.raises(ValidationError):
        ScheduleConfig(interval_minutes=0)
    # a threshold of zero would report every task as dead the moment it first ran
    with pytest.raises(ValidationError):
        RunConfig(stale_task_days=0)
    with pytest.raises(ValidationError):
        RunConfig(stale_task_runs=0)


# ── Layering ──────────────────────────────────────────────────────────────


def test_a_later_config_file_wins(extra_config: ExtraConfig) -> None:
    first = extra_config("first.yml", "browser:\n  action_timeout: 11000\n")
    assert first.browser.action_timeout == 11_000

    second = extra_config("second.yml", "browser:\n  action_timeout: 22000\n")
    assert second.browser.action_timeout == 22_000
    assert second.browser.navigation_timeout == 30_000  # keys nobody overrode survive


def test_a_later_config_file_replaces_a_list_whole(extra_config: ExtraConfig) -> None:
    extra_config("first.yml", "notify:\n  urls:\n    - slack://token-a/token-b/token-c\n")
    settings = extra_config("second.yml", "notify:\n  urls:\n    - discord://webhook/id\n")

    assert settings.notify.urls == ["discord://webhook/id"]


def test_the_repository_config_still_contributes(extra_config: ExtraConfig) -> None:
    settings = extra_config(
        "sources.yml",
        "sources:\n  rutracker:\n    tasks:\n      - text: Bach\n    random_choice: 1\n",
    )
    rutracker = settings.sources["rutracker"]

    assert [task.text for task in rutracker.tasks] == ["Bach"]
    assert rutracker.random_choice == 1
    assert rutracker.connect_url  # untouched, so it still comes from config.yml
    assert rutracker.quota.limit_torrents_one_day >= 1


def test_paths_are_derived_from_the_config(extra_config: ExtraConfig, tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"

    settings = extra_config("paths.yml", f"browser:\n  downloads_dir: {downloads.as_posix()}\n")

    assert settings.download_dir == downloads
    assert downloads.is_dir()  # created while loading, so a run cannot trip over it
    assert settings.db_path.name == "torrents.db"


# ── The source registry ───────────────────────────────────────────────────


class _StubSettings:
    """Just enough of :class:`Settings` for the registry: a ``sources`` mapping."""

    def __init__(self, sections: dict[str, SourceSettings]) -> None:
        self.sources = sections


def _stub(**sections: SourceSettings) -> Settings:
    return cast(Settings, _StubSettings(sections))


def test_a_configured_source_is_built_from_its_own_section() -> None:
    source = get_source("rutracker", _stub(rutracker=SourceSettings(random_choice=7)))

    assert isinstance(source, RutrackerSource)
    assert source.name == "rutracker"
    assert source.host == "rutracker.org"
    assert source.settings.random_choice == 7


def test_enabled_sources_follow_the_configuration() -> None:
    sources = enabled_sources(_stub(rutracker=SourceSettings()))

    assert [source.name for source in sources] == ["rutracker"]


def test_an_unknown_source_names_the_ones_that_exist() -> None:
    with pytest.raises(UnknownSourceError, match="Unknown source 'nope'"):
        get_source("nope", _stub())


def test_a_source_without_a_configuration_section_is_reported() -> None:
    with pytest.raises(UnknownSourceError, match="has no configuration"):
        get_source("rutracker", _stub())


def test_a_configured_source_without_an_implementation_is_reported() -> None:
    with pytest.raises(UnknownSourceError, match="Unknown source 'a-tracker'"):
        enabled_sources(_stub(rutracker=SourceSettings(), **{"a-tracker": SourceSettings()}))


def test_tasks_are_the_vocabulary_of_the_tracker() -> None:
    task = SearchTask(text="Bach", author="someone", category=5)

    assert (task.text, task.author, task.category) == ("Bach", "someone", 5)
    assert SearchTask().author is None
    assert SearchTask().category is None


def test_a_task_can_ask_for_every_category() -> None:
    assert SearchTask(category="*").category == "*"
    with pytest.raises(ValidationError):
        SearchTask(category="all")  # only the wildcard is a word


def test_a_task_without_a_category_needs_the_categories_to_search() -> None:
    # Naming no category means "every section I configured" — with none configured the
    # task would silently match nothing, so it is refused instead.
    with pytest.raises(ValidationError, match="does not name a category"):
        SourceSettings(tasks=[SearchTask(text="Bach")])
    with pytest.raises(ValidationError, match="does not name a category"):
        SourceSettings(must_complete_tasks=[SearchTask(category="*")])

    # …and a task that *does* name one needs nothing:
    SourceSettings(tasks=[SearchTask(text="Bach", category=794)])


def test_the_order_of_the_categories_is_kept() -> None:
    settings = SourceSettings(categories={"Opera": 794, "Choral": 2307})

    assert list(settings.categories) == ["Opera", "Choral"]


def test_categories_are_section_ids() -> None:
    with pytest.raises(ValidationError):
        SourceSettings(categories={"nowhere": 0})


# ── The example config ────────────────────────────────────────────────────

EXAMPLE = PROJECT_ROOT / "config" / "config.example.yml"

SECTIONS: dict[str, type[BaseModel]] = {
    "browser": BrowserConfig,
    "run": RunConfig,
    "diagnostics": DiagnosticsConfig,
    "schedule": ScheduleConfig,
    "retry": RetryConfig,
    "log": LogConfig,
    "database": DatabaseConfig,
    "notify": NotifyConfig,
}


def _example() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """One section of the example, as the mapping it is written to be."""
    section = data[name]
    assert isinstance(section, dict), name
    return section


def test_the_example_documents_every_setting() -> None:
    # config.example.yml is the reference, so a setting added to the models has to be
    # added there too — otherwise it is a setting nobody can discover.
    data = _example()

    assert set(data) == set(Settings.model_fields)
    for name, model in SECTIONS.items():
        assert set(_section(data, name)) == set(model.model_fields), name

    sources = _section(data, "sources")
    assert set(sources) == {"rutracker"}
    rutracker = _section(sources, "rutracker")
    assert set(rutracker) == set(SourceSettings.model_fields)
    assert set(_section(rutracker, "login")) == set(LoginConfig.model_fields)
    assert set(_section(rutracker, "quota")) == set(QuotaConfig.model_fields)

    tasks = rutracker["tasks"]
    assert isinstance(tasks, list) and tasks
    # a task may name any one of the three fields, and between them the example shows all
    # of them
    documented: set[str] = set().union(*(set(task) for task in tasks))
    assert documented == set(SearchTask.model_fields)


def test_the_example_is_a_valid_configuration() -> None:
    data = _example()

    # Every section has to satisfy its own model: the example cannot document a value the
    # code would reject.
    for name, model in SECTIONS.items():
        model(**_section(data, name))

    settings = SourceSettings(**_section(_section(data, "sources"), "rutracker"))
    assert settings.tasks and settings.tasks[0].text
    assert settings.login.username == ""
    assert settings.selectors_file is None
