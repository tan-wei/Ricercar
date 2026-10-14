"""Discovered uploaders — tracking new uploaders between runs.

A run records every new uploader it encounters (the ones
:meth:`~ricercar.state.StateStore.remember_authors` has not seen before), along with
the section and the timestamp.  The ``ricercar tasks --discovered`` subcommand lists
them; ``ricercar tasks --add-discovered`` appends every unconfirmed uploader to the
YAML config as a new task and marks them as confirmed.

The file is small, human-readable JSON, and every write is atomic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ricercar.log import get_logger


@dataclass
class DiscoveredUploader:
    """One uploader discovered during a run."""

    author: str
    """The uploader's name on the tracker."""
    source: str
    """Which source (e.g. ``rutracker``) the uploader was found on."""
    first_seen: str
    """ISO-8601 timestamp of the first sighting."""
    last_seen: str
    """ISO-8601 timestamp of the most recent sighting."""
    sections_seen: list[int] = field(default_factory=list)
    """The category/section IDs the uploader has been found in."""
    hits: int = 0
    """How many torrents (result rows) have been seen from this uploader in total."""
    confirmed: bool = False
    """Whether a task has been generated for this uploader."""


class DiscoveredUploaders:
    """A JSON file that remembers uploaders discovered across runs.

    Every write goes through an atomic rename, so a crash in the middle never truncates
    the file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._uploaders: dict[str, DiscoveredUploader] = {}
        """Keyed by ``(source, author)`` via :meth:`_key`."""
        self._loaded = False

    @staticmethod
    def _key(source: str, author: str) -> str:
        return f"{source}\0{author}"

    # ── Loading & saving ────────────────────────────────────────────────

    def load(self) -> DiscoveredUploaders:
        """Read the file; treat anything unreadable as "nothing discovered yet"."""
        if self._loaded:
            return self
        self._loaded = True

        if not self._path.is_file():
            return self

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            get_logger().warning(
                "Ignoring unreadable discovered-uploaders file {}: {}", self._path, exc
            )
            return self

        if not isinstance(data, list):
            get_logger().warning(
                "Ignoring discovered-uploaders file {} — expected a list, got {!r}",
                self._path,
                type(data).__name__,
            )
            return self

        for item in data:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            source = item.get("source")
            if not author or not source:
                continue
            key = self._key(str(source), str(author))
            self._uploaders[key] = DiscoveredUploader(
                author=str(author),
                source=str(source),
                first_seen=str(item.get("first_seen", "")),
                last_seen=str(item.get("last_seen", "")),
                sections_seen=list(item.get("sections_seen", [])),
                hits=int(item.get("hits", 0)),
                confirmed=bool(item.get("confirmed", False)),
            )
        return self

    def save(self) -> None:
        """Write atomically."""
        if not self._loaded:
            return

        payload = [
            {
                "author": u.author,
                "source": u.source,
                "first_seen": u.first_seen,
                "last_seen": u.last_seen,
                "sections_seen": sorted(u.sections_seen),
                "hits": u.hits,
                "confirmed": u.confirmed,
            }
            for u in sorted(
                self._uploaders.values(),
                key=lambda u: (u.source, u.author),
            )
        ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._path.with_name(f"{self._path.name}.tmp")
        staging.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, self._path)

    # ── Recording ────────────────────────────────────────────────────────

    def record(
        self,
        source: str,
        author: str,
        *,
        section: int | None = None,
        now: str | None = None,
    ) -> None:
        """Record that *author* was seen on *source*, optionally in *section*.

        If the uploader is already known, only ``last_seen``, ``sections_seen`` and
        ``hits`` are updated.
        """
        self.load()
        key = self._key(source, author)
        ts = now or datetime.now(UTC).isoformat()

        if key in self._uploaders:
            existing = self._uploaders[key]
            existing.last_seen = ts
            existing.hits += 1
            if section is not None and section not in existing.sections_seen:
                existing.sections_seen.append(section)
        else:
            self._uploaders[key] = DiscoveredUploader(
                author=author,
                source=source,
                first_seen=ts,
                last_seen=ts,
                sections_seen=[section] if section is not None else [],
                hits=1,
                confirmed=False,
            )

    # ── Query ────────────────────────────────────────────────────────────

    def unconfirmed(self, source: str | None = None) -> list[DiscoveredUploader]:
        """Uploaders that have not been turned into tasks yet.

        If *source* is given, only those from that source are returned.
        When all uploaders are confirmed, returns an empty list.
        """
        self.load()
        result = [u for u in self._uploaders.values() if not u.confirmed]
        if source:
            result = [u for u in result if u.source == source]
        return sorted(result, key=lambda u: (u.hits, u.last_seen), reverse=True)

    def mark_confirmed(self, source: str, author: str) -> bool:
        """Mark one uploader as confirmed.  Returns ``True`` if it was found."""
        self.load()
        key = self._key(source, author)
        if key not in self._uploaders:
            return False
        self._uploaders[key].confirmed = True
        return True

    def mark_all_confirmed(self, source: str | None = None) -> int:
        """Mark every unconfirmed uploader as confirmed.

        If *source* is given, only those from that source are marked.
        Returns the number of uploaders that were marked.
        """
        self.load()
        count = 0
        for uploader in self._uploaders.values():
            if uploader.confirmed:
                continue
            if source is not None and uploader.source != source:
                continue
            uploader.confirmed = True
            count += 1
        return count

    @property
    def total(self) -> int:
        """Total number of distinct (source, author) pairs recorded."""
        self.load()
        return len(self._uploaders)
