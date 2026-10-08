"""Importing a legacy database: a copy kept, staged, verified, swapped in at the end."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from ricercar.config import Settings
from ricercar.repository import migrate as migrate_module
from ricercar.repository.db import SCHEMA_VERSION, connect, schema_version
from ricercar.repository.migrate import (
    BACKUP_SUFFIX,
    STAGING_SUFFIX,
    Fingerprint,
    NothingToImportError,
    backup_path,
    keep_a_copy,
    main,
    migrate,
)
from tests.conftest import LegacyDatabase

ExtraConfig = Callable[[str, str], Settings]

MOMENT = datetime(2026, 10, 9, 0, 15, 30)
"""A fixed clock, so a copy's name is predictable in a test."""


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


def _backups(path: Path) -> list[Path]:
    """The copies kept beside *path*, oldest name first."""
    return sorted(path.parent.glob(f"{path.name}{BACKUP_SUFFIX}-*"))


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

    migrate(path, path)

    with contextlib.closing(connect(path)) as conn:
        assert schema_version(conn) == SCHEMA_VERSION
    assert _rows(path, "url_table") == expected
    for leftover in (f"{path}{STAGING_SUFFIX}", f"{path}-wal", f"{path}-shm"):
        assert not Path(leftover).exists()


def test_a_copy_of_what_the_import_replaced_is_kept(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    # The interesting case is the in-place one: the file that is read is also the file
    # that is rebuilt, so the only thing standing between a bug and the legacy data is
    # this copy.
    path = legacy_database.create(tmp_path / "torrents.db")
    before = path.read_bytes()

    migrate(path, path)

    copies = _backups(path)
    assert len(copies) == 1
    assert copies[0].read_bytes() == before
    with contextlib.closing(connect(copies[0])) as conn:
        assert schema_version(conn) == 0  # the legacy file, untouched
    assert copies[0].name.startswith("torrents.db" + BACKUP_SUFFIX)


def test_a_second_copy_does_not_overwrite_the_first(tmp_path: Path) -> None:
    # Timestamped names, so keeping a copy is never destructive itself.
    path = tmp_path / "torrents.db"

    first = backup_path(path, moment=MOMENT)
    second = backup_path(path, moment=datetime(2026, 10, 9, 1, 30, 0))

    assert first.name == "torrents.db.backup-20261009-001530"
    assert second.name == "torrents.db.backup-20261009-013000"
    assert first != second


def test_a_copy_carries_the_write_ahead_log_with_it(tmp_path: Path) -> None:
    # A -wal holds commits the main file does not have yet, so a copy of the .db alone
    # would be older than the database it is meant to preserve.
    path = tmp_path / "torrents.db"
    path.write_bytes(b"the main file")
    Path(f"{path}-wal").write_bytes(b"the commits that are not in it yet")

    backup = keep_a_copy(path, moment=MOMENT)

    assert backup is not None
    assert backup.read_bytes() == b"the main file"
    assert Path(f"{backup}-wal").read_bytes() == b"the commits that are not in it yet"
    # Copied, not moved: the target is still whole if the import then fails.
    assert path.read_bytes() == b"the main file"
    assert Path(f"{path}-wal").is_file()


def test_a_first_import_keeps_no_copy(tmp_path: Path) -> None:
    # Nothing to preserve: the target does not exist.
    assert keep_a_copy(tmp_path / "torrents.db", moment=MOMENT) is None


def test_a_target_is_replaced_without_being_asked(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    # No --replace and no prompt: what stands in the way of an overwrite is the copy,
    # not a question.
    source = legacy_database.create(tmp_path / "legacy.db")
    target = tmp_path / "torrents.db"
    target.write_bytes(b"something already here")

    migrate(source, target)

    copies = _backups(target)
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"something already here"
    assert _rows(target, "url_table") == sorted(legacy_database.ROWS)


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


def test_an_import_that_is_not_needed_says_so(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    # An already-imported database is not a failure: running the import twice has to be
    # harmless, and there is genuinely nothing to do.
    path = legacy_database.create(tmp_path / "torrents.db")
    migrate(path, path)

    with pytest.raises(NothingToImportError, match="nothing to import"):
        migrate(path, path)


def test_an_import_that_is_not_needed_leaves_the_file_alone(
    legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    path = legacy_database.create(tmp_path / "torrents.db")
    migrate(path, path)
    settled = path.read_bytes()
    copies = len(_backups(path))

    with pytest.raises(NothingToImportError):
        migrate(path, path)

    assert path.read_bytes() == settled
    assert len(_backups(path)) == copies  # no second copy of a file nothing happened to


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
        migrate(source, target)

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


# ── The command ───────────────────────────────────────────────────────────
#
# `just migrate` with no argument, which is the way this is meant to be run: the
# database is the one at `database.path`, and the import is in place.


@pytest.fixture
def configured(
    extra_config: ExtraConfig, monkeypatch: pytest.MonkeyPatch
) -> Callable[[Path], None]:
    """A `main()` that reads the given path as database.path, with no log files written."""

    def use(path: Path) -> None:
        settings = extra_config("database.yml", f"database:\n  path: {path.as_posix()}\n")
        monkeypatch.setattr(migrate_module, "get_settings", lambda: settings)
        monkeypatch.setattr(migrate_module, "configure_logging", lambda _config: None)

    return use


def test_the_configured_database_is_imported_in_place(
    configured, legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    path = legacy_database.create(tmp_path / "torrents.db")
    configured(path)

    assert main([]) == 0

    # Same file, same name, current schema — and the legacy file kept beside it.
    assert _rows(path, "url_table") == sorted(legacy_database.ROWS)
    with contextlib.closing(connect(path)) as conn:
        assert schema_version(conn) == SCHEMA_VERSION
    assert len(_backups(path)) == 1


def test_a_named_file_is_imported_into_the_configured_database(
    configured, legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    elsewhere = legacy_database.create(tmp_path / "elsewhere" / "old.db")
    target = tmp_path / "torrents.db"
    configured(target)

    assert main([str(elsewhere)]) == 0

    assert _rows(target, "url_table") == sorted(legacy_database.ROWS)
    assert _backups(target) == []  # there was nothing there to keep


def test_running_it_twice_is_not_an_error(
    configured, legacy_database: LegacyDatabase, tmp_path: Path
) -> None:
    path = legacy_database.create(tmp_path / "torrents.db")
    configured(path)
    assert main([]) == 0

    assert main([]) == 0  # nothing to import, and it says so instead of failing

    assert len(_backups(path)) == 1


def test_a_database_that_is_not_there_is_an_error(configured, tmp_path: Path) -> None:
    configured(tmp_path / "torrents.db")

    assert main([]) == 1


def test_a_file_that_is_not_a_database_is_an_error(configured, tmp_path: Path) -> None:
    elsewhere = tmp_path / "notes.txt"
    elsewhere.write_text("not a database", encoding="utf-8")
    target = tmp_path / "torrents.db"
    configured(target)

    assert main([str(elsewhere)]) == 1

    assert not target.exists()
    assert _backups(target) == []
