"""Schema changes applied to a database that already holds torrents.

Three situations, three mechanisms:

* a **fresh** database is created straight at
  :data:`ricercar.repository.db.SCHEMA_VERSION` from ``db.SCHEMA_STATEMENTS``;
* a **legacy** database (no ``user_version`` at all) is imported by
  :mod:`ricercar.repository.migrate`, because its rows have never been checked against
  the current constraints;
* an **older database this project wrote itself** is upgraded in place by the steps
  below — which is this module.

A step is small, idempotent and ordered by version: it runs inside a transaction that
also stamps the new ``PRAGMA user_version``, so an upgrade interrupted halfway leaves the
database at the last version it completed and the next open continues from there.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from ricercar.log import get_logger


@dataclass(frozen=True, slots=True)
class Migration:
    """One step, taking a database from ``version`` to ``version + 1``."""

    version: int
    """The version the step starts from."""
    description: str
    """What it does, in the terms the log and the changelog use."""
    apply: Callable[[sqlite3.Connection], None]
    """The change itself; has to be safe to run twice."""


def _index_add_date(conn: sqlite3.Connection) -> None:
    """Index the column the daily counters filter on."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_url_table_add_date ON url_table (add_date);")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=2,
        description="index url_table(add_date), which the daily counters query",
        apply=_index_add_date,
    ),
)
"""Every implemented step, in order. A new one is appended, never edited."""


def pending(current: int, target: int) -> tuple[Migration, ...]:
    """The steps that take a database from *current* to *target*, in order."""
    return tuple(
        sorted(
            (migration for migration in MIGRATIONS if current <= migration.version < target),
            key=lambda migration: migration.version,
        )
    )


def _apply(conn: sqlite3.Connection, step: Migration) -> None:
    """Run one step and its version stamp in a single transaction.

    Explicit rather than ``with conn:``: sqlite3 only opens an implicit transaction for
    DML, and every step here is DDL — which it would commit straight away, including the
    stamp that says the step is done.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        step.apply(conn)
        conn.execute(f"PRAGMA user_version={step.version + 1};")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def upgrade(conn: sqlite3.Connection, *, current: int, target: int) -> int:
    """Apply the pending steps and return the version the database is at afterwards.

    Each step commits on its own, together with its version stamp: a step that fails
    leaves the database usable at the version it had, instead of half-changed.
    """
    log = get_logger()
    version = current
    for step in pending(current, target):
        log.info(
            "Upgrading the database schema: v{} → v{} — {}",
            step.version,
            step.version + 1,
            step.description,
        )
        _apply(conn, step)
        version = step.version + 1
    return version


__all__ = ["MIGRATIONS", "Migration", "pending", "upgrade"]
