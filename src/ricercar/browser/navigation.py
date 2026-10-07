"""Navigation plumbing shared by every source."""

from __future__ import annotations

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeout
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

NAVIGATION_TIMEOUT = 30_000
"""Milliseconds to wait for a page to become usable."""


@retry(
    retry=retry_if_exception_type(PlaywrightTimeout),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def goto(page: Page, url: str) -> Response | None:
    """Navigate to *url*, retrying the transient timeouts the site throws."""
    return await page.goto(url, wait_until="domcontentloaded")


__all__ = ["NAVIGATION_TIMEOUT", "goto"]
