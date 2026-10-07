"""The rutracker adapter, checked against pages captured from the live site."""

from __future__ import annotations

from pathlib import Path

import pytest

from ricercar.config import SearchTask, SourceSettings
from ricercar.sources.rutracker import RutrackerSource, parse_results, uploaders
from ricercar.sources.rutracker.diagnose import diagnose_results, diagnose_topic
from ricercar.sources.rutracker.search import next_page_url
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors, load_selectors
from ricercar.sources.rutracker.topic import parse_topic

RESULTS_URL = "https://rutracker.org/forum/tracker.php?search_id=abc&nm=Bach"
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t=1580744"

CHALLENGE_HTML = "<html><body><h1>Just a moment...</h1></body></html>"
LOGIN_HTML = '<html><body><form><input name="login_username"></form></body></html>'
RENAMED_HTML = "<html><body><p>Результатов поиска: 500</p><a href='/x'>row</a></body></html>"
PLAIN_HTML = "<html><body><p>nothing much here</p></body></html>"


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
