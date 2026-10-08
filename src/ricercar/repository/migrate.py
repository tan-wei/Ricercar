"""Import the database the legacy Selenium tool wrote.

The current schema is the legacy one plus the constraints it was missing (see
:mod:`ricercar.repository.db`), so an import is a server-side ``INSERT ... SELECT``
through ``ATTACH`` — no blob ever travels through Python, which matters at 600 MiB::

    just migrate        # upgrade the configured database, in place
    just migrate D:/old/torrents.db   # …or import one from somewhere else

Because a legacy database is found *at* the configured path rather than somewhere of
its own — :func:`ricercar.repository.db.check_schema` refuses to open it, and the
configuration already says where it is — the default is an **in-place** upgrade of
``database.path``: the same file, the same name, on the current schema afterwards.

Nothing is ever lost to a mistake: an existing database is copied aside first
(``<name>.backup-<timestamp>``, beside it) and the import is only built to a staging
file that is verified and then swapped in. A database that is already current has
nothing to import, which is reported rather than treated as a failure — so the command
is safe to run twice.

The source is attached read-only. Rows the new constraints reject (a duplicate URL, a
NULL, a zero MD5) abort the import instead of being silently dropped. Afterwards the
copy is fingerprinted — row counts, blob bytes, distinct MD5s, date range — and compared
against the source; any difference is an error.

Rows are copied in batches of :data:`BATCH_ROWS` so the import can report how far along
it is (a single ``INSERT ... SELECT`` of 25,000 blobs is ~20 seconds of silence) and so
no transaction grows to the size of the database.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.progress import Progress, TaskID

from ricercar.config import get_settings
from ricercar.log import configure_logging, get_logger
from ricercar.progress import new_progress, phase
from ricercar.repository import db
from ricercar.repository.db import connect, ensure_schema

SOURCE_ALIAS = "legacy_source"
"""Alias the read-only source database is attached under."""

STAGING_SUFFIX = ".migrating"
"""Suffix of the database being built, before it replaces the target."""

BACKUP_SUFFIX = ".backup"
"""Suffix of the copy kept of what an import replaces: ``torrents.db.backup-20261009-001530``.

Timestamped rather than a fixed name, so a second import cannot overwrite the copy the
first one kept, and dated the way :mod:`ricercar.diagnostics` dates its failure
directories.
"""

BACKUP_CHUNK = 8 * 1024 * 1024
"""Bytes copied per read when keeping a copy, so the progress bar moves on a 600 MiB file."""

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
"""The files SQLite keeps beside a database, as suffixes of its own name."""

BATCH_ROWS = 2_000
"""Rows copied per statement.

Batching exists for the progress bar as much as for memory: a single
``INSERT ... SELECT`` of 25,000 blobs takes about 20 seconds with nothing to
report, while batches let the bar advance and keep each transaction small.
"""

_REQUIRED_SOURCE_TABLES = ("url_table", "torrent_table")

_REQUIRED_URL_COLUMNS = ("url", "name", "download_size", "md5", "add_date")
"""Columns ``url_table`` may not leave NULL, in the schema this imports into."""

# ── Which rows the current schema can hold ────────────────────────────────
#
# A legacy database can hold rows the constraints of the current schema reject, in two
# ways, and neither is a reason to refuse the import:
#
# * **The same url twice** — a topic stored again later, identical in name, size and
#   content, with a later add_date. The legacy tool deduplicated through its
#   primary-key index ("SELECT url FROM url_table WHERE url = ?"), so once the index lost
#   an entry it could no longer see that row and added the topic again. A file whose
#   index no longer matches its rows is damaged; ``PRAGMA integrity_check`` reports it,
#   and every index-based query in it agrees with the index rather than the table.
# * **One md5 under two urls** — the same content posted in two sections, which
#   ``md5 UNIQUE`` allows only once.
#
# Both are resolved the way an intact database would have resolved them: the earliest row
# wins, since that is the one a working index would have pointed at and the one a
# duplicate-avoiding tool would have kept. The rows that lose are counted and reported,
# and the source itself is never modified.
#
# ``NOT INDEXED`` throughout is not decoration: it is what makes these queries see the
# table rather than the damaged index. Grouping by url through that index would report
# every url as unique — the very mistake that produced the duplicates.
#
# The keep sets are materialised once, because a per-batch rule cannot work: two rows
# sharing a url can straddle a batch boundary, and then both would look like the first
# of their group.

_KEEP_SETS = (
    f"""
    CREATE TEMP TABLE keep_rowid AS
    SELECT u.rid AS rid FROM
        (SELECT MIN(rowid) AS rid FROM {SOURCE_ALIAS}.url_table NOT INDEXED GROUP BY url) AS u
    JOIN
        (SELECT MIN(rowid) AS rid FROM {SOURCE_ALIAS}.url_table NOT INDEXED GROUP BY md5) AS m
        ON m.rid = u.rid;
    """,
    "CREATE INDEX keep_rowid_idx ON keep_rowid (rid);",
    f"""
    CREATE TEMP TABLE keep_url AS
    SELECT url FROM {SOURCE_ALIAS}.url_table NOT INDEXED
    WHERE rowid IN (SELECT rid FROM keep_rowid);
    """,
    "CREATE INDEX keep_url_idx ON keep_url (url);",
    f"""
    CREATE TEMP TABLE keep_blob AS
    SELECT MIN(rowid) AS rid FROM {SOURCE_ALIAS}.torrent_table NOT INDEXED
    WHERE url IN (SELECT url FROM keep_url)
    GROUP BY url;
    """,
    "CREATE INDEX keep_blob_idx ON keep_blob (rid);",
)

_COUNT_SOURCE_URLS = f"SELECT COUNT(*) FROM {SOURCE_ALIAS}.url_table NOT INDEXED;"
_COUNT_SOURCE_BLOBS = f"""
SELECT COUNT(*), SUM(torrent_file IS NULL) FROM {SOURCE_ALIAS}.torrent_table NOT INDEXED;
"""
_COUNT_URL_NULLS = f"""
SELECT COUNT(*) FROM {SOURCE_ALIAS}.url_table NOT INDEXED
WHERE {" OR ".join(f"{column} IS NULL" for column in _REQUIRED_URL_COLUMNS)};
"""

_COUNT_KEPT_URLS = f"""
SELECT COUNT(*), COUNT(DISTINCT md5), MIN(add_date), MAX(add_date)
FROM {SOURCE_ALIAS}.url_table NOT INDEXED WHERE rowid IN (SELECT rid FROM keep_rowid);
"""
_COUNT_KEPT_BLOBS = f"""
SELECT COUNT(*), COALESCE(SUM(LENGTH(torrent_file)), 0)
FROM {SOURCE_ALIAS}.torrent_table NOT INDEXED WHERE rowid IN (SELECT rid FROM keep_blob);
"""

_COPY_URLS = f"""
INSERT INTO url_table (url, name, download_size, md5, add_date)
SELECT url, name, download_size, md5, add_date FROM {SOURCE_ALIAS}.url_table NOT INDEXED
WHERE rowid BETWEEN ? AND ? AND rowid IN (SELECT rid FROM keep_rowid);
"""
_COPY_BLOBS = f"""
INSERT INTO torrent_table (url, torrent_file)
SELECT url, torrent_file FROM {SOURCE_ALIAS}.torrent_table NOT INDEXED
WHERE rowid BETWEEN ? AND ? AND rowid IN (SELECT rid FROM keep_blob);
"""


class NothingToImportError(ValueError):
    """The source is a torrent database, but not one that has to be imported.

    Its rows have already been checked against the current constraints, so rebuilding
    it would be work with no result. Reported as "nothing to do" rather than as an
    error, which is what makes running the import twice harmless.
    """


@dataclass(frozen=True, slots=True)
class Plan:
    """The rows an import will take from a source, and the ones it cannot hold.

    ``expected`` is the fingerprint of what the copy must contain once the rows the
    current constraints reject have been left behind; comparing the copy against it is
    what proves the copy is complete. ``source_*`` are the source's raw counts, so the
    difference can be reported rather than discovered later.
    """

    source_rows: int
    source_blobs: int
    expected: Fingerprint

    @property
    def dropped_rows(self) -> int:
        """Rows left behind: the same url, or the same content under another url."""
        return self.source_rows - self.expected.torrents

    @property
    def dropped_blobs(self) -> int:
        """Blobs left behind — those of the rows left behind, and no others."""
        return self.source_blobs - self.expected.blobs


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


def fingerprint(conn: sqlite3.Connection) -> Fingerprint:
    """Summarise the torrent tables of the database *conn* is open on."""
    urls = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT md5), MIN(add_date), MAX(add_date) FROM url_table;"
    ).fetchone()
    blobs = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(torrent_file)), 0) FROM torrent_table;"
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
    """Fail early unless *source* is a legacy database there is something to import from.

    Runs before anything is written, so a typo does not leave a copy or a staging file
    behind.

    Raises:
        ValueError: *source* is not a torrent database at all — the tables the import
            reads are missing.
        NothingToImportError: *source* holds the torrent tables but is already on the
            current schema, so its rows have been checked against the constraints the
            import exists to add.
    """
    try:
        with contextlib.closing(sqlite3.connect(source_uri(source), uri=True)) as conn:
            present = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table';")
            }
            version = db.schema_version(conn)
    except sqlite3.DatabaseError as exc:
        # A truncated download, a text file, someone else's format: all of them are
        # "the wrong file", which is worth saying plainly.
        msg = f"{source} is not a readable SQLite database: {exc}"
        raise ValueError(msg) from exc

    missing = [table for table in _REQUIRED_SOURCE_TABLES if table not in present]
    if missing:
        msg = (
            f"{source} does not look like a legacy torrent database — "
            f"missing table(s): {', '.join(missing)}"
        )
        raise ValueError(msg)

    if version != 0:
        # Every version above zero came out of this project, which means the rows have
        # already been through the import once (or the file was created current).
        msg = (
            f"{source} is already on schema version {version} — there is nothing to "
            f"import. It is upgraded in place, if it needs to be, the first time it is "
            f"opened."
        )
        raise NothingToImportError(msg)


def backup_path(target: Path, *, moment: datetime | None = None) -> Path:
    """Where the copy of *target* goes before an import replaces it."""
    stamp = (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return target.with_name(f"{target.name}{BACKUP_SUFFIX}-{stamp}")


def keep_a_copy(
    target: Path,
    *,
    progress: Progress | None = None,
    moment: datetime | None = None,
) -> Path | None:
    """Copy *target* to :func:`backup_path`, and return where it landed.

    ``None`` when there is nothing to copy: a first import, whose target does not exist
    yet, has nothing to lose.

    The database's own sidecars are copied along with it — a ``-wal`` holds commits the
    main file does not have yet, so a copy of the ``.db`` alone would quietly be older
    than the database it is meant to preserve.
    """
    if not target.is_file():
        return None

    backup = backup_path(target, moment=moment)
    total = target.stat().st_size
    with phase(progress, f"Keeping a copy of the database ({backup.name})", total) as task:
        _copy_file(target, backup, progress=progress, task=task)
        for suffix in SIDECAR_SUFFIXES:
            sidecar = Path(f"{target}{suffix}")
            if sidecar.is_file():
                _copy_file(sidecar, Path(f"{backup}{suffix}"))

    get_logger().info("Kept a copy of the database as it was: {}", backup)
    return backup


def _copy_file(
    source: Path,
    target: Path,
    *,
    progress: Progress | None = None,
    task: TaskID | None = None,
) -> None:
    """Copy *source* to *target*, advancing *task* by the bytes written."""
    with source.open("rb") as src, target.open("wb") as dst:
        while chunk := src.read(BACKUP_CHUNK):
            dst.write(chunk)
            if progress is not None and task is not None:
                progress.advance(task, len(chunk))


def attach_source(conn: sqlite3.Connection, source: Path) -> None:
    """Attach *source* to *conn* read-only, under :data:`SOURCE_ALIAS`."""
    try:
        conn.execute(f"ATTACH DATABASE ? AS {SOURCE_ALIAS};", (source_uri(source),))
    except sqlite3.OperationalError as exc:
        msg = f"{source} cannot be opened read-only: {exc}"
        raise ValueError(msg) from exc


def _rowid_bounds(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    """The rowid range the source table occupies (0, 0 when it is empty).

    ``NOT INDEXED``, so the range comes from the table — which is where the rows are.
    """
    row = conn.execute(
        f"SELECT MIN(rowid), MAX(rowid) FROM {SOURCE_ALIAS}.{table} NOT INDEXED;"
    ).fetchone()
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
    return [Path(f"{path}{suffix}") for suffix in SIDECAR_SUFFIXES]


def plan_source(conn: sqlite3.Connection, *, progress: Progress | None = None) -> Plan:
    """Work out which rows of the attached source the current schema can hold.

    Builds the keep sets (see the note above :data:`_KEEP_SETS`) and returns the
    fingerprint the copy has to match once the rows that lose have been left behind.

    Raises:
        ValueError: the source holds rows the schema cannot take at all — a NULL in a
            column that may not be empty. Duplicates are resolved, not refused; a NULL
            is a defect with no sensible resolution.
    """
    log = get_logger()
    with phase(progress, "Deciding which rows to keep"):
        for statement in _KEEP_SETS:
            conn.execute(statement)

        source_rows = int(conn.execute(_COUNT_SOURCE_URLS).fetchone()[0])
        source_blobs, blank_blobs = conn.execute(_COUNT_SOURCE_BLOBS).fetchone()
        missing = int(conn.execute(_COUNT_URL_NULLS).fetchone()[0])
        if missing or blank_blobs:
            msg = (
                f"the source has rows the current schema cannot hold: "
                f"{missing} row(s) of url_table are missing "
                f"{', '.join(_REQUIRED_URL_COLUMNS)}"
                f"{f' and {blank_blobs} blob(s) have no content' if blank_blobs else ''}. "
                f"Those are defects rather than duplicates, so nothing was imported."
            )
            raise ValueError(msg)

        urls = conn.execute(_COUNT_KEPT_URLS).fetchone()
        blobs = conn.execute(_COUNT_KEPT_BLOBS).fetchone()

    expected = Fingerprint(
        torrents=int(urls[0]),
        distinct_md5=int(urls[1]),
        first_added=urls[2],
        last_added=urls[3],
        blobs=int(blobs[0]),
        blob_bytes=int(blobs[1]),
    )
    plan = Plan(source_rows=source_rows, source_blobs=int(source_blobs), expected=expected)
    if plan.dropped_rows:
        copies = _same_url_again(conn)
        log.warning(
            "The source holds {} row(s) the current schema cannot take: {} of them are "
            "the same url stored again (a topic added a second time — what happens when a "
            "legacy file's primary-key index no longer matches its rows, so its own "
            "duplicate check stops seeing them) and {} are the same content under another "
            "url. Kept the earliest of each, left {} blob(s) behind with them, and kept a "
            "copy of the source as it was.",
            plan.dropped_rows,
            copies,
            plan.dropped_rows - copies,
            plan.dropped_blobs,
        )
    return plan


def _same_url_again(conn: sqlite3.Connection) -> int:
    """How many of the rows being left behind are a copy of a url that was kept.

    The rest were left behind because their *content* is already there under a different
    url, which the log tells apart — they are different things to look at if the numbers
    are ever surprising.
    """
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM {SOURCE_ALIAS}.url_table NOT INDEXED
        WHERE rowid NOT IN (SELECT rid FROM keep_rowid)
          AND url IN (SELECT url FROM keep_url);
        """
    ).fetchone()
    return int(row[0])


def _discard(path: Path) -> None:
    """Delete a database file and whatever SQLite left beside it."""
    path.unlink(missing_ok=True)
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)


def migrate(
    source: Path,
    target: Path,
    *,
    progress: Progress | None = None,
) -> Fingerprint:
    """Copy every row of *source* into a database at *target*, keeping a copy of what was there.

    Returns the fingerprint of the copied data; the fingerprint the copy has to hold —
    the source's rows minus the ones the current constraints cannot take, which
    :func:`plan_source` works out — is compared against it before returning, so a
    truncated copy cannot pass unnoticed.

    An existing *target* is copied aside first (:func:`keep_a_copy`) — that copy is
    never deleted, so nothing depends on the import being right. The import itself is
    staged beside *target* and moved into place at the very end, so *target* keeps its
    old contents until a verified copy exists. Nothing is asked before overwriting:
    with a copy kept, replacing the file is the safe thing to do, and it is what makes
    an in-place import work — ``migrate(db, db)`` reads and rebuilds the same file,
    which is how a database found to be on the legacy schema is upgraded (see
    :func:`ricercar.repository.db.check_schema`).

    Pass a :class:`rich.progress.Progress` to follow along; without one the import is
    silent apart from the log.

    Raises:
        FileNotFoundError: *source* is not a file.
        ValueError: *source* is not a torrent database, or holds rows the current schema
            cannot take at all (a NULL where a value is required).
        NothingToImportError: *source* is already on the current schema.
    """
    log = get_logger()
    started = time.monotonic()

    if not source.is_file():
        msg = f"{source} does not exist"
        raise FileNotFoundError(msg)

    check_source(source)

    # Before anything is written: whatever is at the target may be a database of its
    # own (an import into an existing one is legitimate), and the import replaces it.
    keep_a_copy(target, progress=progress)

    staging = target.with_name(f"{target.name}{STAGING_SUFFIX}")
    _discard(staging)  # leftovers from an interrupted run

    conn = connect(staging)
    failed = True
    try:
        ensure_schema(conn, staging)
        attach_source(conn, source)
        try:
            plan = plan_source(conn, progress=progress)
            expected = plan.expected
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
        "Migrated to {} in {:.1f}s: {}{}",
        target,
        time.monotonic() - started,
        actual.describe(),
        f" (out of {plan.source_rows} source rows)" if plan.dropped_rows else "",
    )
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ricercar-migrate",
        description=(
            "Import the legacy database at database.path into the current schema, in "
            "place. A database that has already been imported is left alone."
        ),
    )
    parser.add_argument(
        "source",
        type=str,
        nargs="?",
        default="",
        metavar="SOURCE",
        help=(
            "legacy SQLite database to read (default: database.path from the "
            "configuration — the database a run uses)"
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log)
    # Empty, not None, is what an unset positional and an unset justfile parameter both
    # arrive as — and both mean "the database the configuration names".
    named = bool(args.source.strip())
    source = Path(args.source) if named else settings.db_path
    # The configuration says where the database lives, so that is also where an import
    # lands: the same file, the same name.
    target = settings.db_path
    log = get_logger().bind(source=str(source), target=str(target))

    from rich import print as rprint

    if not source.is_file():
        if named:
            log.error("{} does not exist", source)
        else:
            log.error(
                "There is no database at {} (database.path in the configuration) — put "
                "the legacy database there and run this again. A run creates the file, "
                "empty, when it is missing.",
                source,
            )
        return 1

    try:
        with new_progress() as progress:
            result = migrate(source, target, progress=progress)
    except NothingToImportError as exc:
        # Not a failure: running the import twice is meant to be harmless.
        log.info("{}", exc)
        return 0
    except sqlite3.IntegrityError as exc:
        # The import refuses rows the current schema cannot take rather than dropping
        # them; a constraint that still fires is a defect in the source, and it deserves
        # the reason rather than a traceback.
        log.error(
            "{} holds rows the current schema rejects ({}). Nothing was written; the "
            "database is untouched.",
            source,
            exc,
        )
        return 1
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        log.error("{}", exc)
        return 1

    rprint(f"[green]{target}[/green]: {result.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
