"""Persistence for downloaded torrents."""

from ricercar.repository.db import (
    SCHEMA_VERSION,
    DatabaseSchemaError,
    LegacyDatabaseError,
    SchemaVersionError,
    StoredTorrent,
    TorrentRepository,
    check_database,
    connect,
    ensure_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "DatabaseSchemaError",
    "LegacyDatabaseError",
    "SchemaVersionError",
    "StoredTorrent",
    "TorrentRepository",
    "check_database",
    "connect",
    "ensure_schema",
]
