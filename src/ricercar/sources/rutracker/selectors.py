"""Everything rutracker-specific in one place.

The tracker's URLs, CSS selectors and literal strings live here, so a site
redesign is a one-file fix instead of a hunt through the scraping logic. Every
default was verified against the live site.

:meth:`Selectors.load` reads a YAML mapping of overrides, so a fix can also ship
without touching the code (see ``sources.rutracker.selectors_file``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import yaml

from ricercar.config import SourceSettings


@dataclass(frozen=True, slots=True)
class Selectors:
    """Site-specific URLs, CSS selectors and literal text."""

    base_url: str = "https://rutracker.org/forum/"
    """Forum root; relative hrefs found in pages are resolved against it."""

    logged_in: str = "#logged-in-username"
    """Element holding the name of the signed-in user."""
    login_form: str = 'input[name="login_username"]'
    """Login field — its presence means the session is gone (used by the diagnosis)."""
    results_count: str = r"Результатов поиска:\s*(\d+)"
    """Regex matching the results header, capturing how many hits the search has."""
    result_link: str = "a[data-topic_id]"
    """One anchor per result row, carrying the topic id."""
    result_uploader: str = "td.u-name-col"
    """Result-row cell holding the uploader (rutracker calls it «автор»)."""
    pager_link: str = "a.pg"
    """Pagination anchors; their ``start`` parameter identifies the page."""
    author_filter_field: str = 'input[name="pn"]'
    """Uploader filter input on the search page."""
    author_filter_submit: str = "#tr-submit-btn"
    """Button that applies the search page's filters."""
    download_link: str = 'a.dl-link[href*="dl.php"]'
    """Download anchor on a topic page — absent when the torrent is unavailable."""

    # ── URL building ────────────────────────────────────────────────────

    def search_url(self, text: str = "", category: int | None = None) -> str:
        """Build the URL of a search-results page."""
        params: dict[str, str] = {}
        if category is not None:
            params["f"] = str(category)
        if text:
            params["nm"] = text
        return f"{self.base_url}tracker.php?{urlencode(params)}"

    def absolute(self, href: str) -> str:
        """Resolve an href found in a page to a full URL."""
        return urljoin(self.base_url, href)

    # ── Loading ─────────────────────────────────────────────────────────

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Selectors:
        """Build selectors from a mapping, ignoring unknown keys."""
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @classmethod
    def load(cls, path: str | Path) -> Selectors:
        """Build selectors from a YAML file; a partial override is enough."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, Mapping):
            msg = f"{path}: expected a YAML mapping of selector overrides"
            raise TypeError(msg)
        return cls.from_mapping(data)


DEFAULT_SELECTORS = Selectors()
"""The selectors as verified against the live site."""


def load_selectors(settings: SourceSettings) -> Selectors:
    """Return the selectors to use, honouring ``selectors_file``."""
    if settings.selectors_file:
        return Selectors.load(settings.selectors_file)
    return DEFAULT_SELECTORS


__all__ = ["DEFAULT_SELECTORS", "Selectors", "load_selectors"]
