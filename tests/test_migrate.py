"""Importing a legacy database: staged, verified, and swapped in only at the end."""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from ricercar.repository.db import SCHEMA_VERSION, connect, schema_version
from ricercar.repository.migrate import STAGING_SUFFIX, Fingerprint, migrate
from tests.conftest import LegacyDatabase


def _rows(path: Path, table: str) -> list[tuple[object, ...]]:
    """The rows of *table*, in a stable order."""
    with contextlib.closing(connect(path)) as conn:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY url;")]


def _blob(path: Path, url: str) -> bytes:
    with contextlib.closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT torrent_file FROM torrent_table WHERE url = ?;", (url,)
        ).fetchone()
        return bytes(row[0])


def test_a_legacy_database_is_imported(legacy_database: LegacyDatabase, tmp_path: Path) -> None:
    source = legacy_database.create(tmp_path / "legacy.db")
    target = tmp_path / "torrents.db"

    result = migrate(source, target)

    assert result.torrents == len(legacy_database.ROWS)
    assert result.blobs == len(legacy_database.ROWS)
    assert result.distinct_md5 == len(legacy_database.ROWS)
    assert _rows(target, "url_table") == sorted(legacy_database.ROWS)
    assert _blob(target, legacy_database.ROWS[0][0]) == legacy_database.blob_for(
        legacy_database.ROWS[0][0]
    )
    with contextlib.closing(connect(target)) as conn:
        assert schema_version(conn) == SCHEMA_VERSION
    assert not Path(f"{target}{STAGING_SUFFIX}").exists()


def test_the_source_is_left_alone(legacy_database: LegacyDatabase, tmp_path: Path) -> None:
    source = legacy_database.create(tmp_path / "legacy.db")
    before = source.read_bytes()

    migrate(source, tmp_path / "torrents.db")

    assert source.read_bytes() == before
    with contextlib.closing(connect(source)) as conn:
        assert schema_version(conn) == 0


def test_an_in_place_import_rebuilds_the_same_file(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    path = legacy_database.create(tmp_path / "torrents.db")
    expected = sorted(legacy_database.ROWS)

    migrate(path, path, replace=True)

    with contextlib.closing(connect(path)) as conn:
        assert schema_version(conn) == SCHEMA_VERSION
    assert _rows(path, "url_table") == expected
    for leftover in (f"{path}{STAGING_SUFFIX}", f"{path}-wal", f"{path}-shm"):
        assert not Path(leftover).exists()


def test_a_missing_source_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        migrate(tmp_path / "absent.db", tmp_path / "torrents.db")


def test_a_source_that_is_not_a_torrent_database_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "notes.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("CREATE TABLE notes (body TEXT);")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="does not look like a legacy torrent database"):
        migrate(source, tmp_path / "torrents.db")


def test_an_existing_target_needs_saying_so(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    source = legacy_database.create(tmp_path / "legacy.db")
    target = tmp_path / "torrents.db"
    target.write_bytes(b"something already here")

    with pytest.raises(FileExistsError, match="already exists"):
        migrate(source, target)

    assert target.read_bytes() == b"something already here"


def test_a_source_that_breaks_the_new_constraints_aborts(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    # The legacy schema had no unique index on md5, so such a database can legitimately
    # hold the same content twice — and importing it must fail instead of silently
    # dropping a row.
    duplicated = (
        ("https://rutracker.org/forum/viewtopic.php?t=1", "A", 10, "a" * 32, "2024-01-01 00:00:00"),
        ("https://rutracker.org/forum/viewtopic.php?t=2", "B", 20, "a" * 32, "2024-01-02 00:00:00"),
    )
    source = legacy_database.create(tmp_path / "legacy.db", duplicated)
    target = tmp_path / "torrents.db"
    target.write_bytes(b"the target that must survive")

    with pytest.raises(sqlite3.IntegrityError):
        migrate(source, target, replace=True)

    assert target.read_bytes() == b"the target that must survive"
    assert not Path(f"{target}{STAGING_SUFFIX}").exists()


def test_a_fingerprint_notices_what_a_copy_lost() -> None:
    source = Fingerprint(
        torrents=2,
        blobs=2,
        blob_bytes=4096,
        distinct_md5=2,
        first_added="2024-01-01 00:00:00",
        last_added="2024-01-02 00:00:00",
    )

    assert source.differences(source) == []
    assert source.differences(replace(source, blobs=1)) == ["blobs: 2 != 1"]
    assert "2 torrents" in source.describe()
    assert source.describe().endswith("2024-01-02 00:00:00")
