"""CLI decisions with real configuration and isolated external boundaries."""

from __future__ import annotations

import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console

from ricercar import cli
from ricercar.config import Settings, SourceSettings
from ricercar.repository import DatabaseSchemaError


@pytest.fixture
def cli_settings(
    extra_config: Callable[[str, str], Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    settings = extra_config(
        "cli.yml",
        f"""
database:
  path: {tmp_path / "torrents.db"}
browser:
  downloads_dir: {tmp_path / "downloads"}
run:
  task_history_file: {tmp_path / "history.json"}
  discovered_uploaders_file: {tmp_path / "discovered.json"}
""",
    ).model_copy(update={"sources": {"rutracker": SourceSettings(categories={"Music": 1})}})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "_close_browsers", Mock())
    return settings


@pytest.fixture
def cli_output(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    from ricercar import progress

    output = StringIO()
    monkeypatch.setattr(progress, "CONSOLE", Console(file=output, width=120, color_system=None))
    return output


@pytest.mark.parametrize("command", ["show-config", "notify-test", "tasks"])
@pytest.mark.usefixtures("cli_settings")
def test_dry_run_rejects_subcommands_before_side_effects(command: str) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.cli(["--dry-run", command])
    assert exited.value.code == 2


def test_dry_run_validates_database_but_never_runs_or_closes_browsers(
    cli_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_once = Mock()
    scheduled = Mock()
    monkeypatch.setattr(cli, "_run_once", run_once)
    monkeypatch.setattr(cli, "_run_scheduled", scheduled)
    cli.cli(["--dry-run"])
    assert not cli_settings.db_path.exists()
    run_once.assert_not_called()
    scheduled.assert_not_called()
    cli._close_browsers.assert_not_called()


def test_cli_refuses_legacy_database_before_running(
    cli_settings: Settings,
    legacy_database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_database.create(cli_settings.db_path)
    run_once = Mock()
    monkeypatch.setattr(cli, "_run_once", run_once)
    with pytest.raises(SystemExit) as exited:
        cli.cli(["--once"])
    assert exited.value.code == 1
    run_once.assert_not_called()
    cli._close_browsers.assert_not_called()


@pytest.mark.parametrize(("enabled", "delivered"), [(False, False), (True, False), (True, True)])
@pytest.mark.usefixtures("cli_output")
def test_notification_test_returns_delivery_status_without_sending_mail(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    delivered: bool,
) -> None:
    from ricercar.notify import Notifier

    notifier = SimpleNamespace(enabled=enabled, send=AsyncMock(return_value=delivered))
    monkeypatch.setattr(Notifier, "from_config", Mock(return_value=notifier))
    assert cli._notify_test() == (0 if enabled and delivered else 1)
    assert notifier.send.await_count == int(enabled)


@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "both", "schema"])
def test_run_once_maps_outcomes_and_forwards_runner_options(
    cli_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
) -> None:
    from ricercar import pipeline
    from ricercar.sources import get_source

    result = SimpleNamespace(
        failed=outcome in ("failed", "both"),
        interrupted=outcome in ("interrupted", "both"),
        describe=lambda: "cycle finished",
    )
    run = AsyncMock(return_value=result)
    if outcome == "schema":
        run.side_effect = DatabaseSchemaError("migration needed")
    runner = Mock(return_value=SimpleNamespace(run=run))
    monkeypatch.setattr(pipeline, "Runner", runner)
    args = cli._build_parser().parse_args(
        [
            "--once",
            "--source",
            "rutracker",
            "--offline",
            str(tmp_path),
            "--limit",
            "2",
            "--no-resume",
        ]
    )
    shutdown = object()
    expected = {"success": 0, "failed": 1, "interrupted": 130, "both": 1, "schema": 1}
    assert cli._run_once(args, shutdown=shutdown) == expected[outcome]
    kwargs = runner.call_args.kwargs
    assert runner.call_args.args == (cli_settings,)
    assert kwargs["shutdown"] is shutdown
    assert kwargs["offline"] == tmp_path
    assert kwargs["limit"] == 2
    assert kwargs["resume"] is False
    assert kwargs["sources"][0].settings == get_source("rutracker", cli_settings).settings


@pytest.mark.parametrize("failure", [False, True])
def test_scheduler_cycle_clears_current_run_even_on_error(
    cli_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    scheduler = cli._Scheduler(cli._build_parser().parse_args([]), cli_settings)

    def run(_args: Any, *, shutdown: Any) -> int:
        assert scheduler._current is shutdown
        if failure:
            raise RuntimeError("cycle failed")
        return 0

    monkeypatch.setattr(cli, "_run_once", run)
    scheduler._cycle()
    assert scheduler._current is None
    assert not scheduler._stop.is_set()


@pytest.mark.parametrize("active", [False, True])
def test_scheduler_first_signal_stops_run_second_leaves_schedule(
    cli_settings: Settings,
    active: bool,
) -> None:
    from ricercar.pipeline import Shutdown

    scheduler = cli._Scheduler(cli._build_parser().parse_args([]), cli_settings)
    shutdown = Shutdown()
    scheduler._current = shutdown if active else None
    scheduler._handle(signal.SIGINT, None)
    assert scheduler._stop.is_set() is (not active)
    if active:
        assert shutdown.requested
        scheduler._handle(signal.SIGINT, None)
        assert scheduler._stop.is_set()


@pytest.fixture
def category_cli(
    cli_settings: Settings,
    cli_output: StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from ricercar import browser, categories, sources
    from ricercar.sources.rutracker import RutrackerSource
    from ricercar.sources.rutracker import categories as scraper

    source = RutrackerSource(cli_settings.sources["rutracker"])
    login = AsyncMock()
    monkeypatch.setattr(source, "ensure_logged_in", login)
    page = object()
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    released = Mock()

    @asynccontextmanager
    async def attached(_source: Any) -> Any:
        assert _source is source
        try:
            yield SimpleNamespace(context=context)
        finally:
            released()

    fetch = AsyncMock(return_value={1: "Music"})
    order = Mock()
    order.attach_mock(login, "login")
    order.attach_mock(fetch, "fetch")
    write = Mock()
    monkeypatch.setattr(browser, "managed_browser", attached)
    monkeypatch.setattr(sources, "get_source", Mock(return_value=source))
    monkeypatch.setattr(scraper, "fetch_categories", fetch)
    monkeypatch.setattr(categories, "load_snapshot", Mock(return_value={1: "Music"}))
    monkeypatch.setattr(categories, "write_snapshot", write)
    return SimpleNamespace(
        fetch=fetch,
        login=login,
        write=write,
        order=order,
        released=released,
        output=cli_output,
    )


@pytest.mark.parametrize(
    "state", ["same", "added", "removed", "renamed", "empty", "fetch-error", "login-error"]
)
def test_category_check_distinguishes_changes_from_fetch_failures(
    category_cli: Any,
    cli_output: StringIO,
    state: str,
) -> None:
    live = {
        "same": {1: "Music"},
        "added": {1: "Music", 2: "Opera"},
        "removed": {2: "Opera"},
        "renamed": {1: "Classical"},
        "empty": {},
    }
    if state == "fetch-error":
        category_cli.fetch.side_effect = RuntimeError("offline")
    elif state == "login-error":
        category_cli.login.side_effect = RuntimeError("session expired")
    else:
        category_cli.fetch.return_value = live[state]
    args = cli._build_parser().parse_args(["categories"])
    assert args.action == "check"
    assert cli._handle_categories(args) == (0 if state == "same" else 1)
    output = cli_output.getvalue()
    if state == "same":
        assert "No changes" in output
    elif state in ("empty", "fetch-error", "login-error"):
        assert "Removed" not in output
        assert "Category changes detected" not in output
    else:
        assert {"added": "Added", "removed": "Removed", "renamed": "Renamed"}[state] in output
    category_cli.write.assert_not_called()
    category_cli.released.assert_called_once()
    if state == "login-error":
        category_cli.fetch.assert_not_awaited()
    else:
        assert [call[0] for call in category_cli.order.mock_calls] == ["login", "fetch"]


@pytest.mark.parametrize("state", ["success", "empty", "error"])
def test_category_refresh_never_replaces_snapshot_on_empty_or_failed_fetch(
    category_cli: Any,
    state: str,
) -> None:
    if state == "empty":
        category_cli.fetch.return_value = {}
    elif state == "error":
        category_cli.fetch.side_effect = RuntimeError("offline")
    args = cli._build_parser().parse_args(["categories", "refresh"])
    assert cli._handle_categories(args) == (0 if state == "success" else 1)
    if state == "success":
        category_cli.write.assert_called_once_with({1: "Music"})
    else:
        category_cli.write.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "unavailable", "other-source"])
def test_category_command_requires_usable_rutracker_source(
    cli_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from ricercar import sources

    if kind == "missing":
        cli_settings.sources.clear()
    get_source = Mock(return_value=object())
    if kind == "unavailable":
        get_source.side_effect = RuntimeError("unknown source")
    monkeypatch.setattr(sources, "get_source", get_source)
    assert cli._handle_categories(cli._build_parser().parse_args(["categories"])) == 1
