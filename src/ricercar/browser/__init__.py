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
  session — so nothing is closed on exit; the window stays exactly as you left it;
* only a browser launched with an explicit ``--user-data-dir`` accepts
  ``--remote-debugging-port`` (Chrome ≥136 ignores the flag on the default
  profile), which is also why your normal browsing profile is untouched.

Because Playwright's own browsers are never used, ``playwright install`` is not
needed; the ``playwright`` package is required only for its CDP client.
"""

from __future__ import annotations

import asyncio
import time
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
    "MANUAL_LOGIN_TIMEOUT",
    "BrowserSession",
    "managed_browser",
    "wait_for_manual_login",
]
