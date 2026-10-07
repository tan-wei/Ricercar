"""``.torrent`` parsing and validation.

``bencode.py`` decodes keys and text to ``str`` but keeps the binary ``pieces``
as ``bytes``, which is what makes the internal consistency check below possible.

The shape this produces is :class:`ricercar.models.TorrentMetadata`, which the
repository stores — the parser itself knows nothing about any tracker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from math import ceil
from pathlib import Path
from typing import Any

import bencode

from ricercar.models import TorrentMetadata

PIECES_HASH_LENGTH = 20
"""Bytes per piece hash (SHA-1) in the ``pieces`` string."""

_MISSING = object()


class TorrentError(ValueError):
    """The bytes are not a usable ``.torrent`` file."""


def parse_file(path: Path) -> TorrentMetadata:
    """Parse a ``.torrent`` file from disk."""
    return parse(path.read_bytes())


def parse(data: bytes) -> TorrentMetadata:
    """Parse and validate ``.torrent`` bytes.

    Raises:
        TorrentError: the data is not bencode, or is missing something a torrent
            cannot work without (``info``, ``info.name``, and a payload length).
    """
    try:
        decoded = bencode.bdecode(data)
    except Exception as exc:
        msg = "not a valid bencode file"
        raise TorrentError(msg) from exc

    if not isinstance(decoded, Mapping):
        msg = "the top level of a .torrent must be a dictionary"
        raise TorrentError(msg)

    info = _lookup(decoded, "info")
    if not isinstance(info, Mapping):
        msg = "'info' is missing or not a dictionary"
        raise TorrentError(msg)

    name = _text(_lookup(info, "name", default=""))
    if not name:
        msg = "'info.name' is missing"
        raise TorrentError(msg)

    size, file_count = _payload_size(info)

    return TorrentMetadata(
        url=_text(_lookup(decoded, "comment", default="")),
        name=name,
        size=size,
        file_count=file_count,
        md5=hashlib.md5(data).hexdigest(),
        issues=_consistency_issues(info, size),
    )


# ── Internals ─────────────────────────────────────────────────────────────


def _payload_size(info: Mapping[Any, Any]) -> tuple[int, int]:
    """Return ``(total_bytes, file_count)`` for a multi- or single-file torrent."""
    files = _lookup(info, "files", default=None)
    if files:
        try:
            total = sum(int(_lookup(entry, "length")) for entry in files)
        except (TorrentError, TypeError, ValueError) as exc:
            msg = "malformed 'info.files' entry"
            raise TorrentError(msg) from exc
        return total, len(files)

    try:
        return int(_lookup(info, "length")), 1
    except (TorrentError, TypeError, ValueError) as exc:
        msg = "torrent has neither 'info.files' nor 'info.length'"
        raise TorrentError(msg) from exc


def _consistency_issues(info: Mapping[Any, Any], size: int) -> tuple[str, ...]:
    """Check the invariants a sound torrent always satisfies.

    Reported rather than raised: the file is still usable (the payload can be
    fetched), and a strict check must never reject a torrent that is merely
    unusual.
    """
    issues: list[str] = []

    if size <= 0:
        issues.append(f"non-positive payload size ({size})")

    try:
        piece_length = int(_lookup(info, "piece length"))
    except (TorrentError, TypeError, ValueError):
        return (*issues, "'piece length' is missing or malformed")

    pieces = _lookup(info, "pieces", default=None)
    if not isinstance(pieces, bytes):
        return (*issues, "'pieces' is not binary — the piece count cannot be verified")

    if len(pieces) % PIECES_HASH_LENGTH:
        issues.append(f"'pieces' length {len(pieces)} is not a multiple of 20")

    if piece_length > 0:
        expected = ceil(size / piece_length)
        actual = len(pieces) // PIECES_HASH_LENGTH
        if actual != expected:
            issues.append(f"{actual} piece hash(es) but size and piece length imply {expected}")

    return tuple(issues)


def _lookup(mapping: Mapping[Any, Any], key: str, default: Any = _MISSING) -> Any:
    """Fetch *key*, tolerating both ``str`` and ``bytes`` bencode keys."""
    for candidate in (key, key.encode()):
        if candidate in mapping:
            return mapping[candidate]
    if default is not _MISSING:
        return default
    msg = f"'{key}' is missing"
    raise TorrentError(msg)


def _text(value: Any) -> str:
    """Decode a bencode text value, which may still arrive as ``bytes``."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)
