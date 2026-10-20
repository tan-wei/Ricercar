"""Closing the browser we borrowed, from the program's side.

Whether a *close* works is a question only the live browser can answer (the tests marked
``integration`` ask it). What is answerable here is *when* a close is attempted: once per
endpoint however many sources share it, not at all when the configuration says to leave the
browser alone, and whatever way the run ended — a run that crashes still hands the browser
back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from textwrap import dedent
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from ricercar import browser as browser_module
from ricercar import cli
from ricercar.browser import navigation
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


@pytest.fixture
def browser_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    driver = MagicMock()
    driver.stop = AsyncMock()
    browser = MagicMock()
    browser.contexts = [Mock()]
    driver.chromium.connect_over_cdp = AsyncMock(return_value=browser)
    session = MagicMock()
    session.send = AsyncMock(return_value={})
    browser.new_browser_cdp_session = AsyncMock(return_value=session)
    manager = Mock()
    manager.start = AsyncMock(return_value=driver)
    monkeypatch.setattr(browser_module, "async_playwright", lambda: manager)
    return driver, browser, session


@pytest.mark.parametrize("failure", [None, "body", "context", "connection"])
def test_managed_browser_reuses_context_and_always_stops_driver(
    browser_backend: tuple[Any, Any, Any],
    settings: Settings,
    failure: str | None,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    driver, browser, _ = browser_backend
    context = browser.contexts[0]
    if failure == "context":
        browser.contexts = []
    elif failure == "connection":
        driver.chromium.connect_over_cdp.side_effect = PlaywrightError("not listening")
    source = SecondSource(SourceSettings(connect_url=ENDPOINT))

    async def use() -> None:
        async with browser_module.managed_browser(source, settings) as attached:
            assert attached.context is context
            assert attached.browser is browser
            assert attached.playwright is driver
            if failure == "body":
                raise ValueError("body failed")

    if failure:
        expected = {
            "body": "body failed",
            "context": "no open context",
            "connection": "Cannot attach",
        }
        with pytest.raises(
            ValueError if failure == "body" else RuntimeError, match=expected[failure]
        ):
            asyncio.run(use())
    else:
        asyncio.run(use())
        context.set_default_timeout.assert_called_once_with(settings.browser.action_timeout)
        context.set_default_navigation_timeout.assert_called_once_with(
            settings.browser.navigation_timeout
        )
    driver.stop.assert_awaited_once()
    driver.chromium.launch.assert_not_called()
    browser.new_context.assert_not_called()
    browser.close.assert_not_called()


@pytest.mark.parametrize(
    ("available", "pid", "waits", "expected", "terminate"),
    [
        (False, None, [], False, False),
        (True, 123, [True], True, False),
        (True, None, [False], False, False),
        (True, 123, [False, True], True, True),
        (True, 123, [False, False], False, True),
    ],
)
def test_close_attached_uses_only_graceful_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    pid: int | None,
    waits: list[bool],
    expected: bool,
    terminate: bool,
) -> None:
    monkeypatch.setattr(browser_module, "_answers", Mock(return_value=available))
    close = AsyncMock(return_value=pid)
    wait = AsyncMock(side_effect=waits)
    signal = Mock()
    monkeypatch.setattr(browser_module, "_close_over_cdp", close)
    monkeypatch.setattr(browser_module, "_wait_until_gone", wait)
    monkeypatch.setattr(browser_module, "_terminate_gracefully", signal)
    assert asyncio.run(browser_module.close_attached(ENDPOINT, timeout=2)) is expected
    assert wait.await_count == len(waits)
    if terminate:
        signal.assert_called_once_with(pid)
    else:
        signal.assert_not_called()
    if not available:
        close.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "connect", "info", "close", "no-pid"])
def test_cdp_close_extracts_browser_pid_and_stops_driver_on_failure(
    browser_backend: tuple[Any, Any, Any],
    failure: str | None,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    driver, _, session = browser_backend
    info = {"processInfo": [{"type": "renderer", "id": 1}, {"type": "browser", "id": 123}]}
    if failure == "connect":
        driver.chromium.connect_over_cdp.side_effect = PlaywrightError("disconnected")
    session.send.side_effect = [
        PlaywrightError("unsupported")
        if failure == "info"
        else ({} if failure == "no-pid" else info),
        PlaywrightError("refused") if failure == "close" else {},
    ]
    pid = asyncio.run(browser_module._close_over_cdp(ENDPOINT))
    assert pid == (None if failure in ("connect", "info", "no-pid") else 123)
    driver.stop.assert_awaited_once()
    if failure != "connect":
        assert [call.args[0] for call in session.send.call_args_list] == [
            "SystemInfo.getProcessInfo",
            "Browser.close",
        ]


@pytest.mark.parametrize("response", [200, 503, "unreachable"])
def test_endpoint_probe_handles_http_status_and_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
    response: int | str,
) -> None:
    from urllib.error import URLError

    connection = MagicMock()
    connection.__enter__.return_value.status = response
    open_url = Mock(return_value=connection)
    if response == "unreachable":
        open_url.side_effect = URLError("offline")
    monkeypatch.setattr(browser_module.urllib.request, "urlopen", open_url)
    assert browser_module._answers(ENDPOINT + "/") is (response == 200)
    open_url.assert_called_once_with(ENDPOINT + "/json/version", timeout=1.0)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_os_close_never_uses_force_kill(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    import signal

    terminate = Mock()
    taskkill = Mock()
    monkeypatch.setattr(browser_module.os, "kill", terminate)
    monkeypatch.setattr(browser_module.subprocess, "run", taskkill)
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(browser_module.sys, "platform", platform)
        browser_module._terminate_gracefully(123)
    if platform == "win32":
        taskkill.assert_called_once_with(
            ["taskkill", "/PID", "123"], check=False, capture_output=True
        )
        terminate.assert_not_called()
    else:
        terminate.assert_called_once_with(123, signal.SIGTERM)
        taskkill.assert_not_called()


@pytest.fixture
def polling_clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = SimpleNamespace(now=0.0, sleeps=[])

    async def advance(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds

    for module in (browser_module, navigation):
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=advance))
    return clock


@pytest.mark.parametrize("gone", [True, False])
def test_endpoint_polling_finishes_or_times_out_without_real_sleep(
    polling_clock: Any,
    monkeypatch: pytest.MonkeyPatch,
    gone: bool,
) -> None:
    probe = Mock(side_effect=[True, False] if gone else None, return_value=True)
    monkeypatch.setattr(browser_module, "_answers", probe)
    assert asyncio.run(browser_module._wait_until_gone(ENDPOINT, timeout=1)) is gone
    assert polling_clock.now == (0.5 if gone else 1)


@pytest.mark.parametrize("success", [True, False])
def test_manual_login_polling_waits_reminds_and_times_out(
    polling_clock: Any,
    success: bool,
    log_messages: list[str],
) -> None:
    page = object()
    check = AsyncMock(side_effect=[False, False, True] if success else None, return_value=False)
    assert (
        asyncio.run(
            browser_module.wait_for_manual_login(
                page,
                check,
                timeout=4,
                interval=1,
                remind_every=1,
            )
        )
        is success
    )
    assert polling_clock.now == (2 if success else 4)
    assert any("Still waiting for a login" in message for message in log_messages)


@pytest.mark.parametrize("state", ["clear", "clears", "blocked", "unreadable"])
def test_cloudflare_waits_for_clearance_or_times_out(
    polling_clock: Any,
    state: str,
    log_messages: list[str],
) -> None:
    from playwright.async_api import Error as PlaywrightError

    page = Mock()
    text = AsyncMock(return_value="Just a moment")
    if state == "clear":
        text.return_value = "Search results"
    elif state == "clears":
        text.side_effect = ["CHECKING YOUR BROWSER", "Search results"]
    elif state == "unreadable":
        text.side_effect = PlaywrightError("navigation in progress")
    page.locator.return_value.inner_text = text
    assert asyncio.run(navigation.wait_for_cloudflare(page, timeout=6_000)) is (state != "blocked")
    assert (
        len(polling_clock.sleeps) == {"clear": 0, "clears": 1, "blocked": 6, "unreadable": 0}[state]
    )
    if state == "blocked":
        assert any("Waiting for Cloudflare" in message for message in log_messages)


@pytest.mark.parametrize("failure", ["recovers", "timeout", "other"])
def test_navigation_retries_only_timeouts(failure: str) -> None:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from tenacity import wait_none

    page = Mock()
    response = object()
    page.goto = AsyncMock(
        side_effect=(
            [PlaywrightTimeout("slow"), PlaywrightTimeout("slow"), response]
            if failure == "recovers"
            else PlaywrightTimeout("slow")
            if failure == "timeout"
            else PlaywrightError("bad URL")
        )
    )
    sleep = AsyncMock()
    navigate = navigation.goto.retry_with(wait=wait_none(), sleep=sleep)
    if failure == "recovers":
        assert asyncio.run(navigate(page, "https://tracker.example")) is response
    else:
        with pytest.raises(PlaywrightError):
            asyncio.run(navigate(page, "https://tracker.example"))
    assert page.goto.await_count == (1 if failure == "other" else 3)
    assert sleep.await_count == (0 if failure == "other" else 2)
