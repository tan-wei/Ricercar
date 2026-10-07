"""What each configured task has yielded over time.

The database answers "do I already have this torrent?"; it cannot answer "is this *task*
still worth running?" — that is a question about a task's yield over months, which is
neither a torrent nor a row, so it does not belong in the database. It lives in its own
small JSON file instead (``run.task_history_file``): how many times a task ran, how many
result rows came back, and the last day each of those happened.

Two things follow from that purpose:

* the file is *not* pruned to today (unlike :mod:`ricercar.state`, which is about
  resuming an interrupted run) — "this task has found nothing for two months" is exactly
  the kind of memory worth keeping;
* it is written once per run rather than once per search, because a run finishes
  thousands of searches and every write rewrites the file.

What it is for: pruning the task list. A task that has not returned a single result row
for ``run.stale_task_days`` days, over at least ``run.stale_task_runs`` runs, is reported at
the end of a run as a candidate for removal. Both thresholds have to be met, so a task that
is new — or that the random sample has barely picked yet — is never judged: no history is
not the same as a bad history.

Entries outlive the tasks they describe. Removing a task from the config leaves its yield
behind, which is what lets the report tell you that a task *used* to find things and has
stopped, before you delete it for good.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ricercar.config import SourceSettings
from ricercar.log import get_logger
from ricercar.state import task_key, today

HISTORY_VERSION = 1
"""Bumped when the layout changes; an unknown version is treated as empty."""


def configured_task_keys(settings: SourceSettings) -> set[str]:
    """The keys of the tasks a source's configuration lists, as written.

    As *written* means before :meth:`Source.expand` turns one task into its searches:
    the history is kept per configured task, because that is the line you would delete
    from the config.
    """
    return {task_key(task) for task in (*settings.must_complete_tasks, *settings.tasks)}


def _days_between(stamp: str, day: str) -> int:
    """Whole days from the ISO date *stamp* to the ISO date *day* (never negative)."""
    try:
        then = date.fromisoformat(stamp[:10])
        now = date.fromisoformat(day[:10])
    except ValueError:  # pragma: no cover - a hand-edited file
        return 0
    return max((now - then).days, 0)


@dataclass(slots=True)
class TaskYield:
    """The long-term yield of one configured task."""

    key: str
    """Task key of the task *as configured* (before it is expanded into searches)."""
    description: str = ""
    """The task in one line, so the file (and the report) reads without the config."""
    runs: int = 0
    """How many runs planned and ran this task."""
    searches: int = 0
    """How many searches those runs actually performed (one task can be many)."""
    hits: int = 0
    """Result rows seen, duplicates included — a row means the search works."""
    stored: int = 0
    """Torrents stored because of this task."""
    first_run: str | None = None
    last_run: str | None = None
    last_hit: str | None = None
    """The last day a search for this task returned anything at all."""
    last_stored: str | None = None
    """The last day it brought something the database did not have yet."""

    def age(self, day: str | None = None) -> int:
        """Days since this task was first run (0 when it never was)."""
        return 0 if self.first_run is None else _days_between(self.first_run, day or today())

    def days_without_hit(self, day: str | None = None) -> int | None:
        """Days since the last result row, or ``None`` when there never was one."""
        if self.last_hit is None:
            return None
        return _days_between(self.last_hit, day or today())

    def days_without_new(self, day: str | None = None) -> int | None:
        """Days since the last torrent this task brought, or ``None`` when there was none."""
        if self.last_stored is None:
            return None
        return _days_between(self.last_stored, day or today())

    def stale(self, *, min_runs: int, min_days: int, day: str | None = None) -> bool:
        """Whether this task looks dead — long enough, run often enough, finding nothing.

        All three conditions matter: a task only counts as a candidate for removal once it
        is old enough to judge, has actually run a few times, and has not seen a result row
        within the window.
        """
        if self.runs < min_runs or self.age(day) < min_days:
            return False
        without_hit = self.days_without_hit(day)
        return without_hit is None or without_hit >= min_days

    def unrewarding(self, *, min_runs: int, min_days: int, day: str | None = None) -> bool:
        """Whether this task still finds rows but has brought nothing new for a long time.

        Weaker than :meth:`stale`: a task that is simply already collected looks like this,
        which is normal for a monitor. It is reported, never warned about.
        """
        if self.runs < min_runs or self.age(day) < min_days or self.hits == 0:
            return False
        without_new = self.days_without_new(day)
        return without_new is None or without_new >= min_days

    def verdict(self, *, min_runs: int, min_days: int, day: str | None = None) -> str:
        """A short verdict for the report table: what to do about this task."""
        if self.runs == 0:
            return "never run"
        if self.stale(min_runs=min_runs, min_days=min_days, day=day):
            return "remove?"
        if self.unrewarding(min_runs=min_runs, min_days=min_days, day=day):
            return "nothing new"
        return "ok"

    def to_json(self) -> dict[str, object]:
        return {
            "description": self.description,
            "runs": self.runs,
            "searches": self.searches,
            "hits": self.hits,
            "stored": self.stored,
            "first_run": self.first_run,
            "last_run": self.last_run,
            "last_hit": self.last_hit,
            "last_stored": self.last_stored,
        }

    @classmethod
    def from_json(cls, key: str, data: object) -> TaskYield:
        if not isinstance(data, dict):
            return cls(key=key)

        def count(name: str) -> int:
            value = data.get(name)
            return max(int(value), 0) if isinstance(value, int) else 0

        def stamp(name: str) -> str | None:
            value = data.get(name)
            return str(value)[:10] if value else None

        description = data.get("description")
        return cls(
            key=key,
            description=str(description) if description else "",
            runs=count("runs"),
            searches=count("searches"),
            hits=count("hits"),
            stored=count("stored"),
            first_run=stamp("first_run"),
            last_run=stamp("last_run"),
            last_hit=stamp("last_hit"),
            last_stored=stamp("last_stored"),
        )


@dataclass
class _SourceHistory:
    """What is remembered about the tasks of one source."""

    tasks: dict[str, TaskYield] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {key: stat.to_json() for key, stat in sorted(self.tasks.items())}

    @classmethod
    def from_json(cls, data: object) -> _SourceHistory:
        if not isinstance(data, dict):
            return cls()
        tasks = {str(key): TaskYield.from_json(str(key), value) for key, value in data.items()}
        return cls(tasks=tasks)


class TaskHistory:
    """The task history file, loaded lazily and saved atomically.

    A file that cannot be read is a warning, never a crash: losing this costs you a
    suggestion about pruning your task list, not a run.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._sources: dict[str, _SourceHistory] = {}
        self._loaded = False

    @property
    def path(self) -> Path | None:
        return self._path

    # ── Loading & saving ────────────────────────────────────────────────

    def load(self) -> TaskHistory:
        """Read the file, treating anything unreadable as "no history yet"."""
        if self._loaded:
            return self
        self._loaded = True

        if self._path is None or not self._path.is_file():
            return self

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            get_logger().warning("Ignoring unreadable task history {}: {}", self._path, exc)
            return self

        if not isinstance(data, dict) or data.get("version") != HISTORY_VERSION:
            get_logger().warning(
                "Ignoring task history {} (version {!r}, expected {})",
                self._path,
                data.get("version") if isinstance(data, dict) else None,
                HISTORY_VERSION,
            )
            return self

        sources = data.get("sources")
        if isinstance(sources, dict):
            self._sources = {
                str(name): _SourceHistory.from_json(value) for name, value in sources.items()
            }
        return self

    def save(self) -> None:
        """Write the history file, atomically (a crash must not truncate it)."""
        if self._path is None or not self._loaded:
            return

        payload = {
            "version": HISTORY_VERSION,
            "sources": {name: history.to_json() for name, history in sorted(self._sources.items())},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._path.with_name(f"{self._path.name}.tmp")
        staging.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, self._path)

    # ── Access ──────────────────────────────────────────────────────────

    def yields(self, source: str) -> dict[str, TaskYield]:
        """Every task known for *source*, by task key."""
        self.load()
        return dict(self._sources.get(source, _SourceHistory()).tasks)

    def names(self) -> list[str]:
        """The sources with any history, in name order."""
        self.load()
        return sorted(self._sources)

    def record(
        self,
        source: str,
        key: str,
        description: str,
        *,
        searches: int,
        hits: int,
        stored: int,
        day: str | None = None,
    ) -> TaskYield:
        """Add one run of one task: what it searched, what it saw, what it brought.

        Only ever called for a task that actually ran — a search that failed before it
        produced a page is not this task's fault, and must not count against it.
        """
        self.load()
        moment = day or today()
        history = self._sources.setdefault(source, _SourceHistory())
        stat = history.tasks.setdefault(key, TaskYield(key=key, first_run=moment))
        stat.description = description or stat.description
        stat.runs += 1
        stat.searches += max(searches, 0)
        stat.hits += max(hits, 0)
        stat.stored += max(stored, 0)
        stat.first_run = stat.first_run or moment
        stat.last_run = moment
        if hits > 0:
            stat.last_hit = moment
        if stored > 0:
            stat.last_stored = moment
        return stat

    def stale(
        self,
        source: str,
        *,
        min_runs: int,
        min_days: int,
        day: str | None = None,
    ) -> list[TaskYield]:
        """The tasks of *source* that look dead, the emptiest first."""
        found = [
            stat
            for stat in self.yields(source).values()
            if stat.stale(min_runs=min_runs, min_days=min_days, day=day)
        ]
        return sorted(found, key=lambda stat: (stat.hits, stat.last_hit or "", stat.key))

    def forget(self, source: str, keys: Iterable[str]) -> int:
        """Drop the named tasks from *source*'s history; returns how many went."""
        self.load()
        history = self._sources.get(source)
        if history is None:
            return 0
        removed = [key for key in keys if history.tasks.pop(key, None) is not None]
        return len(removed)


__all__ = ["HISTORY_VERSION", "TaskHistory", "TaskYield", "configured_task_keys"]
