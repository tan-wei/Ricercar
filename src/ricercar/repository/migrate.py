"""Import a database written by the legacy Selenium tool.

The current schema is the legacy one plus the constraints it was missing (see
:mod:`ricercar.repository.db`), so an import is a server-side ``INSERT ... SELECT``
through ``ATTACH`` — no blob ever travels through Python, which matters at
600 MiB::

    uv run python -m ricercar.repository.migrate legacy/torrents.db
    just migrate legacy/torrents.db

The source is attached read-only and left untouched. Rows the new constraints
reject (a duplicate URL, a NULL, a zero MD5) abort the import instead of being
silently dropped. Afterwards the copy is fingerprinted — row counts, blob bytes,
distinct MD5s, date range — and compared against the source; any difference is
an error.

Rows are copied in batches of :data:`BATCH_ROWS` so the import can report how far
along it is (a single ``INSERT ... SELECT`` of 25,000 blobs is ~20 seconds of
silence) and so no transaction grows to the size of the database.

The copy is built **next to** the target and swapped in only once it verifies, so
an existing target survives a failed import, and an in-place import (source and
target being the same file, ``--replace``) is safe too.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from rich.progress import Progress, TaskID

from ricercar.config import get_settings
from ricercar.log import configure_logging, get_logger
from ricercar.progress import new_progress, phase
from ricercar.repository.db import connect, ensure_schema

SOURCE_ALIAS = "legacy_source"
"""Alias the read-only source database is attached under."""

STAGING_SUFFIX = ".migrating"
"""Suffix of the database being built, before it replaces the target."""

BATCH_ROWS = 2_000
"""Rows copied per statement.

Batching exists for the progress bar as much as for memory: a single
``INSERT ... SELECT`` of 25,000 blobs takes about 20 seconds with nothing to
report, while batches let the bar advance and keep each transaction small.
"""

_COPY_URLS = f"""
INSERT INTO url_table (url, name, download_size, md5, add_date)
SELECT url, name, download_size, md5, add_date FROM {SOURCE_ALIAS}.url_table
WHERE rowid BETWEEN ? AND ?;
"""
_COPY_BLOBS = f"""
INSERT INTO torrent_table (url, torrent_file)
SELECT url, torrent_file FROM {SOURCE_ALIAS}.torrent_table
WHERE rowid BETWEEN ? AND ?;
"""
_REQUIRED_SOURCE_TABLES = ("url_table", "torrent_table")


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Just enough about a database to prove a copy of it is complete."""

    torrents: int
    blobs: int
    blob_bytes: int
    distinct_md5: int
    first_added: str | None
    last_added: str | None

    def describe(self) -> str:
        return (
            f"{self.torrents} torrents, {self.blobs} blobs, "
            f"{self.blob_bytes / 1024 / 1024:.1f} MiB, "
            f"{self.distinct_md5} distinct md5, "
            f"added {self.first_added} .. {self.last_added}"
        )

    def differences(self, other: Fingerprint) -> list[str]:
        """Field-by-field differences against *other*, empty when identical."""
        mismatches = [
            f"{name}: {getattr(self, name)!r} != {getattr(other, name)!r}"
            for name in self.__slots__
            if getattr(self, name) != getattr(other, name)
        ]
        return mismatches


def fingerprint(conn: sqlite3.Connection, schema: str = "main") -> Fingerprint:
    """Summarise the torrent tables of *schema* (``main`` or an attached alias)."""
    urls = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT md5), MIN(add_date), MAX(add_date)"
        f" FROM {schema}.url_table;"
    ).fetchone()
    blobs = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(LENGTH(torrent_file)), 0) FROM {schema}.torrent_table;"
    ).fetchone()
    return Fingerprint(
        torrents=int(urls[0]),
        distinct_md5=int(urls[1]),
        first_added=urls[2],
        last_added=urls[3],
        blobs=int(blobs[0]),
        blob_bytes=int(blobs[1]),
    )


def source_uri(source: Path) -> str:
    """URI that opens *source* read-only, whatever the caller does with it."""
    return f"file:{source.as_posix()}?mode=ro"


def check_source(source: Path) -> None:
    """Fail early unless *source* looks like a legacy torrent database.

    Runs before the target is created, so a typo does not leave an empty database
    behind (which the next run would then refuse to overwrite).
    """
    with contextlib.closing(sqlite3.connect(source_uri(source), uri=True)) as conn:
        present = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table';")
        }
    missing = [table for table in _REQUIRED_SOURCE_TABLES if table not in present]
    if missing:
        msg = (
            f"{source} does not look like a legacy torrent database — "
            f"missing table(s): {', '.join(missing)}"
        )
        raise ValueError(msg)


def attach_source(conn: sqlite3.Connection, source: Path) -> None:
    """Attach *source* to *conn* read-only, under :data:`SOURCE_ALIAS`."""
    try:
        conn.execute(f"ATTACH DATABASE ? AS {SOURCE_ALIAS};", (source_uri(source),))
    except sqlite3.OperationalError as exc:
        msg = f"{source} cannot be opened read-only: {exc}"
        raise ValueError(msg) from exc


def _rowid_bounds(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    """The rowid range the source table occupies (0, 0 when it is empty)."""
    row = conn.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {SOURCE_ALIAS}.{table};").fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


def _copy_table(
    conn: sqlite3.Connection,
    statement: str,
    table: str,
    *,
    progress: Progress | None = None,
    task: TaskID | None = None,
    batch_rows: int = BATCH_ROWS,
) -> int:
    """Copy one table from the attached source, in rowid windows.

    Advances *task* by the number of rows each batch actually inserted, so gaps in
    the source's rowids (deleted rows) cannot make the bar lie. Returns the total
    copied.
    """
    first, last = _rowid_bounds(conn, table)
    copied = 0
    for start in range(first, last + 1, batch_rows):
        with conn:
            cursor = conn.execute(statement, (start, start + batch_rows - 1))
        inserted = max(cursor.rowcount, 0)
        copied += inserted
        if progress is not None and task is not None:
            progress.advance(task, inserted)
    return copied


def _sidecars(path: Path) -> list[Path]:
    """The files SQLite keeps beside a database (WAL, shared memory, journal)."""
    return [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]


def _discard(path: Path) -> None:
    """Delete a database file and whatever SQLite left beside it."""
    path.unlink(missing_ok=True)
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)


def migrate(
    source: Path,
    target: Path,
    *,
    replace: bool = False,
    progress: Progress | None = None,
) -> Fingerprint:
    """Copy every row of *source* into a database at *target*.

    Returns the fingerprint of the copied data; the source fingerprint is
    compared against it before returning, so a truncated copy cannot pass
    unnoticed.

    The copy is staged beside *target* and moved into place at the very end, so
    *target* keeps its old contents until a verified copy exists. That also makes
    an in-place import safe — ``migrate(db, db, replace=True)`` reads and rebuilds
    the same file — which is how a database discovered to be on the legacy schema
    is upgraded (see :func:`ricercar.repository.db.check_schema`).

    Pass a :class:`rich.progress.Progress` to follow along; without one the import
    is silent apart from the log.
    """
    log = get_logger()
    started = time.monotonic()

    if not source.is_file():
        msg = f"{source} does not exist"
        raise FileNotFoundError(msg)

    if target.exists() and not replace:
        msg = f"{target} already exists — pass replace=True to overwrite it"
        raise FileExistsError(msg)

    check_source(source)

    staging = target.with_name(f"{target.name}{STAGING_SUFFIX}")
    _discard(staging)  # leftovers from an interrupted run

    conn = connect(staging)
    failed = True
    try:
        ensure_schema(conn, staging)
        attach_source(conn, source)
        try:
            with phase(progress, "Reading the source"):
                expected = fingerprint(conn, SOURCE_ALIAS)
            log.info("Source: {}", expected.describe())

            with phase(progress, "Importing torrent metadata", expected.torrents) as task:
                _copy_table(conn, _COPY_URLS, "url_table", progress=progress, task=task)

            with phase(progress, "Importing .torrent files", expected.blobs) as task:
                _copy_table(conn, _COPY_BLOBS, "torrent_table", progress=progress, task=task)

            with phase(progress, "Verifying the copy"):
                actual = fingerprint(conn)

            mismatches = expected.differences(actual)
            if mismatches:
                msg = f"migration of {source} is incomplete: " + "; ".join(mismatches)
                raise RuntimeError(msg)
        finally:
            conn.execute(f"DETACH DATABASE {SOURCE_ALIAS};")
        failed = False
    finally:
        conn.close()
        if failed:
            _discard(staging)

    # A -wal left behind by the previous file must not outlive it, or SQLite would
    # try to apply it to the new one.
    for sidecar in _sidecars(target):
        sidecar.unlink(missing_ok=True)
    os.replace(staging, target)

    log.info(
        "Migrated to {} in {:.1f}s: {}",
        target,
        time.monotonic() - started,
        actual.describe(),
    )
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ricercar-migrate",
        description="Import a legacy torrent database into the current schema.",
    )
    parser.add_argument("source", type=Path, help="legacy SQLite database to read")
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="database to create (default: database.path from the configuration)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "rebuild the target even when it already exists — required for an "
            "in-place import, where source and target are the same file"
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log)
    target = args.target if args.target is not None else settings.db_path
    log = get_logger().bind(source=str(args.source), target=str(target))

    try:
        with new_progress() as progress:
            result = migrate(
                args.source,
                target,
                replace=args.replace,
                progress=progress,
            )
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        log.error("{}", exc)
        return 1

    from rich import print as rprint

    rprint(f"[green]{target}[/green]: {result.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
