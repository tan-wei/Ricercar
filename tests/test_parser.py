"""Torrent parsing: what it accepts, what it refuses, and what it only warns about."""

from __future__ import annotations

import hashlib
from pathlib import Path

import bencode
import pytest

from ricercar.parser import PIECES_HASH_LENGTH, TorrentError, parse, parse_file

PIECE_LENGTH = 25

# Real piece data is a SHA-1 hash, so it is almost never valid UTF-8 — and bencode.py
# hands byte strings back as ``str`` when they happen to be, which would make the piece
# count unverifiable. Byte 0xFF keeps the synthetic torrents in the realistic case.
PIECES = b"\xff" * PIECES_HASH_LENGTH


def _torrent(info: dict[str, object]) -> bytes:
    """Bencode a minimal torrent around *info*."""
    payload = bencode.bencode({"info": info})
    assert isinstance(payload, bytes)
    return payload


@pytest.fixture
def single_file() -> bytes:
    """A 50-byte payload in two pieces, so the piece count adds up."""
    return _torrent(
        {
            "name": "track.mp3",
            "length": 50,
            "pieces": PIECES * 2,
            "piece length": PIECE_LENGTH,
        }
    )


def test_a_real_torrent_parses_cleanly(sample_torrent: bytes) -> None:
    meta = parse(sample_torrent)

    assert meta.name == "01 - Track 1.rmj.MP3"
    assert meta.size == 81_248_640
    assert meta.file_count == 1
    assert meta.md5 == hashlib.md5(sample_torrent).hexdigest()
    assert meta.url.startswith("https://rutracker.org/forum/viewtopic.php?t=")
    assert meta.issues == ()


def test_a_single_file_torrent_counts_as_one_file(single_file: bytes) -> None:
    meta = parse(single_file)

    assert meta.name == "track.mp3"
    assert meta.size == 50
    assert meta.file_count == 1
    assert meta.issues == ()


def test_a_multi_file_torrent_adds_up_its_files() -> None:
    meta = parse(
        _torrent(
            {
                "name": "album",
                "pieces": PIECES,
                "piece length": 100,
                "files": [
                    {"length": 30, "path": ["a.mp3"]},
                    {"length": 20, "path": ["b.mp3"]},
                ],
            }
        )
    )

    assert meta.size == 50
    assert meta.file_count == 2
    assert meta.issues == ()


def test_a_torrent_can_be_read_from_disk(tmp_path: Path, single_file: bytes) -> None:
    path = tmp_path / "sample.torrent"
    path.write_bytes(single_file)

    assert parse_file(path).name == "track.mp3"


def test_bytes_that_are_not_bencode_are_refused() -> None:
    with pytest.raises(TorrentError, match="bencode"):
        parse(b"this is not a torrent")


def test_a_top_level_list_is_refused() -> None:
    payload = bencode.bencode([1, 2])
    assert isinstance(payload, bytes)

    with pytest.raises(TorrentError, match="dictionary"):
        parse(payload)


def test_a_torrent_without_info_is_refused() -> None:
    payload = bencode.bencode({"announce": "http://tracker.example/announce"})
    assert isinstance(payload, bytes)

    with pytest.raises(TorrentError, match="'info'"):
        parse(payload)


def test_a_torrent_without_a_name_is_refused() -> None:
    with pytest.raises(TorrentError, match="info.name"):
        parse(_torrent({"length": 10, "pieces": PIECES}))


def test_a_torrent_without_a_payload_is_refused() -> None:
    with pytest.raises(TorrentError, match="info.files"):
        parse(_torrent({"name": "nothing", "pieces": PIECES}))


def test_a_piece_count_that_contradicts_the_size_is_reported() -> None:
    # 50 bytes in 25-byte pieces needs two hashes; only one is given.
    meta = parse(_torrent({"name": "short", "length": 50, "pieces": PIECES, "piece length": 25}))

    assert len(meta.issues) == 1
    assert "piece hash" in meta.issues[0]


def test_a_non_positive_size_is_reported() -> None:
    meta = parse(_torrent({"name": "empty", "length": 0, "pieces": b"", "piece length": 25}))

    assert any("non-positive" in issue for issue in meta.issues)


def test_pieces_that_are_not_binary_are_reported() -> None:
    meta = parse(
        _torrent({"name": "text-pieces", "length": 25, "pieces": "not bytes", "piece length": 25})
    )

    assert any("not binary" in issue for issue in meta.issues)


def test_a_missing_piece_length_is_reported() -> None:
    meta = parse(_torrent({"name": "no-piece-length", "length": 25, "pieces": PIECES}))

    assert any("piece length" in issue for issue in meta.issues)
