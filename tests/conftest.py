"""Shared fixtures for the test suite.

Two things are deliberately absent from these tests: a browser and a network. The
database, the parser, the retry logic and the rutracker page handling all work on
bytes, HTML or a temporary SQLite file — real pages and a real ``.torrent`` are kept
in ``tests/fixtures`` for that. The only test that needs a browser is the offline
run in ``test_pipeline.py``, marked ``integration`` and skipped when nothing is
listening on the configured CDP endpoint.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from loguru import logger

from ricercar.config import Settings, reload_settings, set_extra_config_files

FIXTURES = Path(__file__).resolve().parent / "fixtures"

LegacyRow = tuple[str, str, int, str, str]
"""One ``url_table`` row: url, name, size, md5, add_date."""


@pytest.fixture(autouse=True)
def quiet_logger() -> Iterator[None]:
    """Silence loguru: no console sink and no log files, for the whole suite."""
    logger.remove()
    logger.add(lambda _: None, level="CRITICAL")
    yield
    logger.remove()


@pytest.fixture
def log_messages() -> Iterator[list[str]]:
    """Everything that gets logged while the test runs, one message per line.

    For the lines that *are* the feature — a skip that says when the torrent was stored
    first is only correct if the date is in it, and nothing else about the run would
    notice it missing. Line endings are stripped, so a message can be compared whole.
    """
    messages: list[str] = []

    def collect(message: object) -> None:
        messages.append(str(message).rstrip("\n"))

    logger.remove()
    logger.add(collect, level="DEBUG", format="{message}")
    yield messages
    logger.remove()


# ── Fixture data ──────────────────────────────────────────────────────────


@pytest.fixture
def fixtures_root() -> Path:
    """The directory the offline mode is pointed at."""
    return FIXTURES


@pytest.fixture
def search_html() -> str:
    """A real search-results page, captured from the tracker."""
    return (FIXTURES / "html" / "search" / "results.html").read_text(encoding="utf-8")


@pytest.fixture
def topic_html() -> str:
    """A real topic page, captured from the tracker."""
    return (FIXTURES / "html" / "topic" / "topic.html").read_text(encoding="utf-8")


@pytest.fixture
def sample_torrent() -> bytes:
    """A real ``.torrent`` file, taken out of the legacy database."""
    return (FIXTURES / "torrents" / "sample.torrent").read_bytes()


# ── Configuration ─────────────────────────────────────────────────────────


@pytest.fixture
def extra_config(tmp_path: Path) -> Iterator[Callable[[str, str], Settings]]:
    """Register extra YAML config layers, highest priority, and undo it afterwards.

    Returns ``add(name, body)``; each call writes a file, layers it on top of the
    previous ones and returns the reloaded settings.
    """
    registered: list[Path] = []

    def add(name: str, body: str) -> Settings:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        registered.append(path)
        set_extra_config_files(registered)
        return reload_settings()

    yield add

    set_extra_config_files([])
    reload_settings()


# ── Legacy database ───────────────────────────────────────────────────────


class LegacyDatabase:
    """Factory for a database shaped like the one the legacy tool leaves behind.

    The legacy file has no ``PRAGMA user_version``, no primary key and no unique
    constraint — which is exactly why this build refuses it (see
    :mod:`ricercar.repository.db`).
    """

    SCHEMA = """
    CREATE TABLE url_table
    (
        url           TEXT,
        name          TEXT,
        download_size INTEGER,
        md5           TEXT,
        add_date      TEXT
    );
    CREATE TABLE torrent_table
    (
        url          TEXT,
        torrent_file BLOB
    );
    """

    ROWS: tuple[LegacyRow, ...] = (
        (
            "https://rutracker.org/forum/viewtopic.php?t=101",
            "Bach - Brandenburg Concertos",
            1024,
            "0" * 32,
            "2024-01-01 10:00:00",
        ),
        (
            "https://rutracker.org/forum/viewtopic.php?t=102",
            "Bach - Cantatas",
            2048,
            "1" * 32,
            "2024-01-02 11:30:00",
        ),
    )

    @staticmethod
    def blob_for(url: str) -> bytes:
        """Deterministic stand-in for the ``.torrent`` stored under *url*."""
        return f"torrent of {url}".encode()

    def create(self, path: Path, rows: Sequence[LegacyRow] | None = None) -> Path:
        """Write a legacy database at *path* and return it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(self.SCHEMA)
            for row in rows if rows is not None else self.ROWS:
                conn.execute("INSERT INTO url_table VALUES (?, ?, ?, ?, ?);", row)
                conn.execute(
                    "INSERT INTO torrent_table VALUES (?, ?);",
                    (row[0], sqlite3.Binary(self.blob_for(row[0]))),
                )
            conn.commit()
        finally:
            # Explicitly: the fixture has to let go of the file, or a test that rebuilds
            # it in place cannot rename over it on Windows.
            conn.close()
        return path


@pytest.fixture
def legacy_database() -> LegacyDatabase:
    """Build legacy-schema databases for the schema and migration tests."""
    return LegacyDatabase()
