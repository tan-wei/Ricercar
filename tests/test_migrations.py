"""Upgrading a database this project wrote itself, in place and one version at a time."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from ricercar.repository import TorrentRepository
from ricercar.repository import db as repository_db
from ricercar.repository.db import (
    SCHEMA_VERSION,
    SchemaVersionError,
    check_database,
    connect,
    ensure_schema,
    schema_version,
)
from ricercar.repository.migrations import MIGRATIONS, Migration, pending, upgrade

AS_VERSION_2 = """
CREATE TABLE url_table
(
    url           TEXT PRIMARY KEY NOT NULL,
    name          TEXT NOT NULL,
    download_size INTEGER NOT NULL,
    md5           TEXT NOT NULL UNIQUE,
    add_date      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE torrent_table
(
    url          TEXT PRIMARY KEY NOT NULL REFERENCES url_table(url) ON DELETE CASCADE,
    torrent_file BLOB NOT NULL
);
"""
"""The schema as version 2 shipped it: no index on ``add_date``."""

V2_URL = "https://rutracker.org/forum/viewtopic.php?t=101"
V2_MD5 = "a" * 32
ADD_INDEX = "idx_url_table_add_date"


def _version_2_database(path: Path) -> Path:
    """A database in the shape version 2 wrote, holding one torrent stored today."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(AS_VERSION_2)
        conn.execute(
            "INSERT INTO url_table VALUES (?, 'Bach', 1024, ?, datetime('now','localtime'));",
            (V2_URL, V2_MD5),
        )
        conn.execute("INSERT INTO torrent_table VALUES (?, ?);", (V2_URL, sqlite3.Binary(b"blob")))
        conn.execute("PRAGMA user_version=2;")
        conn.commit()
    finally:
        conn.close()
    return path


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index';")
    }


@pytest.fixture
def version_2(tmp_path: Path) -> Path:
    return _version_2_database(tmp_path / "torrents.db")


# ── The steps ─────────────────────────────────────────────────────────────


def test_the_steps_are_ordered_and_complete() -> None:
    versions = [migration.version for migration in MIGRATIONS]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert all(migration.description and migration.apply for migration in MIGRATIONS)
    assert versions[0] > 0, "version 0 is the legacy import's job, not a step"


def test_only_the_steps_between_the_two_versions_are_pending() -> None:
    first = MIGRATIONS[0]

    assert pending(first.version, first.version + 1) == (first,)
    assert pending(first.version + 1, first.version + 2) == ()
    assert pending(1, first.version + 1) == (first,)


# ── Opening an older database ─────────────────────────────────────────────


def test_a_version_2_database_is_upgraded_when_it_is_opened(version_2: Path) -> None:
    check_database(version_2)  # readable, so no complaint

    with TorrentRepository(version_2) as repo:
        assert repo.schema_version == SCHEMA_VERSION
        assert repo.count() == 1
        assert repo.blob(V2_URL) == b"blob"
        assert repo.count_today() == 1
        assert repo.count_today_from("rutracker.org") == 1

    with contextlib.closing(connect(version_2)) as conn:
        assert ADD_INDEX in _indexes(conn)


def test_an_up_to_date_database_is_not_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ricercar.repository.db.upgrade", _must_not_be_called)
    path = tmp_path / "torrents.db"
    TorrentRepository(path).open().close()  # created at the current version

    with TorrentRepository(path) as repo:
        # opening it writes nothing: the migration path is not even consulted
        assert repo.schema_version == SCHEMA_VERSION


def test_an_upgraded_database_still_refuses_a_newer_one(version_2: Path) -> None:
    conn = sqlite3.connect(version_2)
    try:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1};")
        conn.commit()

        with pytest.raises(SchemaVersionError, match="newer version of Ricercar"):
            ensure_schema(conn, version_2)
    finally:
        conn.close()


# ── A step that fails ─────────────────────────────────────────────────────


def test_a_step_that_fails_changes_nothing(
    version_2: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A step that manages to do something and *then* fails: the work and the absence of a
    # version stamp have to go together, or the next open would think it was already done.
    broken = Migration(version=2, description="a step that fails halfway", apply=_fail_halfway)
    monkeypatch.setattr("ricercar.repository.migrations.MIGRATIONS", (broken,))

    with contextlib.closing(connect(version_2)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            upgrade(conn, current=2, target=3)

        assert schema_version(conn) == 2
        assert ADD_INDEX not in _indexes(conn)
        assert conn.execute("SELECT COUNT(*) FROM url_table;").fetchone()[0] == 1


def test_a_database_that_cannot_be_created_is_not_half_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same guarantee for a fresh database: tables and version stamp, or nothing.
    monkeypatch.setattr(
        repository_db, "SCHEMA_STATEMENTS", (*repository_db.SCHEMA_STATEMENTS, "CREATE TABLE ;")
    )
    path = tmp_path / "torrents.db"

    with contextlib.closing(connect(path)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            ensure_schema(conn, path)

        assert schema_version(conn) == 0
        created = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'url_table';")
        assert created.fetchone()[0] == 0


def _fail_halfway(conn: sqlite3.Connection) -> None:
    """A step that changes something and only then finds out that it cannot finish."""
    conn.execute("CREATE INDEX idx_halfway ON url_table (md5);")
    conn.execute("ALTER TABLE nothing_here RENAME TO nope;")


def _must_not_be_called(*_args: object, **_kwargs: object) -> None:
    msg = "an up-to-date database must not be upgraded"
    raise AssertionError(msg)
