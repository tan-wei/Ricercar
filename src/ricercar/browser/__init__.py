"""
Browser access — attach to a browser you launched yourself.

Playwright never launches a browser for this project, and that is a deliberate
decision rather than a workaround for one site. A browser a human started and can
look at is the most reliable client there is: it is not the thing being
fingerprinted as automation, and when a challenge does appear a person solves it
once. Measured on the tracker this project was written for (see the "Cloudflare"
section of ``README.md``): bundled Firefox headless *and* headed, real Edge via
``channel="msedge"``, and ``requests`` replaying cookies exported from a real
Firefox all fail; a browser the user launched by hand gets through, and its
clearance persists in its profile.

So there is exactly one mode: **you** launch your own browser

    chrome.exe --remote-debugging-port=9222 --user-data-dir=<profile dir>

and Playwright attaches to it over CDP. Two consequences worth knowing:

* the existing context is reused as-is — that is what carries the signed-in
  session — so nothing is closed while a run is in flight: the window stays exactly
  as you left it until the command you started is done with it;
* only a browser launched with an explicit ``--user-data-dir`` accepts
  ``--remote-debugging-port`` (Chrome ≥136 ignores the flag on the default
  profile), which is also why your normal browsing profile is untouched.

When the program *exits* it closes that browser again (``browser.close_on_exit``,
true by default) via :func:`close_attached`. Closing is a graceful shutdown, not a
kill: cookies, ``cf_clearance`` and the rest of the session live in the profile
directory, so the next launch comes back signed in — which is exactly why the
profile has to be the same one every time.

Because Playwright's own browsers are never used, ``playwright install`` is not
needed; the ``playwright`` package is required only for its CDP client.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from ricercar.config import Settings, get_settings
from ricercar.log import get_logger

if TYPE_CHECKING:
    from ricercar.sources.base import Source


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """A browser ready to be driven, plus the driver that owns it."""

    playwright: Playwright
    browser: Browser
    context: BrowserContext


# ── Public API ────────────────────────────────────────────────────────────


@asynccontextmanager
async def managed_browser(
    source: Source,
    cfg: Settings | None = None,
) -> AsyncIterator[BrowserSession]:
    """Attach to the browser *source* is configured to use.

    The browser belongs to the user: it is reused as-is and never closed, only the
    Playwright driver is stopped. The endpoint is per source, so two trackers can
    be driven in the same browser (or in two different ones) without the
    application deciding anything.

    Usage::

        async with managed_browser(source) as session:
            page = await session.context.new_page()
            await goto(page, "https://example.org")
    """
    cfg = cfg or get_settings()

    pw = await async_playwright().start()
    try:
        browser = await _attach(pw, source.settings.connect_url)

        # Reusing the existing context is the whole point: a fresh context would be
        # logged out, and the login POST is blocked on the trackers that need this.
        if not browser.contexts:
            msg = "The attached browser has no open context — open a tab in it and retry."
            raise RuntimeError(msg)

        context = browser.contexts[0]
        context.set_default_timeout(cfg.browser.action_timeout)
        context.set_default_navigation_timeout(cfg.browser.navigation_timeout)

        yield BrowserSession(playwright=pw, browser=browser, context=context)
    finally:
        await pw.stop()


async def _attach(pw: Playwright, connect_url: str) -> Browser:
    """Connect to the user's browser over CDP."""
    try:
        return await pw.chromium.connect_over_cdp(connect_url)
    except PlaywrightError as exc:
        msg = (
            f"Cannot attach to {connect_url} — is your browser running?\n"
            f"Start it yourself first, for example:\n"
            f"  chrome.exe --remote-debugging-port=9222 "
            f"--user-data-dir=<profile dir>\n"
            f"See the 'Cloudflare' section in README.md."
        )
        raise RuntimeError(msg) from exc


# ── Closing the browser we attached to ────────────────────────────────────

CLOSE_TIMEOUT = 10.0
"""Seconds to wait for the browser to go away before asking the OS harder."""


def _answers(connect_url: str, *, timeout: float = 1.0) -> bool:
    """Whether something is still listening on the CDP endpoint."""
    try:
        with urllib.request.urlopen(
            f"{connect_url.rstrip('/')}/json/version", timeout=timeout
        ) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


async def close_attached(
    connect_url: str,
    *,
    timeout: float = CLOSE_TIMEOUT,
) -> bool:
    """Close the browser at *connect_url*, and report whether it went away.

    A run only ever *borrows* the browser, which is why this is a separate step the CLI
    takes when it is done: the window stays put for the whole run, and is closed at the
    end (``browser.close_on_exit``).

    Closing is graceful, because the profile has to survive: cookies (``cf_clearance``
    included) are flushed to the ``--user-data-dir`` directory, so the next launch is
    signed in again. Three steps, in order:

    1. ``Browser.close`` over CDP — the browser's own shutdown;
    2. the platform's polite request for a process to end itself (``taskkill`` without
       ``/F`` on Windows, ``SIGTERM`` elsewhere), if the endpoint is still answering;
    3. nothing else: a browser that survives both is left alone rather than killed, since
       a hard kill can cost the very session this is meant to preserve.
    """
    log = get_logger()
    if not _answers(connect_url):
        log.debug("Nothing is listening on {} — no browser to close", connect_url)
        return False

    pid = await _close_over_cdp(connect_url)
    if await _wait_until_gone(connect_url, timeout=timeout):
        log.info("Closed the browser we attached to ({})", connect_url)
        return True

    if pid is None:
        log.warning("The browser at {} is still running and its pid is unknown", connect_url)
        return False

    log.debug("Browser.close did not take — asking the OS to end pid {}", pid)
    _terminate_gracefully(pid)
    if await _wait_until_gone(connect_url, timeout=timeout):
        log.info("Closed the browser we attached to (pid {})", pid)
        return True

    log.warning(
        "Could not close the browser at {} — close it yourself when convenient "
        "(killing it outright would risk the signed-in profile)",
        connect_url,
    )
    return False


async def _close_over_cdp(connect_url: str) -> int | None:
    """Send ``Browser.close`` and return the browser's own pid (when it says)."""
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(connect_url)
        session = await browser.new_browser_cdp_session()
        try:
            info = await session.send("SystemInfo.getProcessInfo")
        except PlaywrightError:  # pragma: no cover - not every Chromium answers this
            info = {}
        processes = info.get("processInfo", []) if isinstance(info, dict) else []
        pid = next((p["id"] for p in processes if p.get("type") == "browser"), None)
        try:
            await session.send("Browser.close")
        except PlaywrightError as exc:
            get_logger().debug("Browser.close was refused: {}", exc)
        return int(pid) if pid is not None else None
    except PlaywrightError as exc:
        get_logger().debug("Could not ask the browser to close: {}", exc)
        return None
    finally:
        await pw.stop()


async def _wait_until_gone(connect_url: str, *, timeout: float) -> bool:
    """Poll the CDP endpoint until nothing answers (or *timeout* elapses)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _answers(connect_url):
            return True
        await asyncio.sleep(0.5)
    return False


def _terminate_gracefully(pid: int) -> None:
    """Ask the OS to end *pid* the way clicking the window's X would."""
    if sys.platform == "win32":
        # Without /F: Windows posts WM_CLOSE, so Chrome shuts down and flushes its profile.
        subprocess.run(["taskkill", "/PID", str(pid)], check=False, capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:  # pragma: no cover - already gone
            get_logger().debug("Could not signal pid {}: {}", pid, exc)


# ── Waiting for a human ───────────────────────────────────────────────────

MANUAL_LOGIN_TIMEOUT = 300.0
"""Seconds to wait for someone to complete a login in the attached browser."""


async def wait_for_manual_login(
    page: Page,
    is_logged_in: Callable[[Page], Awaitable[bool]],
    *,
    timeout: float = MANUAL_LOGIN_TIMEOUT,
    interval: float = 3.0,
    remind_every: float = 30.0,
) -> bool:
    """Poll until ``is_logged_in(page)`` is true, or ``timeout`` elapses.

    A login cannot always be automated (Cloudflare blocks every ``login.php`` POST
    on such a tracker), so when a session expires mid-run the only way forward is
    for a human to log in in the browser window — which means waiting for them
    rather than failing. Waiting for minutes in silence would look like a hang, so
    the remaining time is repeated every ``remind_every`` seconds. Returns ``False``
    on timeout.

    ``is_logged_in`` keeps this module site-agnostic; it should return ``False``
    rather than raise while the human is navigating the page.
    """
    log = get_logger()
    deadline = time.monotonic() + timeout
    remind_at = time.monotonic() + remind_every

    while time.monotonic() < deadline:
        if await is_logged_in(page):
            return True
        if time.monotonic() >= remind_at:
            log.warning("Still waiting for a login — {}s left", int(deadline - time.monotonic()))
            remind_at = time.monotonic() + remind_every
        await asyncio.sleep(interval)
    return False


__all__ = [
    "CLOSE_TIMEOUT",
    "MANUAL_LOGIN_TIMEOUT",
    "BrowserSession",
    "close_attached",
    "managed_browser",
    "wait_for_manual_login",
]
