"""Session state: is the browser signed in, and what if it is not.

A login can never be automated — Cloudflare blocks every ``login.php`` POST — so
when the session has expired the only way forward is for a human to log in in the
browser we attached to. That makes a session expiry a normal event to wait out,
not an error to raise: :func:`ensure_logged_in` waits, and only gives up
(``SessionExpiredError``) once nobody has logged in.
"""

from __future__ import annotations

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ricercar.browser import MANUAL_LOGIN_TIMEOUT, wait_for_manual_login
from ricercar.browser.navigation import goto
from ricercar.log import get_logger
from ricercar.sources.base import SessionExpiredError
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors


async def is_logged_in(
    page: Page,
    username: str,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> bool:
    """Whether *page* shows a session belonging to *username*."""
    marker = page.locator(selectors.logged_in)
    try:
        if await marker.count() == 0:
            return False
        return username.lower() in (await marker.first.inner_text()).strip().lower()
    except PlaywrightError:
        # The page was mid-navigation while we polled — report "not yet".
        return False


async def ensure_logged_in(
    page: Page,
    username: str,
    *,
    timeout: float = MANUAL_LOGIN_TIMEOUT,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> None:
    """Open the forum and make sure it holds a session for *username*.

    On a missing session the human is asked to log in in the attached browser and
    we wait for them.

    Raises:
        SessionExpiredError: nobody logged in before *timeout*.
    """
    log = get_logger()

    await goto(page, selectors.base_url)
    if await is_logged_in(page, username, selectors):
        log.info("Reusing the browser's signed-in session for '{}'", username)
        return

    log.warning(
        "No live session for '{}' in the attached browser — log in there by hand "
        "(a programmatic login is blocked by Cloudflare).",
        username,
    )
    log.warning("Waiting up to {}s for the login to complete…", int(timeout))

    if await wait_for_manual_login(
        page,
        lambda current: is_logged_in(current, username, selectors),
        timeout=timeout,
    ):
        log.info("Session for '{}' is live", username)
        return

    msg = (
        f"Timed out after {int(timeout)}s waiting for a login as {username!r}.\n"
        f"Log in in the browser window you attached to, then re-run "
        f"(or allow more time)."
    )
    raise SessionExpiredError(msg)


__all__ = ["ensure_logged_in", "is_logged_in"]
