"""The rutracker adapter, checked against pages captured from the live site."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup

from ricercar.config import SearchTask, SourceSettings
from ricercar.models import SearchPage
from ricercar.sources.rutracker import RutrackerSource, parse_results, uploaders
from ricercar.sources.rutracker.diagnose import diagnose_results, diagnose_topic
from ricercar.sources.rutracker.search import iter_result_pages, next_page_url, page_count
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors, load_selectors
from ricercar.sources.rutracker.session import LoginOutcome, login_page_state
from ricercar.sources.rutracker.topic import parse_topic

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
