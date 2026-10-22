"""The rutracker adapter, checked against pages captured from the live site."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup

from ricercar.config import SearchTask, SourceSettings
from ricercar.models import SearchPage
from ricercar.sources.base import SelectorsBrokenError, SiteUnderMaintenanceError
from ricercar.sources.rutracker import RutrackerSource, parse_results, uploaders
from ricercar.sources.rutracker import session as session_module
from ricercar.sources.rutracker.diagnose import diagnose_results, diagnose_topic
from ricercar.sources.rutracker.search import (
    _raise_if_unusable,
    iter_result_pages,
    next_page_url,
    page_count,
)
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors, load_selectors
from ricercar.sources.rutracker.session import LoginOutcome, login_page_state
from ricercar.sources.rutracker.topic import open_topic, parse_topic

RESULTS_URL = "https://rutracker.org/forum/tracker.php?search_id=abc&nm=Bach"
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t=1580744"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHALLENGE_HTML = "<html><body><h1>Just a moment...</h1></body></html>"
LOGIN_HTML = '<html><body><form><input name="login_username"></form></body></html>'
RENAMED_HTML = "<html><body><p>Результатов поиска: 500</p><a href='/x'>row</a></body></html>"
PLAIN_HTML = "<html><body><p>nothing much here</p></body></html>"

PAGE_SIZE = 50
"""Rows per results page; the pager's ``start`` parameter is a multiple of it."""


def _results_html(number: int, total: int, *, dead_next: bool = False) -> str:
    """A results page shaped like the real one: header, one row, pager, "page N of M".

    *dead_next* adds the "След." link a last page may still carry, pointing at a page that
    does not exist.
    """
    offsets = [(page - 1) * PAGE_SIZE for page in range(1, total + 1)]
    pager = "".join(
        f'<a class="pg" href="https://rutracker.org/forum/tracker.php?search_id=abc'
        f'&amp;start={offset}">{offset // PAGE_SIZE + 1}</a>'
        for offset in offsets
    )
    if dead_next:
        pager += (
            f'<a class="pg" href="https://rutracker.org/forum/tracker.php?search_id=abc'
            f'&amp;start={total * PAGE_SIZE}">След.</a>'
        )
    return (
        "<html><body>"
        "<p>Результатов поиска: 500</p>"
        f"<p>Страница <b>{number}</b> из <b>{total}</b></p>"
        "<table><tr>"
        '<td class="u-name-col">someone</td>'
        f'<td><a data-topic_id="t{number}" href="/forum/viewtopic.php?t={number}">'
        f"Title {number}</a></td>"
        "<td>1.5 GB</td>"
        "</tr></table>"
        f"<p>{pager}</p>"
        "</body></html>"
    )


class FakePager:
    """As much of a Playwright page as the walk touches, over synthetic pages.

    Serves the page whose ``start`` offset is in the current URL, so following the
    pager is what makes the next page appear — and counts the visits, which is how a
    test sees whether one page too many was fetched.
    """

    def __init__(self, total: int, *, dead_next: bool = False) -> None:
        self.total = total
        self.dead_next = dead_next
        self.url = f"{RESULTS_URL}&start=0"
        self.visited: list[str] = [self.url]

    async def content(self) -> str:
        start = int(parse_qs(urlparse(self.url).query).get("start", ["0"])[0])
        number = start // PAGE_SIZE + 1
        return _results_html(number, self.total, dead_next=self.dead_next)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = url
        self.visited.append(url)

    async def wait_for_function(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _walk(page: FakePager, *, max_pages: int) -> list[SearchPage]:
    async def collect() -> list[SearchPage]:
        return [results async for results in iter_result_pages(page, max_pages=max_pages)]

    return asyncio.run(collect())


# ── Parsing a results page ────────────────────────────────────────────────


def test_a_results_page_parses_into_hits(search_html: str) -> None:
    hits = parse_results(search_html)

    assert len(hits) == 50
    assert len({hit.topic_id for hit in hits}) == 50
    for hit in hits:
        assert hit.title
        assert hit.author
        assert hit.size
        assert hit.topic_id in hit.url
        assert hit.url.startswith("https://rutracker.org/forum/viewtopic.php?t=")


def test_the_uploaders_are_people_and_not_titles(search_html: str) -> None:
    # legacy collected authors with the classes the title link shares, so its "authors"
    # set ended up full of titles; the uploader is read from the row's cell instead.
    hits = parse_results(search_html)
    names = uploaders(search_html)

    assert names
    assert not names & {hit.title for hit in hits}


def test_the_pager_offers_the_page_after_this_one(search_html: str) -> None:
    url = next_page_url(search_html, RESULTS_URL)

    assert url is not None
    assert "start=50" in url
    assert "nm=Bach" in url


def test_there_is_no_page_after_the_last_one(search_html: str) -> None:
    assert next_page_url(search_html, f"{RESULTS_URL}&start=1500") is None


def test_an_override_changes_how_the_page_is_read(search_html: str) -> None:
    selectors = Selectors.from_mapping({"result_link": "a.not-here"})

    assert parse_results(search_html, selectors) == []
    assert any("no row matches" in issue for issue in diagnose_results(search_html, selectors))


# ── How many pages a search has, and how far the walk goes ────────────────


def test_the_pager_says_how_many_pages_the_search_has(search_html: str) -> None:
    # "Страница 1 из 10": a fact about the search, and the one the progress bar needs —
    # the configured ceiling only says how far we are *allowed* to follow it.
    assert page_count(search_html) == (1, 10)


def test_a_page_without_a_pager_says_nothing() -> None:
    assert page_count(RENAMED_HTML) is None


def test_the_walk_reports_the_pagers_own_count_on_every_page() -> None:
    pages = _walk(FakePager(total=10), max_pages=10)

    assert [results.number for results in pages] == list(range(1, 11))
    assert {results.total for results in pages} == {10}
    assert all(results.hits for results in pages)


def test_the_walk_stops_where_the_pager_says_the_search_ends() -> None:
    # A last page may still offer a "next" link; the count the pager states wins, so the
    # page that link points at is never fetched.
    page = FakePager(total=3, dead_next=True)

    pages = _walk(page, max_pages=10)

    assert [results.number for results in pages] == [1, 2, 3]
    assert len(page.visited) == 3


def test_the_walk_still_stops_at_max_pages() -> None:
    # The ceiling is the caller's, and a search longer than it is cut short — but the
    # pages that were read still report how many the search really has.
    pages = _walk(FakePager(total=10), max_pages=2)

    assert [results.number for results in pages] == [1, 2]
    assert pages[-1].total == 10


# ── Parsing a topic page ──────────────────────────────────────────────────


def test_a_topic_page_reports_its_title_and_availability(topic_html: str) -> None:
    info = parse_topic(topic_html, TOPIC_URL)

    assert info.url == TOPIC_URL
    assert info.downloadable is True
    assert info.title


# ── Diagnoses ─────────────────────────────────────────────────────────────


def test_the_captured_pages_look_healthy(search_html: str, topic_html: str) -> None:
    assert diagnose_results(search_html) == ()
    assert diagnose_topic(topic_html) == ()


def test_a_challenge_page_is_recognised() -> None:
    issues = diagnose_results(CHALLENGE_HTML)

    assert any("Cloudflare challenge" in issue for issue in issues)


def test_a_login_form_is_recognised() -> None:
    assert any("login form" in issue for issue in diagnose_results(LOGIN_HTML))


def test_a_results_page_without_rows_is_recognised() -> None:
    issues = diagnose_results(RENAMED_HTML)

    assert any("no row matches" in issue for issue in issues)


def test_a_page_that_is_not_a_topic_is_recognised() -> None:
    issues = diagnose_topic(PLAIN_HTML)

    assert any("not a topic page" in issue for issue in issues)


MAINTENANCE_HTML = (
    "<html><body><p>the site is under maintenance, please come back later</p></body></html>"
)
MAINTENANCE_TOPIC_HTML = (
    "<html><body><p>Under maintenance</p><a href='viewtopic.php?t=1'>topic</a></body></html>"
)


def test_a_maintenance_page_is_recognised_on_results() -> None:
    issues = diagnose_results(MAINTENANCE_HTML)

    assert any("under maintenance" in issue for issue in issues)


def test_a_maintenance_page_is_recognised_on_topic() -> None:
    # A topic page that also says "maintenance" is marked as maintenance, not as broken.
    issues = diagnose_topic(MAINTENANCE_TOPIC_HTML)

    assert any("under maintenance" in issue for issue in issues)


def test_a_maintenance_page_is_not_mistaken_for_a_challenge_first() -> None:
    # Cloudflare markers are checked before maintenance markers, so a page with both
    # is identified as a challenge.  Maintenance is a secondary diagnosis.
    html = (
        "<html><body><p>Just a moment... checking your browser before maintenance</p></body></html>"
    )

    issues = diagnose_results(html)

    assert not any("maintenance" in issue for issue in issues)
    assert any("Cloudflare" in issue for issue in issues)


# ── The login page ────────────────────────────────────────────────────────


def login_page(body: str) -> str:
    """A login page shaped the way the site's own form is: two fields, a button."""
    return (
        "<html><body><form action='login.php' method='post'>"
        "<table><tr><td>Логин</td><td>" + body + "</td></tr></table>"
        "</form></body></html>"
    )


LOGIN_FIELDS = (
    '<input type="text" name="login_username" id="login-username">'
    '<input type="password" name="login_password" id="login-password">'
    '<input type="submit" value="Вход">'
)
CAPTCHA_FIELDS = LOGIN_FIELDS + '<img src="captcha.php?sid=1"><input name="cap_code" type="text">'


def test_the_login_form_is_recognised_as_such() -> None:
    assert login_page_state(login_page(LOGIN_FIELDS)) == "form"


def test_a_captcha_on_the_login_form_is_recognised() -> None:
    # Nothing automated can solve this, so the outcome has to change with the page.
    assert login_page_state(login_page(CAPTCHA_FIELDS)) == "captcha"


def test_a_real_cloudflare_interstitial_is_recognised() -> None:
    # A page captured from the live site: the tracker serves this to a cookie-less client,
    # and its title is "请稍候…" — the markers are the structural ones, not the wording.
    html = (FIXTURES / "html" / "login" / "challenge.html").read_text(encoding="utf-8")

    assert login_page_state(html) == "challenge"


def test_a_page_that_is_neither_is_not_mistaken_for_a_form() -> None:
    assert login_page_state(PLAIN_HTML) == "other"


def real_login_page() -> str:
    """The login page as the site actually serves it (captured while signed out)."""
    return (FIXTURES / "html" / "login" / "login.html").read_text(encoding="utf-8")


def test_the_real_login_page_is_recognised_as_a_form() -> None:
    assert login_page_state(real_login_page()) == "form"


def test_every_form_on_the_real_login_page_has_the_fields_it_needs() -> None:
    # The header carries a second, compact login box that stays hidden until you click
    # "Вход", so the plain selectors match twice — and the one to fill is whichever is
    # visible. What has to hold either way is that the fields and the submit control sit
    # together in the same form, which is where `log_in` looks for the button.
    soup = BeautifulSoup(real_login_page(), "lxml")

    forms = [field.find_parent("form") for field in soup.select(DEFAULT_SELECTORS.login_username)]

    assert len(forms) == 2
    for form in forms:
        assert form is not None
        for selector in (
            DEFAULT_SELECTORS.login_username,
            DEFAULT_SELECTORS.login_password,
            DEFAULT_SELECTORS.login_submit,
        ):
            assert len(form.select(selector)) == 1, selector


def test_the_outcomes_are_all_described() -> None:
    # The value is logged verbatim when an automatic login fails, so every one of them has
    # to read as a sentence.
    for outcome in LoginOutcome:
        assert outcome.value and outcome.value[0].islower()
    assert LoginOutcome.SIGNED_IN.value == "signed in"


@pytest.fixture
def login_controls(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Any, Any]:
    page = MagicMock()
    page.content = AsyncMock(return_value=login_page(LOGIN_FIELDS))
    page.title = AsyncMock(return_value="Sign in")
    page.wait_for_load_state = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    username, password, submit, marker = (MagicMock() for _ in range(4))
    for field in (username, password):
        field.filter.return_value = field
        field.count = AsyncMock(return_value=1)
        field.first.fill = AsyncMock()
        field.first.press = AsyncMock()
    submit.count = AsyncMock(return_value=1)
    submit.first.click = AsyncMock()
    marker.count = AsyncMock(return_value=1)
    marker.first.inner_text = AsyncMock(return_value=" Tester ")
    username.first.locator.return_value.locator.return_value = submit
    locators = {
        DEFAULT_SELECTORS.login_username: username,
        DEFAULT_SELECTORS.login_password: password,
        DEFAULT_SELECTORS.logged_in: marker,
    }
    page.locator.side_effect = locators.__getitem__
    monkeypatch.setattr(session_module, "goto", AsyncMock())
    return page, username, password, submit, marker


@pytest.mark.parametrize("state", ["signed-in", "absent", "other-user", "navigating"])
def test_login_marker_handles_missing_wrong_and_navigating_pages(
    login_controls: tuple[Any, Any, Any, Any, Any],
    state: str,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    page, _, _, _, marker = login_controls
    if state == "absent":
        marker.count.return_value = 0
    elif state == "other-user":
        marker.first.inner_text.return_value = "someone else"
    elif state == "navigating":
        marker.count.side_effect = PlaywrightError("navigating")
    assert asyncio.run(session_module.is_logged_in(page, "tester")) is (state == "signed-in")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (CHALLENGE_HTML, LoginOutcome.BLOCKED),
        (login_page(CAPTCHA_FIELDS), LoginOutcome.CAPTCHA),
        (PLAIN_HTML, LoginOutcome.NO_FORM),
    ],
)
def test_automatic_login_does_not_fill_challenge_captcha_or_unrelated_pages(
    login_controls: tuple[Any, Any, Any, Any, Any],
    html: str,
    expected: LoginOutcome,
) -> None:
    page, username, password, _, _ = login_controls
    page.content.return_value = html
    assert asyncio.run(session_module.log_in(page, "tester", "test-password")) is expected
    username.first.fill.assert_not_awaited()
    password.first.fill.assert_not_awaited()


@pytest.mark.parametrize(
    "mode",
    ["enter", "button", "hidden-user", "hidden-password", "fill-error", "refused", "no-button"],
)
def test_automatic_login_submission_paths_and_failures(
    login_controls: tuple[Any, Any, Any, Any, Any],
    mode: str,
    log_messages: list[str],
) -> None:
    from playwright.async_api import Error as PlaywrightError

    page, username, password, submit, marker = login_controls
    if mode == "enter":
        page.content.side_effect = [login_page(LOGIN_FIELDS), PLAIN_HTML]
    elif mode == "hidden-user":
        username.count.return_value = 0
    elif mode == "hidden-password":
        password.count.return_value = 0
    elif mode == "fill-error":
        username.first.fill.side_effect = PlaywrightError("cannot fill")
    elif mode in ("refused", "no-button"):
        marker.count.return_value = 0
        if mode == "no-button":
            submit.count.return_value = 0
    outcome = asyncio.run(session_module.log_in(page, "tester", "not-a-real-password"))
    if mode in ("enter", "button"):
        assert outcome is LoginOutcome.SIGNED_IN
        username.first.fill.assert_awaited_once_with("tester")
        password.first.fill.assert_awaited_once_with("not-a-real-password")
        password.first.press.assert_awaited_once_with("Enter")
        username.filter.assert_called_once_with(visible=True)
        password.filter.assert_called_once_with(visible=True)
        if mode == "button":
            submit.first.click.assert_awaited_once()
        else:
            submit.first.click.assert_not_awaited()
    elif mode.startswith("hidden"):
        assert outcome is LoginOutcome.NO_FORM
        username.first.fill.assert_not_awaited()
    else:
        assert outcome is LoginOutcome.REFUSED
    assert all("not-a-real-password" not in message for message in log_messages)


@pytest.mark.parametrize("mode", ["reuse", "auto", "manual", "captcha", "timeout", "cloudflare"])
def test_ensure_login_orders_navigation_clearance_and_session_recovery(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    log_messages: list[str],
) -> None:
    from unittest.mock import Mock

    from ricercar.sources.base import SessionExpiredError

    page = object()
    calls = Mock()
    activity = Mock()
    monkeypatch.setattr(session_module, "report_activity", activity)
    navigate = AsyncMock()
    clearance = AsyncMock(return_value=mode != "cloudflare")
    check = AsyncMock(return_value=mode == "reuse")
    automatic = AsyncMock(
        return_value=LoginOutcome.SIGNED_IN if mode == "auto" else LoginOutcome.CAPTCHA
    )
    manual = AsyncMock(return_value=mode != "timeout")
    for name, function in (
        ("goto", navigate),
        ("wait_for_cloudflare", clearance),
        ("is_logged_in", check),
        ("log_in", automatic),
        ("wait_for_manual_login", manual),
    ):
        calls.attach_mock(function, name)
        monkeypatch.setattr(session_module, name, function)
    password = "" if mode == "manual" else "test-password"
    if mode == "timeout":
        with pytest.raises(SessionExpiredError, match="Timed out after 7s"):
            asyncio.run(session_module.ensure_logged_in(page, "tester", password, timeout=7))
    else:
        asyncio.run(session_module.ensure_logged_in(page, "tester", password, timeout=7))
    order = [call[0] for call in calls.mock_calls]
    assert order[:3] == ["goto", "wait_for_cloudflare", "is_logged_in"]
    if mode == "reuse":
        automatic.assert_not_awaited()
        manual.assert_not_awaited()
    elif mode == "auto":
        manual.assert_not_awaited()
    else:
        activity.assert_any_call(
            "Waiting for manual login",
            wait_seconds=7,
            issue="Manual login required; sign in and complete any captcha or Cloudflare "
            "verification in the attached browser.",
        )
        if mode != "timeout":
            activity.assert_any_call(
                "Signed-in session ready",
                issue="Manual login completed; the run is continuing.",
            )
        assert order[-1] == "wait_for_manual_login"
        assert manual.call_args.kwargs["timeout"] == 7
        check.return_value = True
        assert asyncio.run(manual.call_args.args[1](page)) is True
        if mode == "manual":
            automatic.assert_not_awaited()
    if mode == "cloudflare":
        assert any("Cloudflare challenge did not clear" in message for message in log_messages)


def test_ensure_login_propagates_navigation_failure_without_attempting_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    automatic = AsyncMock()
    monkeypatch.setattr(session_module, "goto", AsyncMock(side_effect=PlaywrightError("offline")))
    monkeypatch.setattr(session_module, "log_in", automatic)
    with pytest.raises(PlaywrightError, match="offline"):
        asyncio.run(session_module.ensure_logged_in(object(), "tester", "test-password"))
    automatic.assert_not_awaited()


# ── Selectors ─────────────────────────────────────────────────────────────


def test_relative_links_are_resolved_against_the_forum() -> None:
    assert (
        DEFAULT_SELECTORS.absolute("viewtopic.php?t=1")
        == "https://rutracker.org/forum/viewtopic.php?t=1"
    )
    assert (
        DEFAULT_SELECTORS.absolute("/forum/viewtopic.php?t=1")
        == "https://rutracker.org/forum/viewtopic.php?t=1"
    )


def test_the_search_url_carries_the_query_and_the_category() -> None:
    assert DEFAULT_SELECTORS.search_url() == "https://rutracker.org/forum/tracker.php?"
    assert DEFAULT_SELECTORS.search_url("Bach").endswith("nm=Bach")
    # The exact form the site's own "search in this section" links use.
    assert (
        DEFAULT_SELECTORS.search_url("Bach", 794)
        == "https://rutracker.org/forum/tracker.php?f=794&nm=Bach"
    )
    assert DEFAULT_SELECTORS.search_url(category=5) == "https://rutracker.org/forum/tracker.php?f=5"


# ── Turning a configured task into the searches that run ──────────────────


def _source(**settings: object) -> RutrackerSource:
    return RutrackerSource(SourceSettings(**settings))  # type: ignore[arg-type]


def test_a_task_that_names_a_category_is_one_search() -> None:
    task = SearchTask(text="Bach", author="someone", category=794)

    assert _source().expand(task) == [task]


def test_a_task_without_a_category_becomes_one_search_per_configured_section() -> None:
    source = _source(categories={"Opera": 794, "Choral": 2307, "Solo": 793})
    task = SearchTask(author="someone")

    expanded = source.expand(task)

    assert [entry.category for entry in expanded] == [794, 2307, 793]
    assert all(entry.author == "someone" for entry in expanded)
    assert all(entry.text == task.text for entry in expanded)
    assert task.category is None, "the configured task itself is left alone"


def test_the_wildcard_category_means_the_same_as_naming_none() -> None:
    source = _source(categories={"Opera": 794})

    assert source.expand(SearchTask(category="*")) == [SearchTask(category=794)]


def test_expanding_without_configured_sections_is_refused() -> None:
    # Config validation refuses this already; a hand-built source must not search nothing.
    with pytest.raises(ValueError, match="no sections configured"):
        _source().expand(SearchTask(text="Bach"))


def test_a_partial_selector_override_is_enough(tmp_path: Path) -> None:
    path = tmp_path / "selectors.yml"
    path.write_text("result_link: 'a.new-thing'\nunknown_key: 1\n", encoding="utf-8")

    selectors = Selectors.load(path)

    assert selectors.result_link == "a.new-thing"
    assert selectors.base_url == DEFAULT_SELECTORS.base_url
    assert not hasattr(selectors, "unknown_key")


def test_the_selectors_file_is_honoured(tmp_path: Path) -> None:
    path = tmp_path / "selectors.yml"
    path.write_text("download_link: 'a.new-download'\n", encoding="utf-8")

    assert (
        load_selectors(SourceSettings(selectors_file=str(path))).download_link == "a.new-download"
    )
    assert load_selectors(SourceSettings()) is DEFAULT_SELECTORS


def test_a_selectors_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "selectors.yml"
    path.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(TypeError, match="expected a YAML mapping"):
        Selectors.load(path)


# ── Raising SiteUnderMaintenanceError ─────────────────────────────────────


def test_raise_if_unusable_selectors_broken_error() -> None:
    """A page with generic issues raises SelectorsBrokenError, not maintenance."""
    with pytest.raises(SelectorsBrokenError, match="missing"):
        _raise_if_unusable(PLAIN_HTML, DEFAULT_SELECTORS)


def test_raise_if_unusable_maintenance_error() -> None:
    """A page with maintenance markers raises SiteUnderMaintenanceError."""
    with pytest.raises(SiteUnderMaintenanceError, match="under maintenance"):
        _raise_if_unusable(MAINTENANCE_HTML, DEFAULT_SELECTORS)


def test_raise_if_unusable_clean_page_raises_nothing(search_html: str) -> None:
    """A healthy results page does not raise at all."""
    assert _raise_if_unusable(search_html, DEFAULT_SELECTORS) is None


# ── open_topic maintenance detection ──────────────────────────────────────


class _FakeTopicPage:
    """A fake Playwright page for testing open_topic's maintenance check."""

    def __init__(self, html: str, url: str = TOPIC_URL) -> None:
        self._html = html
        self._url = url

    async def content(self) -> str:
        return self._html

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url: str, **_kwargs: object) -> None:
        self._url = url


def test_open_topic_raises_maintenance_on_maintenance_page() -> None:
    page = _FakeTopicPage(MAINTENANCE_TOPIC_HTML)

    with pytest.raises(SiteUnderMaintenanceError, match="under maintenance"):
        asyncio.run(open_topic(page, TOPIC_URL))


def test_open_topic_raises_selectors_broken_on_unknown_page() -> None:
    page = _FakeTopicPage(PLAIN_HTML)

    with pytest.raises(SelectorsBrokenError, match="not a topic page"):
        asyncio.run(open_topic(page, TOPIC_URL))
