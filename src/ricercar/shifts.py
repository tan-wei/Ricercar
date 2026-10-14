"""Category shifts — tracking uploaders appearing in new sections.

A run normally searches the same categories (sections/forums) that were configured
at setup time.  When an uploader who was previously seen only in category X starts
appearing in category Y, that is a *category shift* — it may mean the uploader
moved to a different section, or the section structure changed, and the task
configuration should probably follow.

Each shift is recorded once: after it has been reported in a notification, it is
marked as ``notified`` and will not appear again.  The data is kept indefinitely
so the ``ricercar tasks --shifts`` command can still list the full history.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ricercar.log import get_logger


@dataclass
class CategoryShift:
    """One uploader appearing in a category they were not seen in before."""

    author: str
    """The uploader's name on the tracker."""
    source: str
    """Which source (e.g. ``rutracker``) the shift was observed on."""
    expected_category: int | None
    """The single category (section id) the uploader was seen in before the shift;
    ``None`` when the uploader had **no** known categories yet (the "shift" is the
    first ever sighting of that author in the context of a specific category)."""
    new_category: int
    """The category the uploader appeared in for the first time."""
    first_seen: str
    """ISO-8601 timestamp of when the shift was first observed."""
    last_seen: str
    """ISO-8601 timestamp of the most recent occurrence of this shift."""
    hits: int = 0
    """How many rows have been seen confirming this shift."""
    notified: bool = False
    """Whether a notification has already been sent for this shift."""


@dataclass
class AuthorCategories:
    """The categories an uploader has been seen in, collated per source."""

    author: str
    source: str
    categories: set[int] = field(default_factory=set)
    """Every category this uploader has been observed in so far."""
    reference_category: int | None = None
    """The "home" category by which this author is queried — typically the first
    one we saw them in, which is the configured task's category."""


class CategoryShiftTracker:
    """Tracks uploaders appearing in previously unseen categories.

    The tracker maintains two pieces of state:

    1. **Known categories** — ``(source, author) -> set[int]``, the categories
       each uploader has been observed in so far.  This is the baseline against
       which new observations are compared.

    2. **Shift records** — a list of observed shifts, each of which will be
       reported at most once in a notification.

    The file is small, human-readable JSON and written atomically.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._known: dict[str, set[int]] = {}
        """``{source\0author -> {category_id, …}}`` — all categories ever seen."""
        self._shifts: list[CategoryShift] = []
        self._loaded = False

    @staticmethod
    def _key(source: str, author: str) -> str:
        return f"{source}\0{author}"

    # ── Loading & saving ────────────────────────────────────────────────

    def load(self) -> CategoryShiftTracker:
        """Read the file; treat anything unreadable as "nothing tracked yet"."""
        if self._loaded:
            return self
        self._loaded = True

        if not self._path.is_file():
            return self

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            get_logger().warning("Ignoring unreadable category-shifts file {}: {}", self._path, exc)
            return self

        if not isinstance(data, dict):
            get_logger().warning(
                "Ignoring category-shifts file {} — expected a dict, got {!r}",
                self._path,
                type(data).__name__,
            )
            return self

        raw_known = data.get("known")
        if isinstance(raw_known, dict):
            for compound, cats in raw_known.items():
                if isinstance(cats, list):
                    self._known[compound] = {
                        int(c)
                        for c in cats
                        if isinstance(c, int) or (isinstance(c, str) and c.lstrip("-").isdigit())
                    }

        raw_shifts = data.get("shifts")
        if isinstance(raw_shifts, list):
            for item in raw_shifts:
                if not isinstance(item, dict):
                    continue
                self._shifts.append(
                    CategoryShift(
                        author=str(item.get("author", "")),
                        source=str(item.get("source", "")),
                        expected_category=item.get("expected_category"),
                        new_category=int(item["new_category"]),
                        first_seen=str(item.get("first_seen", "")),
                        last_seen=str(item.get("last_seen", "")),
                        hits=int(item.get("hits", 0)),
                        notified=bool(item.get("notified", False)),
                    )
                )

        return self

    def save(self) -> None:
        """Write atomically."""
        if not self._loaded:
            return

        payload = {
            "known": {k: sorted(v) for k, v in sorted(self._known.items())},
            "shifts": [
                {
                    "author": s.author,
                    "source": s.source,
                    "expected_category": s.expected_category,
                    "new_category": s.new_category,
                    "first_seen": s.first_seen,
                    "last_seen": s.last_seen,
                    "hits": s.hits,
                    "notified": s.notified,
                }
                for s in self._shifts
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._path.with_name(f"{self._path.name}.tmp")
        staging.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, self._path)

    # ── Recording ────────────────────────────────────────────────────────

    def observe(
        self,
        source: str,
        author: str,
        category: int,
        *,
        now: str | None = None,
    ) -> CategoryShift | None:
        """Record that *author* was seen in *category* on *source*.

        If the author was previously known only in a *different* category, a
        :class:`CategoryShift` is created (or an existing one updated) and returned.
        Returns ``None`` when there is nothing new (author already known in this
        category).

        The first ever sighting of an author in *any* category creates a baseline
        record but does **not** count as a shift (``expected_category=None`` may be
        stored but no shift is emitted).
        """
        self.load()
        key = self._key(source, author)
        ts = now or datetime.now(UTC).isoformat()
        known = self._known.get(key, set())

        if not known:
            # First sighting — set baseline, no shift.
            self._known[key] = {category}
            return None

        if category in known:
            # Already seen in this category — nothing to report.
            return None

        # New category for this author — this is a shift.
        expected = max(known) if known else None
        self._known[key].add(category)
        existing = next(
            (
                s
                for s in self._shifts
                if s.author == author and s.source == source and s.new_category == category
            ),
            None,
        )

        if existing is not None:
            existing.last_seen = ts
            existing.hits += 1
            return existing

        shift = CategoryShift(
            author=author,
            source=source,
            expected_category=expected,
            new_category=category,
            first_seen=ts,
            last_seen=ts,
            hits=1,
            notified=False,
        )
        self._shifts.append(shift)
        return shift

    # ── Query ────────────────────────────────────────────────────────────

    def unread(self, source: str | None = None) -> list[CategoryShift]:
        """Shifts that have not been notified yet, sorted by newest first.

        If *source* is given, only those from that source are returned.
        """
        result = [s for s in self._shifts if not s.notified]
        if source:
            result = [s for s in result if s.source == source]
        return sorted(result, key=lambda s: (s.first_seen, s.author), reverse=True)

    def all(self, source: str | None = None) -> list[CategoryShift]:
        """Every known shift, sorted by newest first.

        If *source* is given, only those from that source are returned.
        """
        result = list(self._shifts)
        if source:
            result = [s for s in result if s.source == source]
        return sorted(result, key=lambda s: (s.first_seen, s.author), reverse=True)

    def mark_notified(self, source: str, author: str, new_category: int) -> bool:
        """Mark one shift as notified.  Returns ``True`` if found."""
        for s in self._shifts:
            if s.author == author and s.source == source and s.new_category == new_category:
                s.notified = True
                return True
        return False

    def mark_all_notified(self, source: str | None = None) -> int:
        """Mark every shift as notified.  Returns the number marked."""
        count = 0
        for s in self._shifts:
            if (source is None or s.source == source) and not s.notified:
                s.notified = True
                count += 1
        return count

    def known_categories(self, source: str, author: str) -> set[int]:
        """The categories *author* has ever been observed in on *source*."""
        self.load()
        return set(self._known.get(self._key(source, author), set()))

    def author_count(self) -> int:
        """How many distinct ``(source, author)`` pairs are being tracked."""
        self.load()
        return len(self._known)

    def shift_count(self) -> int:
        """How many shifts have been recorded in total."""
        self.load()
        return len(self._shifts)


__all__ = [
    "AuthorCategories",
    "CategoryShift",
    "CategoryShiftTracker",
]
