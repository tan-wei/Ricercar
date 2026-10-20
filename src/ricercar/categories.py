"""Category-list change detection.

The site's forum section structure (``{forum_id: russian_name}``) seldom changes, but
when it does — a section is renamed, a new one appears, or one disappears — the
pipeline should notice, report it, and stop so the user can update the config.

A snapshot of the section list is kept in :file:`data/categories.json` and checked
against the live site every run.  When they diverge the pipeline is halted and the
differences are sent through the notifier.

A separate CLI command (``ricercar categories refresh``) overwrites the snapshot with
the live list so the user can update the baseline after reviewing the changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from ricercar.log import get_logger

_DATA_FILE = Path(__file__).with_name("data") / "categories.json"
"""The bundled snapshot of rutracker's forum-section list."""


# ── Diff result ─────────────────────────────────────────────────────────


class CategoryDiff(NamedTuple):
    """Describes how the live categories differ from the bundled snapshot."""

    added: dict[int, str]
    """Sections present on the live site but missing from the snapshot."""
    removed: dict[int, str]
    """Sections present in the snapshot but missing from the live site."""
    changed: dict[int, tuple[str, str]]
    """Sections whose name changed: ``{id: (old_name, new_name)}``."""

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def describe(self) -> str:
        """Human-readable summary of the differences."""
        parts: list[str] = []
        if self.added:
            ids = ", ".join(f"{k} ({v})" for k, v in sorted(self.added.items()))
            parts.append(f"Added: {ids}")
        if self.removed:
            ids = ", ".join(f"{k} ({v})" for k, v in sorted(self.removed.items()))
            parts.append(f"Removed: {ids}")
        if self.changed:
            items = ", ".join(
                f"{k}: '{old}' → '{new}'" for k, (old, new) in sorted(self.changed.items())
            )
            parts.append(f"Renamed: {items}")
        return "; ".join(parts)


# ── Snapshot I/O ────────────────────────────────────────────────────────


def load_snapshot() -> dict[int, str]:
    """Read the bundled category snapshot from :file:`data/categories.json`."""
    import json  # noqa: PLC0415

    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        get_logger().warning("Cannot read category snapshot: {}", exc)
        return {}

    if not isinstance(raw, dict):
        get_logger().warning("Category snapshot is not a dict — ignoring")
        return {}

    from contextlib import suppress  # noqa: PLC0415

    result: dict[int, str] = {}
    for k, v in raw.items():
        with suppress(ValueError, TypeError):
            result[int(k)] = str(v)
    return result


def write_snapshot(categories: Mapping[int, str]) -> None:
    """Overwrite the bundled category snapshot with a new mapping."""
    import json  # noqa: PLC0415

    _DATA_FILE.write_text(
        json.dumps(dict(sorted(categories.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ── Comparison ──────────────────────────────────────────────────────────


def diff_categories(
    live: Mapping[int, str],
    *,
    snapshot: Mapping[int, str] | None = None,
) -> CategoryDiff:
    """Compare *live* categories against the bundled snapshot (or a custom one)."""
    base = load_snapshot() if snapshot is None else dict(snapshot)

    added = {k: v for k, v in live.items() if k not in base}
    removed = {k: v for k, v in base.items() if k not in live}
    changed = {k: (base[k], live[k]) for k in base if k in live and base[k] != live[k]}

    return CategoryDiff(added=added, removed=removed, changed=changed)


# ── Pipeline integration ────────────────────────────────────────────────


def check_categories(live: Mapping[int, str]) -> CategoryDiff | None:
    """Compare live categories with the snapshot.

    Returns the diff when changes are found, ``None`` when everything matches.
    Logs the difference at warning level when there are changes.
    """
    diff = diff_categories(live)
    if diff.has_changes:
        get_logger().warning("Category structure changed: {}", diff.describe())
        return diff
    return None


__all__ = [
    "CategoryDiff",
    "check_categories",
    "diff_categories",
    "load_snapshot",
    "write_snapshot",
]
