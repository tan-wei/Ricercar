"""Domain types shared between sources, the parser and storage.

Only the shapes that cross a module boundary live here: a :class:`SearchHit` is
what a source hands to the application, a :class:`TopicInfo` describes the page a
torrent was taken from, and :class:`TorrentMetadata` is what the parser reads out
of the downloaded file. They sit outside the modules that produce them so that a
second tracker can be added without importing another tracker's code, and so the
parser stays independent of any site.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One row of a search-results page."""

    topic_id: str
    """The tracker's own identifier for the topic."""
    title: str
    url: str
    """Absolute URL of the topic page."""
    author: str = ""
    """Uploader of the torrent — empty when a source has no such notion."""
    size: str = ""
    """Size as the results page displays it, e.g. ``285 MB``."""


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of search results, together with the pager's own numbering.

    The pager says how many pages a search has ("Страница 1 из 10"); that number is the
    truth about the search, while ``max_pages`` in the configuration is only the
    ceiling on how far the run may follow it. Both travel with the page so the caller
    can report and stop by what the site says rather than by what it was allowed to do.
    """

    hits: list[SearchHit]
    """The rows on this page, in the order the site listed them."""
    number: int
    """Where this page sits in the walk, 1-based."""
    total: int | None = None
    """How many pages the tracker says the search has; ``None`` when it does not say."""


@dataclass(frozen=True, slots=True)
class TopicInfo:
    """What a topic page tells us before downloading anything."""

    url: str
    title: str
    downloadable: bool
    """Whether the page offers the torrent at all."""


@dataclass(frozen=True, slots=True)
class TorrentMetadata:
    """What the project needs out of a ``.torrent``."""

    url: str
    """Value of the ``comment`` field — the trackers' topic URL, for rutracker."""
    name: str
    """Value of ``info.name``."""
    size: int
    """Total payload size in bytes."""
    file_count: int
    """Number of files the torrent contains (1 for a single-file torrent)."""
    md5: str
    """MD5 of the whole file — the dedup key."""
    issues: tuple[str, ...] = ()
    """Non-fatal consistency problems found while parsing; empty when sound."""


__all__ = ["SearchHit", "SearchPage", "TopicInfo", "TorrentMetadata"]
