"""SQLite persistence for downloaded torrents.

The tables keep the legacy names and columns, plus the constraints the legacy
schema was missing:

* ``torrent_table.url`` is the primary key — the legacy table had no key at all,
  so nothing stopped one topic from accumulating several blobs.
* ``md5`` is unique, which is what actually enforces the "MD5 dedup" the README
  promises; legacy only deduplicated on the URL, in code.
* Nothing nullable is nullable, and dates stay in the legacy local-time text
  format so migrated rows are byte-for-byte what they were.

``user_version`` says which shape the file is in: ``0`` means the legacy tool wrote
it (import it — see :mod:`ricercar.repository.migrate`), anything older than
:data:`SCHEMA_VERSION` is upgraded in place on first use (see
:mod:`ricercar.repository.migrations`), and anything newer is refused.

SQLite has no connection pool worth the name. What survives concurrent access is
WAL (readers do not block the writer), ``busy_timeout``, and a retry around each
transaction — so a locked database waits instead of failing.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ricercar.log import get_logger
from ricercar.models import TorrentMetadata
from ricercar.repository.migrations import upgrade

SCHEMA_VERSION = 3
"""Value written to ``PRAGMA user_version``; legacy databases carry 0."""

BUSY_TIMEOUT_MS = 5_000
"""How long SQLite waits for a lock before giving up (then we retry)."""

_ADD_ATTEMPTS = 3

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS url_table
    (
        url           TEXT PRIMARY KEY NOT NULL,
        name          TEXT NOT NULL,
        download_size INTEGER NOT NULL,
        md5           TEXT NOT NULL UNIQUE,
        add_date      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS torrent_table
    (
        url          TEXT PRIMARY KEY NOT NULL REFERENCES url_table(url) ON DELETE CASCADE,
        torrent_file BLOB NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_url_table_add_date ON url_table (add_date);",
)
"""The schema of a fresh database, one statement at a time.

Separate statements rather than one script: ``executescript`` commits whatever is pending
before it runs, which would make creating a fresh database impossible to wrap in the
transaction that also stamps its version.
"""

_INSERT_URL = "INSERT INTO url_table (url, name, download_size, md5) VALUES (?, ?, ?, ?);"
_INSERT_URL_IGNORING_DUPES = (
    "INSERT OR IGNORE INTO url_table (url, name, download_size, md5) VALUES (?, ?, ?, ?);"
)
_INSERT_BLOB = "INSERT INTO torrent_table (url, torrent_file) VALUES (?, ?);"
_SELECT_URL = "SELECT add_date FROM url_table WHERE url = ?;"
_SELECT_MD5 = "SELECT url FROM url_table WHERE md5 = ?;"
# A range on the text date, not `DATE(add_date) = DATE(...)`: the format is
# `YYYY-MM-DD HH:MM:SS`, so today's rows are a printable range — and unlike a function
# on the column, a range uses idx_url_table_add_date. Both bounds are inclusive/exclusive
# so this stays equivalent to comparing the dates (a row dated tomorrow is not today).
_COUNT_TODAY = (
    "SELECT COUNT(*) FROM url_table WHERE add_date >= DATE('now','localtime') "
    "AND add_date < DATE('now','localtime','+1 day');"
)
_COUNT_TODAY_FROM = (
    "SELECT COUNT(*) FROM url_table WHERE add_date >= DATE('now','localtime') "
    "AND add_date < DATE('now','localtime','+1 day') AND url LIKE ?;"
)


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection carrying the pragmas this project relies on.

    ``uri=True`` is needed so a database can be attached read-only; ordinary
    paths are unaffected by it (SQLite only reads URI syntax for ``file:``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, uri=True)
    conn.row_factory = sqlite3.Row
    for pragma in (
        "journal_mode=WAL",
        "synchronous=NORMAL",
        "foreign_keys=ON",
        f"busy_timeout={BUSY_TIMEOUT_MS}",
    ):
        conn.execute(f"PRAGMA {pragma};")
    return conn


# ── Schema compatibility ──────────────────────────────────────────────────


class DatabaseSchemaError(RuntimeError):
    """The database file cannot be used by this build as it stands."""


class LegacyDatabaseError(DatabaseSchemaError):
    """The database was written before schema versioning — i.e. by the legacy tool.

    Raised instead of silently writing into it, which would add the new
    constraints to a database whose rows were never checked against them.
    """


class SchemaVersionError(DatabaseSchemaError):
    """The database was written by a different version of this project."""


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's ``PRAGMA user_version`` — 0 for anything pre-versioning."""
    return int(conn.execute("PRAGMA user_version;").fetchone()[0])


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    """The tables the file already contains, ignoring SQLite's own."""
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
        )
    }


def check_schema(conn: sqlite3.Connection, path: Path | None = None) -> None:
    """Fail with an actionable message unless *conn* holds a usable database.

    An empty file is fine — it is a fresh database and :func:`ensure_schema`
    creates the tables. So is an older one this build knows how to upgrade. What is
    not fine is a database from the legacy tool, whose rows were never checked
    against the current constraints, or one written by a newer version of this
    project, whose shape this build cannot know.

    Raises:
        LegacyDatabaseError: the file predates schema versioning.
        SchemaVersionError: the file carries a newer schema version.
    """
    if not existing_tables(conn):
        return

    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        return

    where = path if path is not None else Path("the database")
    if version == 0:
        msg = (
            f"{where} still has the legacy schema (PRAGMA user_version = 0), so it has "
            f"to be imported before it can be used.\n"
            f"The import reads {where}, keeps a copy of it beside itself, and rebuilds "
            f"the same file on the current schema:\n"
            f"  just migrate"
        )
        raise LegacyDatabaseError(msg)

    if version < SCHEMA_VERSION:
        # Older, but every step needed to reach the current schema exists, so it is
        # upgraded in place on first use (see ensure_schema).
        return

    msg = (
        f"{where} has schema version {version}, but this build reads at most "
        f"{SCHEMA_VERSION} — it was written by a newer version of Ricercar.\n"
        f"Update the tool (the database itself needs nothing)."
    )
    raise SchemaVersionError(msg)


def check_database(path: Path) -> None:
    """Check an existing database file without touching it.

    A missing file is not a problem: it means a fresh database, which the
    repository creates on first use.

    Raises:
        DatabaseSchemaError: the file exists but is not usable as it stands.
    """
    if not path.is_file():
        return
    with contextlib.closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        check_schema(conn, path)


def ensure_schema(conn: sqlite3.Connection, path: Path | None = None) -> None:
    """Create or upgrade the schema, and stamp the version.

    A fresh database is created at the current version; an older one this build can
    still read is upgraded in place; an up-to-date one is not touched at all (opening a
    database must not write to it). Refuses what :func:`check_schema` refuses.
    """
    check_schema(conn, path)

    if not existing_tables(conn):
        _transactional(conn, SCHEMA_STATEMENTS, version=SCHEMA_VERSION)
        return

    version = schema_version(conn)
    if version < SCHEMA_VERSION:
        upgrade(conn, current=version, target=SCHEMA_VERSION)


def _transactional(
    conn: sqlite3.Connection,
    statements: Sequence[str],
    *,
    version: int,
) -> None:
    """Run *statements* and stamp *version* in one transaction, or neither.

    Explicit, because sqlite3 only opens an implicit transaction for DML: these are all
    DDL and a version stamp, both of which it would otherwise commit as it goes.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        for statement in statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={version};")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StoredTorrent:
    """One row of ``url_table`` — the metadata, without the blob."""

    url: str
    name: str
    download_size: int
    md5: str
    add_date: str
    """Local-time ``YYYY-MM-DD HH:MM:SS``, as written by the legacy tool."""


class TorrentRepository:
    """The torrent database.

    Use it as a context manager::

        with TorrentRepository(cfg.db_path) as repo:
            repo.add(meta, path.read_bytes())

    A connection is opened lazily and the schema is created for a fresh file, so
    the constructor never touches the disk.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """Location of the database file."""
        return self._path

    def open(self) -> TorrentRepository:
        """Connect (creating the schema when the file is new).

        Either the repository ends up open, or nothing is left behind: a database this
        build refuses (see :func:`check_schema`) closes the connection it opened on the
        way out, instead of leaking it to the garbage collector.
        """
        if self._conn is not None:
            return self

        conn = connect(self._path)
        try:
            ensure_schema(conn, self._path)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return self

    def close(self) -> None:
        """Close the connection (a no-op when it was never opened)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> TorrentRepository:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            msg = "TorrentRepository is not open — use it as a context manager"
            raise RuntimeError(msg)
        return self._conn

    # ── Queries ─────────────────────────────────────────────────────────

    def count(self) -> int:
        """How many torrents the database holds."""
        return int(self._db.execute("SELECT COUNT(*) FROM url_table;").fetchone()[0])

    def count_today(self) -> int:
        """How many torrents were added today (the daily quota's counter)."""
        return int(self._db.execute(_COUNT_TODAY).fetchone()[0])

    def count_today_from(self, host: str) -> int:
        """How many torrents from *host* were added today.

        The daily quota is per tracker, and a stored torrent's only record of where it
        came from is its URL, which carries the host.
        """
        return int(self._db.execute(_COUNT_TODAY_FROM, (f"%://{host}/%",)).fetchone()[0])

    def has_url(self, url: str) -> bool:
        """Whether this topic URL is already stored."""
        return self._db.execute(_SELECT_URL, (url,)).fetchone() is not None

    def has_md5(self, md5: str) -> bool:
        """Whether this content (whole-file MD5) is already stored."""
        return self._db.execute(_SELECT_MD5, (md5,)).fetchone() is not None

    def recent(self, limit: int = 20) -> list[StoredTorrent]:
        """The most recently added torrents, newest first."""
        rows = self._db.execute(
            "SELECT url, name, download_size, md5, add_date FROM url_table "
            "ORDER BY add_date DESC, rowid DESC LIMIT ?;",
            (limit,),
        ).fetchall()
        return [StoredTorrent(**dict(row)) for row in rows]

    def blob(self, url: str) -> bytes | None:
        """The stored ``.torrent`` for a topic, or ``None`` when unknown."""
        row = self._db.execute(
            "SELECT torrent_file FROM torrent_table WHERE url = ?;", (url,)
        ).fetchone()
        return bytes(row[0]) if row is not None else None

    # ── Writes ──────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(sqlite3.OperationalError),
        stop=stop_after_attempt(_ADD_ATTEMPTS),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def add(self, meta: TorrentMetadata, blob: bytes) -> bool:
        """Store one torrent; ``False`` when it is already known.

        Both the topic URL and the content MD5 are checked, so the same content
        re-released under a second topic URL is skipped as well.
        """
        log = get_logger()
        if (known := self.has_url(meta.url)) or self.has_md5(meta.md5):
            log.debug("Already stored ({}): {}", "url" if known else "md5", meta.url)
            return False

        try:
            with self._db:
                self._db.execute(_INSERT_URL, (meta.url, meta.name, meta.size, meta.md5))
                self._db.execute(_INSERT_BLOB, (meta.url, sqlite3.Binary(blob)))
        except sqlite3.IntegrityError:
            # Another writer won the race between our check and the insert.
            log.debug("Lost a race storing {}; treating it as a duplicate", meta.url)
            return False
        return True

    def add_many(self, entries: Iterable[tuple[TorrentMetadata, bytes]]) -> int:
        """Store several torrents in one transaction; returns how many were new.

        One transaction is the point: with ``synchronous=NORMAL`` in WAL mode a
        single commit for the whole batch costs far less than one per torrent.
        """
        log = get_logger()
        added = 0
        try:
            with self._db:
                for meta, blob in entries:
                    cursor = self._db.execute(
                        _INSERT_URL_IGNORING_DUPES, (meta.url, meta.name, meta.size, meta.md5)
                    )
                    if cursor.rowcount == 0:
                        continue
                    self._db.execute(_INSERT_BLOB, (meta.url, sqlite3.Binary(blob)))
                    added += 1
        except sqlite3.IntegrityError as exc:
            log.warning("Batch stopped early after {} insert(s): {}", added, exc)

        log.info("Stored {} new torrent(s) out of the batch", added)
        return added

    def stats(self) -> dict[str, Any]:
        """A small summary, handy for verification and logging."""
        row = self._db.execute(
            "SELECT COUNT(*) AS torrents, COUNT(DISTINCT md5) AS distinct_md5, "
            "SUM(download_size) AS total_bytes, MIN(add_date) AS first_added, "
            "MAX(add_date) AS last_added FROM url_table;"
        ).fetchone()
        blobs = self._db.execute(
            "SELECT COUNT(*) AS blobs, COALESCE(SUM(LENGTH(torrent_file)), 0) AS blob_bytes "
            "FROM torrent_table;"
        ).fetchone()
        return {**dict(row), **dict(blobs), "schema_version": self.schema_version}

    @property
    def schema_version(self) -> int:
        """``PRAGMA user_version`` of the open database."""
        return int(self._db.execute("PRAGMA user_version;").fetchone()[0])
