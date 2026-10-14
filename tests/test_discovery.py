"""Discovered uploaders: recording, querying, and marking as confirmed."""

from __future__ import annotations

from pathlib import Path

import pytest

from ricercar.discovery import DiscoveredUploaders


@pytest.fixture
def file(tmp_path: Path) -> Path:
    return tmp_path / "discovered_uploaders.json"


@pytest.fixture
def target(file: Path) -> DiscoveredUploaders:
    return DiscoveredUploaders(file)


# ── Recording ────────────────────────────────────────────────────────────────


def test_record_new_uploader(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "SomeUploader", section=794, now="2025-01-15T10:00:00")
    assert target.total == 1
    u = target.unconfirmed()[0]
    assert u.author == "SomeUploader"
    assert u.source == "rutracker"
    assert u.sections_seen == [794]
    assert u.hits == 1
    assert u.confirmed is False


def test_record_existing_uploader_updates(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "UploaderA", section=794, now="2025-01-15T10:00:00")
    target.record("rutracker", "UploaderA", section=557, now="2025-01-16T12:00:00")
    assert target.total == 1
    u = target.unconfirmed()[0]
    assert u.hits == 2
    assert sorted(u.sections_seen) == [557, 794]
    assert u.last_seen == "2025-01-16T12:00:00"
    # first_seen should not have changed
    assert u.first_seen == "2025-01-15T10:00:00"


def test_record_different_sources_are_independent(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "AuthorX", now="2025-01-15T10:00:00")
    target.record("other", "AuthorX", now="2025-01-16T12:00:00")
    assert target.total == 2
    rut = [u for u in target.unconfirmed() if u.source == "rutracker"]
    oth = [u for u in target.unconfirmed() if u.source == "other"]
    assert len(rut) == 1
    assert len(oth) == 1


# ── Persistence ─────────────────────────────────────────────────────────────


def test_save_and_load(file: Path) -> None:
    d1 = DiscoveredUploaders(file)
    d1.record("rutracker", "Alpha", section=794, now="2025-01-15T10:00:00")
    d1.record("rutracker", "Beta", section=557, now="2025-01-16T12:00:00")
    d1.mark_confirmed("rutracker", "Alpha")
    d1.save()

    d2 = DiscoveredUploaders(file)
    d2.load()
    assert d2.total == 2
    confirmed = [u for u in d2.unconfirmed() if u.confirmed]
    assert len(confirmed) == 0  # Alpha is confirmed, so not in unconfirmed
    unconfirmed = d2.unconfirmed()
    assert len(unconfirmed) == 1
    assert unconfirmed[0].author == "Beta"


def test_load_corrupted_file(file: Path) -> None:
    file.write_text("not valid json", encoding="utf-8")
    d = DiscoveredUploaders(file)
    d.load()  # should not raise
    assert d.total == 0


def test_load_non_list_json(file: Path) -> None:
    file.write_text('{"foo": "bar"}', encoding="utf-8")
    d = DiscoveredUploaders(file)
    d.load()
    assert d.total == 0


def test_atomic_write_does_not_truncate(file: Path) -> None:
    """A crash during save should not truncate the file."""
    d = DiscoveredUploaders(file)
    d.record("rutracker", "Survivor", now="2025-01-15T10:00:00")
    d.save()
    content = file.read_text(encoding="utf-8")
    assert "Survivor" in content


# ── Query ───────────────────────────────────────────────────────────────────


def test_unconfirmed_empty_when_all_confirmed(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "A", now="2025-01-15T10:00:00")
    target.record("rutracker", "B", now="2025-01-15T10:00:00")
    target.mark_all_confirmed()
    assert target.unconfirmed() == []


def test_unconfirmed_filtered_by_source(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "A", now="2025-01-15T10:00:00")
    target.record("other", "B", now="2025-01-15T10:00:00")
    assert len(target.unconfirmed(source="rutracker")) == 1
    assert len(target.unconfirmed(source="other")) == 1


def test_mark_confirmed_returns_true(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "A", now="2025-01-15T10:00:00")
    assert target.mark_confirmed("rutracker", "A") is True
    assert target.unconfirmed() == []


def test_mark_confirmed_unknown_returns_false(target: DiscoveredUploaders) -> None:
    assert target.mark_confirmed("rutracker", "Nobody") is False


def test_mark_all_confirmed_returns_count(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "A", now="2025-01-15T10:00:00")
    target.record("rutracker", "B", now="2025-01-15T10:00:00")
    assert target.mark_all_confirmed() == 2
    assert target.unconfirmed() == []


def test_mark_all_confirmed_by_source(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "A", now="2025-01-15T10:00:00")
    target.record("other", "B", now="2025-01-15T10:00:00")
    assert target.mark_all_confirmed(source="rutracker") == 1
    # "B" from "other" should still be unconfirmed
    assert len(target.unconfirmed()) == 1


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_no_file_yet(target: DiscoveredUploaders) -> None:
    """Loading a non-existent file is not an error."""
    target.load()
    assert target.total == 0


def test_empty_file(target: DiscoveredUploaders) -> None:
    """Empty discovered list is not an error."""
    target.save()
    target.load()
    assert target.total == 0


def test_unconfirmed_sorted_by_hits(target: DiscoveredUploaders) -> None:
    target.record("rutracker", "Low", section=794, now="2025-01-15T10:00:00")
    target.record("rutracker", "High", section=557, now="2025-01-15T10:00:00")
    target.record("rutracker", "High", section=557, now="2025-01-15T10:00:00")
    target.record("rutracker", "High", section=557, now="2025-01-15T10:00:00")
    result = target.unconfirmed()
    assert result[0].author == "High"
    assert result[1].author == "Low"


def test_record_no_section(target: DiscoveredUploaders) -> None:
    """Section-less tasks should still be recorded."""
    target.record("rutracker", "NoSection", now="2025-01-15T10:00:00")
    u = target.unconfirmed()[0]
    assert u.sections_seen == []
    assert u.hits == 1
