"""Session state: is the browser signed in, and what if it is not.

Signing in is attempted before anyone is asked for help: the login form is filled and
submitted in the browser we attached to, which is a *real* browser on a real profile, so
the site sees the same client a person would be. That works whenever the profile still
holds its Cloudflare clearance — verified end to end against the live tracker. It does not
*always* work: served to a client with no clearance, the site answers with an interstitial
instead of the form, and the form itself can demand a captcha. So a failure is a normal
outcome rather than an error: what is left is a browser window a human can type into, and
:func:`ensure_logged_in` waits for them.

What is *not* needed for the session to survive is the browser staying open: cookies,
``cf_clearance`` included, live in the ``--user-data-dir`` profile, so a graceful close
(the one :func:`ricercar.browser.close_attached` performs) leaves the next launch signed
in.
"""

from __future__ import annotations

import enum

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ricercar.browser import MANUAL_LOGIN_TIMEOUT, wait_for_manual_login
from ricercar.browser.navigation import goto
from ricercar.log import get_logger
from ricercar.sources.base import SessionExpiredError
from ricercar.sources.rutracker.diagnose import CHALLENGE_MARKERS
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors


class LoginOutcome(enum.StrEnum):
    """What came of an attempt to sign in automatically."""

    SIGNED_IN = "signed in"
    NO_FORM = "the login form was not on the page"
    BLOCKED = "the page was a Cloudflare interstitial"
    CAPTCHA = "the form wants a captcha"
    REFUSED = "the form was submitted but no session started"


def login_page_state(html: str, selectors: Selectors = DEFAULT_SELECTORS) -> str:
    """What the page in front of us is asking for.

    One of ``"form"``, ``"captcha"``, ``"challenge"`` or ``"other"``. HTML in, verdict
    out — so a saved page is enough to check this without a browser.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True).lower()

    if any(marker in text or marker in html.lower() for marker in CHALLENGE_MARKERS):
        return "challenge"
    if soup.select_one(selectors.login_username) is None:
        return "other"
    if soup.select_one(selectors.login_captcha) is not None:
        return "captcha"
    return "form"


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


async def log_in(
    page: Page,
    username: str,
    password: str,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> LoginOutcome:
    """Fill the tracker's login form and submit it.

    Returns *why* it did not work rather than raising: every failure mode here (a
    challenge, a captcha, a wrong password) has the same remedy — a person in the browser
    window — so the caller decides what to do about it.
    """
    log = get_logger()

    await goto(page, selectors.login_url)
    state = login_page_state(await page.content(), selectors)
    if state != "form":
        log.warning("Not signing in automatically: {}", state)
        if state == "challenge":
            return LoginOutcome.BLOCKED
        return LoginOutcome.CAPTCHA if state == "captcha" else LoginOutcome.NO_FORM

    log.info("Signing in automatically as '{}'", username)

    # The site keeps a compact login box in the header of every guest page — hidden until
    # you click "Вход" — so the form to fill is whichever one is *visible*: the page's own.
    user_field = page.locator(selectors.login_username).filter(visible=True)
    pass_field = page.locator(selectors.login_password).filter(visible=True)
    if not await user_field.count() or not await pass_field.count():
        log.warning("A login form is in the page but none of it is visible — nothing to fill")
        return LoginOutcome.NO_FORM

    form = user_field.first.locator("xpath=ancestor::form[1]")
    try:
        await user_field.first.fill(username)
        await pass_field.first.fill(password)
        # Enter, rather than a click: that is the submit path a person takes, JavaScript
        # and all.
        await pass_field.first.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1_000)

        if login_page_state(await page.content(), selectors) == "form":
            # Still on the form, so Enter submitted nothing — use the button instead.
            submit = form.locator(selectors.login_submit)
            if await submit.count():
                await submit.first.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1_000)
    except PlaywrightError as exc:
        log.warning("The login form could not be filled: {}", exc)
        return LoginOutcome.REFUSED

    if await is_logged_in(page, username, selectors):
        log.info("Signed in as '{}'", username)
        return LoginOutcome.SIGNED_IN

    log.warning(
        "The login form was submitted but no session started — a wrong password, a "
        "captcha or an interstitial; the page now looks like {}",
        await _page_summary(page),
    )
    return LoginOutcome.REFUSED


async def _page_summary(page: Page) -> str:
    """A short, safe description of what the page is now (never a field's value)."""
    try:
        state = login_page_state(await page.content())
        title = (await page.title()).strip()[:60]
        return f"{state} ({title})" if title else state
    except PlaywrightError:  # pragma: no cover - the page went away
        return "an unreadable page"


async def ensure_logged_in(
    page: Page,
    username: str,
    password: str = "",
    *,
    timeout: float = MANUAL_LOGIN_TIMEOUT,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> None:
    """Open the forum and make sure it holds a session for *username*.

    The order of attempts: reuse the session already in the attached browser; else sign
    in with *username* / *password*; else — a captcha, an interstitial, or no credentials
    configured — ask a person to log in in that window, and wait for them.

    Raises:
        SessionExpiredError: nobody logged in before *timeout*.
    """
    log = get_logger()

    await goto(page, selectors.base_url)
    if await is_logged_in(page, username, selectors):
        log.info("Reusing the browser's signed-in session for '{}'", username)
        return

    if username and password:
        outcome = await log_in(page, username, password, selectors=selectors)
        if outcome is LoginOutcome.SIGNED_IN:
            return
        log.warning("Signing in automatically did not work: {}", outcome.value)
    else:
        log.warning(
            "No session, and no credentials to sign in with — set `login.username` and "
            "`login.password` in config/config.local.yml to have that done for you."
        )

    log.warning("Waiting up to {}s for a login in the attached browser…", int(timeout))
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


__all__ = ["LoginOutcome", "ensure_logged_in", "is_logged_in", "log_in", "login_page_state"]
