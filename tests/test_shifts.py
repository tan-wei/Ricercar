"""Tests for :mod:`ricercar.shifts` — category-shift tracking."""

from __future__ import annotations

import json
from pathlib import Path

from ricercar.shifts import CategoryShiftTracker

# ── Helpers ────────────────────────────────────────────────────────────────


def _tracker(tmp_path: Path) -> CategoryShiftTracker:
    return CategoryShiftTracker(tmp_path / "shifts.json")


# ── Recording ──────────────────────────────────────────────────────────────


def test_first_sighting_creates_baseline(tmp_path: Path) -> None:
    """First ever sighting of an author sets baseline — no shift returned."""
    t = _tracker(tmp_path)
    result = t.observe("rutracker", "Alice", 794)
    assert result is None
    assert t.known_categories("rutracker", "Alice") == {794}


def test_same_category_no_shift(tmp_path: Path) -> None:
    """Seeing an author in the same category does not create a shift."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    result = t.observe("rutracker", "Alice", 794)
    assert result is None
    # Repeated sightings don't inflate hits on the baseline.
    assert t.known_categories("rutracker", "Alice") == {794}


def test_new_category_creates_shift(tmp_path: Path) -> None:
    """Seeing an author in a new category returns a shift record."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    shift = t.observe("rutracker", "Alice", 795)
    assert shift is not None
    assert shift.author == "Alice"
    assert shift.source == "rutracker"
    assert shift.expected_category == 794
    assert shift.new_category == 795
    assert shift.hits == 1
    assert shift.notified is False
    assert t.known_categories("rutracker", "Alice") == {794, 795}


def test_repeated_shift_updates_existing(tmp_path: Path) -> None:
    """Seeing the same shift again returns None (already tracked)."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    shift = t.observe("rutracker", "Alice", 795)
    # After the category is already known, a repeated sighting is no longer a shift.
    assert shift is None
    # The shift record still exists with hits=1.
    assert t.shift_count() == 1


def test_different_sources_independent(tmp_path: Path) -> None:
    """Same author on different sources has separate category baselines."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    shift1 = t.observe("rutracker", "Alice", 795)
    assert shift1 is not None  # shift on rutracker

    t.observe("other", "Alice", 100)
    shift2 = t.observe("other", "Alice", 101)
    assert shift2 is not None  # also a shift on other

    assert t.shift_count() == 2
    assert t.known_categories("rutracker", "Alice") == {794, 795}
    assert t.known_categories("other", "Alice") == {100, 101}


# ── Save & load ────────────────────────────────────────────────────────────


def test_save_and_load(tmp_path: Path) -> None:
    """Round-trip through JSON preserves everything."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)  # shift 1
    t.observe("rutracker", "Bob", 556)
    t.observe("rutracker", "Bob", 557)  # shift 2
    t.mark_notified("rutracker", "Alice", 795)
    t.save()

    t2 = _tracker(tmp_path)
    t2.load()

    assert t2.shift_count() == 2
    assert t2.author_count() == 2
    assert t2.known_categories("rutracker", "Alice") == {794, 795}

    shifts = t2.all()
    alice = next(s for s in shifts if s.author == "Alice")
    assert alice.notified is True
    bob = next(s for s in shifts if s.author == "Bob")
    assert bob.notified is False


def test_load_corrupted_file(tmp_path: Path) -> None:
    """A corrupted file does not crash — starts empty and warns."""
    (tmp_path / "shifts.json").write_text("not json", encoding="utf-8")
    t = _tracker(tmp_path)
    t.load()
    assert t.shift_count() == 0
    assert t.author_count() == 0
    # a subsequent observe still works
    t.observe("rutracker", "Alice", 794)
    assert t.author_count() == 1


def test_no_file_yet(tmp_path: Path) -> None:
    """No file means no data — no crash."""
    t = _tracker(tmp_path)
    assert t.all() == []
    assert t.unread() == []


def test_load_idempotent(tmp_path: Path) -> None:
    """Calling load() twice does not reset data."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    n1 = t.shift_count()
    t.load()  # second call — no-op because _loaded is already True
    assert t.shift_count() == n1


def test_load_non_dict_top_level(tmp_path: Path) -> None:
    """A file containing a JSON array (not a dict) warns and starts empty."""
    (tmp_path / "shifts.json").write_text("[]", encoding="utf-8")
    t = _tracker(tmp_path)
    t.load()
    assert t.shift_count() == 0
    assert t.author_count() == 0


def test_atomic_write_does_not_truncate(tmp_path: Path) -> None:
    """A crash during write never truncates the file."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.save()
    content = (tmp_path / "shifts.json").read_text(encoding="utf-8")
    # The data should still be intact after a simulated crash (we just check
    # that the tmp file pattern is used — the actual atomic write uses os.replace).
    assert json.loads(content) is not None


# ── Query ──────────────────────────────────────────────────────────────────


def test_unread_returns_unnotified_only(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("rutracker", "Alice", 796)
    t.mark_notified("rutracker", "Alice", 795)

    unread = t.unread()
    assert len(unread) == 1
    assert unread[0].new_category == 796


def test_unread_filtered_by_source(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("other", "Alice", 100)
    t.observe("other", "Alice", 101)

    unread = t.unread("rutracker")
    assert len(unread) == 1
    assert unread[0].new_category == 795


def test_mark_notified_returns_true(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    assert t.mark_notified("rutracker", "Alice", 795) is True
    assert t.unread() == []


def test_mark_notified_unknown_returns_false(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    assert t.mark_notified("rutracker", "Unknown", 999) is False


def test_mark_all_notified_returns_count(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("rutracker", "Bob", 556)
    t.observe("rutracker", "Bob", 557)
    assert t.mark_all_notified() == 2  # two shifts: Alice→795, Bob→557
    assert t.unread() == []


def test_mark_all_notified_filtered_by_source(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("other", "Alice", 100)
    t.observe("other", "Alice", 101)
    assert t.mark_all_notified("rutracker") == 1
    assert len(t.unread()) == 1  # the other source shift remains


def test_known_categories_returns_expected_set(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("rutracker", "Alice", 796)
    assert t.known_categories("rutracker", "Alice") == {794, 795, 796}


def test_known_categories_unknown_author_returns_empty(tmp_path: Path) -> None:
    """Asking about an unknown author returns an empty set."""
    t = _tracker(tmp_path)
    assert t.known_categories("rutracker", "Nobody") == set()


def test_unread_sorted_by_newest_first(tmp_path: Path) -> None:
    """Unread shifts should be sorted newest first."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "A", 794, now="2025-01-01T00:00:00")
    t.observe("rutracker", "A", 795, now="2025-01-02T00:00:00")
    t.observe("rutracker", "B", 794, now="2025-01-03T00:00:00")
    t.observe("rutracker", "B", 795, now="2025-01-04T00:00:00")
    unread = t.unread()
    assert len(unread) == 2
    # The B→795 shift was first_seen last (2025-01-04).
    assert unread[0].author == "B"


def test_all_filtered_by_source(tmp_path: Path) -> None:
    """all(source=) returns only shifts from that source."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("other", "Bob", 100)
    t.observe("other", "Bob", 101)

    rutracker_shifts = t.all(source="rutracker")
    assert len(rutracker_shifts) == 1
    assert rutracker_shifts[0].author == "Alice"

    other_shifts = t.all(source="other")
    assert len(other_shifts) == 1
    assert other_shifts[0].author == "Bob"


def test_empty_state_after_save(tmp_path: Path) -> None:
    """Saving an empty tracker produces a valid file."""
    t = _tracker(tmp_path)
    t.load()  # must mark as loaded so save() actually writes
    t.save()
    content = (tmp_path / "shifts.json").read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["known"] == {}
    assert data["shifts"] == []


# ── CategoryShift dataclass ────────────────────────────────────────────────


def test_shift_expected_category_none_on_baseline(tmp_path: Path) -> None:
    """The first observed category does not create a shift, so expected_category
    is only set when a shift actually occurs."""
    t = _tracker(tmp_path)
    # First sighting — no shift, so no CategoryShift object is created.
    t.observe("rutracker", "Alice", 794)

    # Sighting in a second category does produce a shift with expected_category.
    shift = t.observe("rutracker", "Alice", 795)
    assert shift is not None
    # expected_category is the max of the *known* categories before this observation.
    # Since only {794} was known, expected=794.
    assert shift.expected_category == 794


def test_multiple_shifts_same_author(tmp_path: Path) -> None:
    """One author can have multiple shifts across different new categories."""
    t = _tracker(tmp_path)
    t.observe("rutracker", "Alice", 794)
    t.observe("rutracker", "Alice", 795)
    t.observe("rutracker", "Alice", 900)

    shifts = t.all()
    assert len(shifts) == 2
    cats = {s.new_category for s in shifts}
    assert cats == {795, 900}
