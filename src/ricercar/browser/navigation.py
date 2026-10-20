"""Navigation plumbing shared by every source."""

from __future__ import annotations

import asyncio
import time

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeout
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ricercar.log import get_logger

NAVIGATION_TIMEOUT = 30_000
"""Milliseconds to wait for a page to become usable."""

CF_WAIT_TIMEOUT = 90_000
"""Milliseconds to wait for a Cloudflare challenge to clear before giving up."""

CF_POLL_INTERVAL = 1.0
"""Seconds between each poll while Cloudflare is still on screen."""

_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl-",
    "checking your browser",
    "attention required",
)
"""Text that only appears on a Cloudflare interstitial."""


@retry(
    retry=retry_if_exception_type(PlaywrightTimeout),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def goto(page: Page, url: str) -> Response | None:
    """Navigate to *url*, retrying the transient timeouts the site throws."""
    return await page.goto(url, wait_until="domcontentloaded")


async def _is_challenge_page(page: Page) -> bool:
    """Quick check — does the current page look like a Cloudflare challenge?

    Uses the same markers as :mod:`ricercar.sources.rutracker.diagnose` but works
    directly on the page DOM (no BeautifulSoup overhead when polling rapidly).
    """
    try:
        text = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return False
    text_lower = text.lower()
    return any(marker in text_lower for marker in _CF_CHALLENGE_MARKERS)


async def wait_for_cloudflare(page: Page, *, timeout: float = CF_WAIT_TIMEOUT) -> bool:
    """Wait for a Cloudflare challenge interstitial to clear.

    When reusing an attached browser that holds a valid ``cf_clearance`` cookie,
    Cloudflare still runs a quick check (1–5 s) before letting the request through.
    This function polls the page until the challenge markers are gone or *timeout*
    elapses, and returns whether Cloudflare cleared.

    Safe to call on any page — if no challenge is detected, returns ``True``
    immediately.
    """
    log = get_logger()
    deadline = time.monotonic() + timeout / 1000.0

    while time.monotonic() < deadline:
        if not await _is_challenge_page(page):
            return True
        remaining = int(deadline - time.monotonic())
        if remaining > 0 and remaining % 5 == 0:
            log.info("Waiting for Cloudflare to clear ({}s left)…", remaining)
        await asyncio.sleep(CF_POLL_INTERVAL)

    return False


__all__ = [
    "NAVIGATION_TIMEOUT",
    "CF_WAIT_TIMEOUT",
    "goto",
    "wait_for_cloudflare",
]
