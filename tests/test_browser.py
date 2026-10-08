"""Closing the browser we borrowed, from the program's side.

Whether a *close* works is a question only the live browser can answer (the tests marked
``integration`` ask it). What is answerable here is *when* a close is attempted: once per
endpoint however many sources share it, not at all when the configuration says to leave the
browser alone, and whatever way the run ended — a run that crashes still hands the browser
back.
"""

from __future__ import annotations

from collections.abc import Callable
from textwrap import dedent

import pytest

from ricercar import cli
from ricercar.config import Settings, SourceSettings
from ricercar.sources import SOURCES

ExtraConfig = Callable[[str, str], Settings]

ENDPOINT = "http://localhost:9999"
"""Where the tests pretend the browser listens."""


class SecondSource:
    """A second tracker, so that two configured names can point at one browser."""

    name = "second"

    def __init__(self, settings: SourceSettings) -> None:
        self.settings = settings


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The endpoints a close was attempted for, with no browser to really close."""
    attempted: list[str] = []

    async def fake_close_attached(url: str) -> None:
        attempted.append(url)

    monkeypatch.setattr("ricercar.browser.close_attached", fake_close_attached)
    return attempted


@pytest.fixture(autouse=True)
def settings(extra_config: ExtraConfig) -> Settings:
    """The real configuration, with the browser endpoint pointed somewhere harmless.

    Autouse, so that every test in this module starts from an endpoint it can recognise —
    the ones that lay more configuration on top only care about the order of the layers.
    """
    return extra_config(
        "browser.yml",
        "sources:\n  rutracker:\n    connect_url: " + ENDPOINT + "\n",
    )


def test_the_browser_of_a_configured_source_is_closed(
    settings: Settings, closed: list[str]
) -> None:
    cli._close_browsers(settings)

    assert closed == [ENDPOINT]


def test_two_sources_sharing_a_browser_close_it_once(
    extra_config: ExtraConfig,
    closed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(SOURCES, SecondSource.name, SecondSource)
    shared = extra_config(
        "shared.yml",
        dedent(
            f"""
            sources:
              second:
                connect_url: {ENDPOINT}
            """
        ),
    )

    cli._close_browsers(shared)

    assert closed == [ENDPOINT]


def test_a_browser_is_left_alone_when_the_config_says_so(
    extra_config: ExtraConfig, closed: list[str]
) -> None:
    kept = extra_config("keep.yml", "browser:\n  close_on_exit: false\n")

    cli._close_browsers(kept)

    assert closed == []


def test_a_configured_source_with_no_implementation_is_only_reported(
    extra_config: ExtraConfig, closed: list[str]
) -> None:
    # Config validation catches this before anything runs; closing is not the place that
    # should turn a typo into a traceback.
    typo = extra_config("typo.yml", "sources:\n  nowhere:\n    connect_url: http://x/\n")

    cli._close_browsers(typo)

    assert closed == [ENDPOINT]


def _isolated(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """`cli()` with no config files, no log files and no real database."""
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli, "_require_usable_database", lambda _settings: None)


def test_the_browser_is_closed_after_a_run(
    settings: Settings, closed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated(monkeypatch, settings)
    monkeypatch.setattr(cli, "_run_once", lambda _args: 0)

    with pytest.raises(SystemExit) as exit_info:
        cli.cli(["--once"])

    assert exit_info.value.code == 0
    assert closed == [ENDPOINT]


def test_the_browser_is_closed_even_when_the_run_crashes(
    settings: Settings, closed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated(monkeypatch, settings)

    def crash(_args: object, **_kwargs: object) -> int:
        msg = "the run broke"
        raise RuntimeError(msg)

    monkeypatch.setattr(cli, "_run_once", crash)

    # The exception still travels — what must not travel is a browser nobody closes.
    with pytest.raises(RuntimeError, match="the run broke"):
        cli.cli(["--once"])

    assert closed == [ENDPOINT]
