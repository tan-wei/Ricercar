"""Failure evidence: what gets written, where, and which traces are kept."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from ricercar.diagnostics import Recorder


class FakeTracing:
    """The ``context.tracing`` API the recorder uses, without a browser."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.starts = 0
        self.stops = 0
        self.chunks: list[str] = []

    async def start(self, **_kwargs: object) -> None:
        if self.refuse:
            raise PlaywrightError("tracing is not available for this browser")
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1

    async def start_chunk(self, *, title: str = "", **_kwargs: object) -> None:
        self.chunks.append(title)

    async def stop_chunk(self, path: Path | None = None) -> None:
        if path is not None:
            path.write_bytes(b"zip")


class FakeContext:
    def __init__(self, *, refuse: bool = False) -> None:
        self.tracing = FakeTracing(refuse=refuse)


class FakePage:
    """Just enough of a Playwright page for the recorder."""

    def __init__(self, *, url: str = "https://rutracker.org/forum/tracker.php") -> None:
        self.url = url

    async def title(self) -> str:
        return "Поиск"

    async def content(self) -> str:
        return "<html><body>page</body></html>"

    async def screenshot(self, *, path: Path, full_page: bool = False) -> None:
        assert full_page is True
        path.write_bytes(b"png")


class UnreadablePage(FakePage):
    """A page that is already gone by the time it is captured."""

    async def title(self) -> str:
        raise PlaywrightError("the page is closed")

    async def content(self) -> str:
        raise PlaywrightError("the page is closed")


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    """A recorder without tracing — plain evidence, no Playwright involved."""
    return Recorder(tmp_path / "failures", save_traces=False)


def test_capture_writes_the_page_the_screenshot_and_the_reason(recorder: Recorder) -> None:
    page = FakePage()

    failure = asyncio.run(
        recorder.capture(
            page,
            "Selectors broken: 500 rows",
            issues=("the header announces 500 result(s) but no row matches",),
            note="2 task(s) failed",
        )
    )

    directory = failure.directory
    assert directory.parent == recorder.root
    assert directory.name.endswith("_selectors-broken-500-rows")
    assert (directory / "page.html").read_text(encoding="utf-8") == "<html><body>page</body></html>"
    assert (directory / "page.png").read_bytes() == b"png"
    assert failure.trace is None
    assert recorder.failures == (failure,)
    assert len(recorder) == 1
    assert str(directory) in failure.describe()

    payload = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
    assert payload["reason"] == "Selectors broken: 500 rows"
    assert payload["url"] == page.url
    assert payload["title"] == "Поиск"
    assert payload["issues"] == ["the header announces 500 result(s) but no row matches"]
    assert payload["note"] == "2 task(s) failed"
    assert payload["trace"] is None


def test_capture_survives_a_page_that_cannot_be_read(recorder: Recorder) -> None:
    failure = asyncio.run(recorder.capture(UnreadablePage(), "timeout"))

    page = (failure.directory / "page.html").read_text(encoding="utf-8")
    assert "page.content() failed" in page
    assert failure.title == ""


# ── Tracing ───────────────────────────────────────────────────────────────


def test_a_chunk_that_ends_well_leaves_nothing_behind(tmp_path: Path) -> None:
    context = FakeContext()
    recorder = Recorder(tmp_path / "failures", save_traces=True)

    async def scenario() -> None:
        await recorder.start(context)
        async with recorder.chunk("search Bach — page 1"):
            pass
        await recorder.stop()

    asyncio.run(scenario())

    assert context.tracing.starts == 1
    assert context.tracing.chunks == ["search Bach — page 1"]
    assert recorder.failures == ()
    assert not list(recorder.root.glob("*.zip"))


def test_the_chunk_that_failed_is_the_one_that_is_kept(tmp_path: Path) -> None:
    context = FakeContext()
    recorder = Recorder(tmp_path / "failures", save_traces=True)

    async def scenario() -> None:
        await recorder.start(context)
        with pytest.raises(RuntimeError, match="boom"):
            async with recorder.chunk("search Bach — page 1"):
                raise RuntimeError("boom")
        await recorder.capture(FakePage(), "selectors")

    asyncio.run(scenario())

    (failure,) = recorder.failures
    assert failure.trace is not None
    assert failure.trace.name == "trace.zip"
    assert failure.trace.read_bytes() == b"zip"
    assert failure.trace.parent == failure.directory
    assert not list(recorder.root.glob("*.zip"))  # moved, not copied


def test_a_failure_handled_inside_a_chunk_can_still_keep_its_trace(tmp_path: Path) -> None:
    context = FakeContext()
    recorder = Recorder(tmp_path / "failures", save_traces=True)

    async def scenario() -> Path | None:
        await recorder.start(context)
        async with recorder.chunk("search Bach — page 1"):
            return await recorder.keep_chunk()

    kept = asyncio.run(scenario())

    assert kept is not None
    assert kept.read_bytes() == b"zip"
    # recording continues in a fresh chunk, so the enclosing block still had one to close
    assert context.tracing.chunks == ["search Bach — page 1", "after a kept chunk"]


def test_a_browser_that_refuses_tracing_still_runs(tmp_path: Path) -> None:
    context = FakeContext(refuse=True)
    recorder = Recorder(tmp_path / "failures", save_traces=True)

    async def scenario() -> None:
        await recorder.start(context)
        async with recorder.chunk("search Bach — page 1"):
            pass
        await recorder.stop()

    asyncio.run(scenario())

    assert context.tracing.starts == 0
    assert context.tracing.chunks == []
    assert recorder.failures == ()
