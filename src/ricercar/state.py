"""Run state that outlives a run.

Two things are worth remembering between runs, and the legacy tool kept both:

* **the uploaders seen so far** (legacy ``authors.json``), so a run can report the
  uploaders it discovered for the first time;
* **which tasks are done**, so an interrupted run is resumed instead of repeated.

The file is small, human-readable JSON and written atomically — a state file that
cannot be read is a warning, never a crash.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ricercar.config import SearchTask
from ricercar.log import get_logger

STATE_VERSION = 1
"""Bumped when the layout changes; an unknown version is treated as empty."""


def task_key(task: SearchTask) -> str:
    """A stable, readable key for a search task."""
    category = "" if task.category is None else str(task.category)
    return f"text={task.text}|author={task.author or ''}|category={category}"


def today() -> str:
    """Today's local date, as used in the state file and in the database."""
    return datetime.now().strftime("%Y-%m-%d")


@dataclass
class SourceState:
    """What is remembered about one source."""

    authors: set[str] = field(default_factory=set)
    """Every uploader seen so far."""
    completed: dict[str, str] = field(default_factory=dict)
    """Task key → ISO timestamp of the run that finished it."""
    last_run: str | None = None
    """ISO timestamp of the last run against this source."""

    def completed_today(self) -> set[str]:
        """Keys of the tasks finished today."""
        return {key for key, stamp in self.completed.items() if stamp[:10] == today()}

    def keep_only(self, day: str) -> None:
        """Forget the tasks finished on any other day.

        Only today's matter — the resume filter asks nothing else — and a run can finish
        thousands of searches (every uploader in every configured section), so keeping
        the whole history would grow the file without ever being read.
        """
        self.completed = {key: stamp for key, stamp in self.completed.items() if stamp[:10] == day}

    def to_json(self) -> dict[str, object]:
        return {
            "authors": sorted(self.authors),
            "completed": dict(sorted(self.completed.items())),
            "last_run": self.last_run,
        }

    @classmethod
    def from_json(cls, data: object) -> SourceState:
        if not isinstance(data, dict):
            return cls()
        authors = data.get("authors")
        completed = data.get("completed")
        last_run = data.get("last_run")
        return cls(
            authors={str(a) for a in authors} if isinstance(authors, list) else set(),
            completed={str(k): str(v) for k, v in completed.items()}
            if isinstance(completed, dict)
            else {},
            last_run=str(last_run) if last_run else None,
        )


class StateStore:
    """The state file, loaded lazily and saved atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sources: dict[str, SourceState] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    # ── Loading & saving ────────────────────────────────────────────────

    def load(self) -> StateStore:
        """Read the file, treating anything unreadable as "no state yet"."""
        if self._loaded:
            return self
        self._loaded = True

        if not self._path.is_file():
            return self

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            get_logger().warning("Ignoring unreadable state file {}: {}", self._path, exc)
            return self

        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            get_logger().warning(
                "Ignoring state file {} (version {!r}, expected {})",
                self._path,
                data.get("version") if isinstance(data, dict) else None,
                STATE_VERSION,
            )
            return self

        sources = data.get("sources")
        if isinstance(sources, dict):
            self._sources = {
                str(name): SourceState.from_json(value) for name, value in sources.items()
            }
        return self

    def save(self) -> None:
        """Write the state file, atomically (a crash must not truncate it)."""
        if not self._loaded:
            return

        day = today()
        for state in self._sources.values():
            state.keep_only(day)
        payload = {
            "version": STATE_VERSION,
            "sources": {name: state.to_json() for name, state in sorted(self._sources.items())},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._path.with_name(f"{self._path.name}.tmp")
        staging.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, self._path)

    # ── Access ──────────────────────────────────────────────────────────

    def source(self, name: str) -> SourceState:
        """The state of one source, created empty on first use."""
        self.load()
        return self._sources.setdefault(name, SourceState())

    def known_authors(self, name: str) -> set[str]:
        """The uploaders already seen for *name*."""
        return set(self.source(name).authors)

    def remember_authors(self, name: str, authors: Iterable[str]) -> set[str]:
        """Add *authors* and return the ones that were not known before."""
        state = self.source(name)
        seen = {str(author) for author in authors if author}
        new = seen - state.authors
        state.authors |= seen
        return new

    def completed_today(self, name: str) -> set[str]:
        """Keys of the tasks already finished today for *name*."""
        return self.source(name).completed_today()

    def mark_completed(self, name: str, task: SearchTask, *, when: datetime | None = None) -> None:
        """Record that *task* finished, so a resume does not repeat it."""
        moment = (when or datetime.now()).isoformat(timespec="seconds")
        state = self.source(name)
        state.completed[task_key(task)] = moment
        state.last_run = moment
        self.save()


__all__ = ["STATE_VERSION", "SourceState", "StateStore", "task_key", "today"]
