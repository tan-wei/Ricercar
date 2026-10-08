"""The database: the schema guard rails, deduplication, and the daily counter."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from ricercar.models import TorrentMetadata
from ricercar.repository import TorrentRepository
from ricercar.repository.db import (
    SCHEMA_VERSION,
    LegacyDatabaseError,
    SchemaVersionError,
    check_database,
    connect,
)
from tests.conftest import LegacyDatabase

RUTRACKER_URL = "https://rutracker.org/forum/viewtopic.php?t=101"
OTHER_URL = "https://other.example/viewtopic.php?t=202"


def _meta(url: str, md5: str, *, name: str = "Bach", size: int = 1024) -> TorrentMetadata:
    return TorrentMetadata(url=url, name=name, size=size, file_count=1, md5=md5)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "torrents.db"


@pytest.fixture
def repo(db_path: Path) -> Iterator[TorrentRepository]:
    with TorrentRepository(db_path) as repository:
        yield repository


# ── A fresh database ──────────────────────────────────────────────────────


def test_a_fresh_database_creates_itself_on_the_current_schema(repo: TorrentRepository) -> None:
    assert repo.schema_version == SCHEMA_VERSION
    assert repo.count() == 0
    assert repo.count_today() == 0
    assert repo.recent() == []


def test_a_fresh_database_carries_the_index_the_counters_need(repo: TorrentRepository) -> None:
    indexes = {
        str(row[0])
        for row in repo._db.execute("SELECT name FROM sqlite_master WHERE type = 'index';")
    }

    assert "idx_url_table_add_date" in indexes


def test_the_constructor_does_not_touch_the_disk(db_path: Path) -> None:
    TorrentRepository(db_path)

    assert not db_path.exists()


def test_stats_summarise_an_empty_database(repo: TorrentRepository) -> None:
    stats = repo.stats()

    assert stats["torrents"] == 0
    assert stats["blobs"] == 0
    assert stats["blob_bytes"] == 0
    assert stats["schema_version"] == SCHEMA_VERSION


# ── Deduplication ─────────────────────────────────────────────────────────


def test_a_topic_is_stored_once(repo: TorrentRepository) -> None:
    assert repo.add(_meta(RUTRACKER_URL, "a" * 32), b"first") is True
    assert repo.add(_meta(RUTRACKER_URL, "a" * 32), b"second") is False

    assert repo.count() == 1
    assert repo.blob(RUTRACKER_URL) == b"first"


def test_content_already_known_is_skipped_under_a_second_topic(repo: TorrentRepository) -> None:
    assert repo.add(_meta(RUTRACKER_URL, "a" * 32), b"payload") is True
    assert repo.add(_meta(OTHER_URL, "a" * 32), b"payload") is False

    assert repo.count() == 1
    assert repo.has_md5("a" * 32) is True


def test_an_unknown_topic_has_no_blob(repo: TorrentRepository) -> None:
    assert repo.blob(RUTRACKER_URL) is None
    assert repo.has_url(RUTRACKER_URL) is False
    assert repo.has_md5("a" * 32) is False


def test_what_is_stored_can_be_looked_up_with_its_date(repo: TorrentRepository) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32, name="Bach - Brandenburg"), b"payload")

    stored = repo.stored(RUTRACKER_URL)
    assert stored is not None
    assert stored.name == "Bach - Brandenburg"
    assert stored.add_date[:4].isdigit()
    assert repo.stored(OTHER_URL) is None


def test_content_can_be_looked_up_by_its_md5(repo: TorrentRepository) -> None:
    # The row that holds a md5 is not always the row the md5 arrived by, which is
    # exactly what a log line about a skipped download needs to name.
    repo.add(_meta(RUTRACKER_URL, "a" * 32, name="Bach - Brandenburg"), b"payload")

    stored = repo.stored_content("a" * 32)
    assert stored is not None
    assert stored.url == RUTRACKER_URL
    assert stored.add_date[:4].isdigit()
    assert repo.stored_content("f" * 32) is None


def test_the_skip_line_says_when_the_topic_was_stored_first(
    repo: TorrentRepository, log_messages: list[str]
) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32), b"first")
    stored = repo.stored(RUTRACKER_URL)
    assert stored is not None

    assert repo.add(_meta(RUTRACKER_URL, "a" * 32), b"second") is False

    assert f"Already stored: {RUTRACKER_URL} — added {stored.add_date}" in log_messages


def test_the_skip_line_names_the_topic_that_holds_the_same_content(
    repo: TorrentRepository, log_messages: list[str]
) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32), b"payload")
    stored = repo.stored_content("a" * 32)
    assert stored is not None

    assert repo.add(_meta(OTHER_URL, "a" * 32), b"payload") is False

    assert (
        f"Already stored as {stored.url} — added {stored.add_date} (the md5 is the same)"
        in log_messages
    )


def test_add_many_reports_only_the_new_ones(repo: TorrentRepository) -> None:
    entries = [
        (_meta(RUTRACKER_URL, "a" * 32), b"first"),
        (_meta(OTHER_URL, "b" * 32), b"second"),
        (_meta(RUTRACKER_URL, "a" * 32), b"first"),
    ]

    assert repo.add_many(entries) == 2
    assert repo.count() == 2


def test_recent_reports_what_was_stored(repo: TorrentRepository) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32, name="Bach - Brandenburg", size=2048), b"payload")

    (stored,) = repo.recent()
    assert stored.url == RUTRACKER_URL
    assert stored.name == "Bach - Brandenburg"
    assert stored.download_size == 2048
    assert stored.md5 == "a" * 32
    assert stored.add_date[:4].isdigit()


def test_stats_count_the_rows_and_the_blobs(repo: TorrentRepository) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32, size=100), b"payload")
    repo.add(_meta(OTHER_URL, "b" * 32, size=10), b"second")

    stats = repo.stats()
    assert stats["torrents"] == 2
    assert stats["distinct_md5"] == 2
    assert stats["total_bytes"] == 110
    assert stats["blobs"] == 2
    assert stats["blob_bytes"] == len(b"payloadsecond")


# ── The daily counter is per tracker ──────────────────────────────────────


def test_the_daily_counter_is_per_host(repo: TorrentRepository) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32), b"first")
    repo.add(_meta(OTHER_URL, "b" * 32), b"second")

    assert repo.count_today() == 2
    assert repo.count_today_from("rutracker.org") == 1
    assert repo.count_today_from("other.example") == 1
    assert repo.count_today_from("example.org") == 0


def test_the_daily_counter_ignores_what_was_stored_before_today(
    repo: TorrentRepository, db_path: Path
) -> None:
    repo.add(_meta(RUTRACKER_URL, "a" * 32), b"payload")

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("UPDATE url_table SET add_date = DATE('now', 'localtime', '-1 day');")
        conn.commit()

    assert repo.count() == 1
    assert repo.count_today() == 0
    assert repo.count_today_from("rutracker.org") == 0


def test_a_row_dated_tomorrow_is_not_counted_as_today(
    repo: TorrentRepository, db_path: Path
) -> None:
    # The counter compares text against a range, so it has to stop at the end of the day
    # as well as start at the beginning of it.
    repo.add(_meta(RUTRACKER_URL, "a" * 32), b"payload")

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("UPDATE url_table SET add_date = DATE('now', 'localtime', '+1 day');")
        conn.commit()

    assert repo.count() == 1
    assert repo.count_today() == 0


# ── The schema guard rails ────────────────────────────────────────────────


def test_a_missing_database_is_not_a_problem(tmp_path: Path) -> None:
    check_database(tmp_path / "absent.db")


def test_the_current_schema_passes_the_check(db_path: Path, repo: TorrentRepository) -> None:
    assert repo.count() == 0  # created, empty, and on the current schema

    check_database(db_path)


def test_a_legacy_database_is_refused_with_instructions(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    path = legacy_database.create(tmp_path / "legacy.db")

    with pytest.raises(LegacyDatabaseError, match="legacy schema"):
        check_database(path)


def test_a_database_from_another_version_is_refused(db_path: Path) -> None:
    TorrentRepository(db_path).open().close()  # a database on the current schema

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1};")
        conn.commit()

    with pytest.raises(SchemaVersionError, match="newer version of Ricercar"):
        check_database(db_path)


def test_the_repository_refuses_to_open_a_legacy_database(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    path = legacy_database.create(tmp_path / "legacy.db")

    with pytest.raises(LegacyDatabaseError), TorrentRepository(path):
        pass
