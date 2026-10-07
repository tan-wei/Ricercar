"""Evidence for the failures you cannot debug from a log line.

A selector that stops matching is the one failure that cannot be fixed without
seeing the page — the site changed, and the code has to change with it. So a
failure writes what the browser actually had in front of it into
``diagnostics.failures_dir/<timestamp>_<reason>/``:

* ``page.html`` — the document, as parsed;
* ``page.png`` — a full-page screenshot;
* ``trace.zip`` — the Playwright trace of the step that failed, when tracing is on
  (open it with ``playwright show-trace <dir>/trace.zip``);
* ``failure.json`` — URL, title, the reason, the issues the source reported, and
  anything the caller wanted to add.

Traces are recorded in *chunks*: a chunk that ends well is discarded, and only the
chunk containing a failure is kept. A long healthy run therefore does not pile up
trace files nobody will ever open.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from ricercar.log import get_logger

TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Failure:
    """One saved failure: what went wrong, and where the evidence is."""

    reason: str
    directory: Path
    url: str
    title: str
    issues: tuple[str, ...]
    note: str
    trace: Path | None

    def describe(self) -> str:
        detail = f" ({'; '.join(self.issues)})" if self.issues else ""
        return f"{self.reason} on {self.url}{detail} — evidence in {self.directory}"


class Recorder:
    """Collects failures and writes them to disk.

    Used as a collaborator by the pipeline: :meth:`chunk` wraps a step whose trace
    is only kept when it fails, and :meth:`capture` writes the evidence.
    """

    def __init__(
        self,
        root: Path,
        *,
        save_traces: bool = True,
        save_screenshots: bool = True,
    ) -> None:
        self._root = root
        self._want_traces = save_traces
        self._want_screenshots = save_screenshots
        self._context: BrowserContext | None = None
        self._tracing = False
        self._in_chunk = False
        self._pending: Path | None = None
        self._failures: list[Failure] = []

    # ── Traces ──────────────────────────────────────────────────────────

    async def start(self, context: BrowserContext) -> None:
        """Begin recording a trace for this context, if tracing is wanted.

        A browser Playwright merely attached to may refuse; that is a warning, not a
        reason to abort a run.
        """
        self._context = context
        if not self._want_traces:
            return
        try:
            await context.tracing.start(screenshots=True, snapshots=True)
        except PlaywrightError as exc:
            get_logger().warning("Tracing is unavailable for this browser: {}", exc)
            self._want_traces = False
            return
        self._tracing = True

    async def stop(self) -> None:
        """Stop recording (the trace itself is not kept)."""
        self._discard_pending()
        if self._tracing and self._context is not None:
            try:
                await self._context.tracing.stop()
            except PlaywrightError as exc:  # pragma: no cover - best effort
                get_logger().debug("Stopping the trace failed: {}", exc)
        self._tracing = False
        self._in_chunk = False

    @asynccontextmanager
    async def chunk(self, title: str) -> AsyncIterator[None]:
        """Record *title* into a trace chunk that is kept only if the block fails.

        Yields immediately when tracing is off, so callers need no special case.
        """
        if not self._tracing or self._context is None:
            yield
            return

        await self._context.tracing.start_chunk(title=title)
        self._in_chunk = True
        try:
            yield
        except BaseException:
            if self._pending is None:
                self._pending = self._new_path()
                await self._context.tracing.stop_chunk(path=self._pending)
            else:
                # Something inside already kept its own chunk (see keep_chunk).
                await self._context.tracing.stop_chunk()
            raise
        else:
            # Nothing went wrong here: drop the chunk instead of accumulating traces.
            await self._context.tracing.stop_chunk()
        finally:
            self._in_chunk = False

    async def keep_chunk(self) -> Path | None:
        """Keep the trace of the chunk being recorded right now.

        A caller that handles a failure *inside* a chunk (a download it decided to
        skip, say) would otherwise see the evidence thrown away by the enclosing
        :meth:`chunk`. Recording continues in a fresh chunk, so the enclosing block
        still has one to close, and only what happened up to the failure is kept.
        """
        if not self._tracing or self._context is None or not self._in_chunk:
            return None

        path = self._new_path()
        await self._context.tracing.stop_chunk(path=path)
        await self._context.tracing.start_chunk(title="after a kept chunk")
        self._pending = self._pending or path
        return path

    # ── Evidence ────────────────────────────────────────────────────────

    async def capture(
        self,
        page: Page,
        reason: str,
        *,
        issues: Sequence[str] = (),
        note: str = "",
        when: datetime | None = None,
    ) -> Failure:
        """Save what the page looks like now, and return where it went."""
        log = get_logger()
        await self.keep_chunk()
        moment = when or datetime.now()
        directory = self._root / f"{moment.strftime(TIMESTAMP_FORMAT)}_{_slug(reason)}"
        directory.mkdir(parents=True, exist_ok=True)

        url = page.url
        title = ""
        try:
            title = (await page.title()).strip()
        except PlaywrightError as exc:
            log.debug("Could not read the page title: {}", exc)

        try:
            html = await page.content()
        except PlaywrightError as exc:
            html = f"<!-- page.content() failed: {exc} -->"
        (directory / "page.html").write_text(html, encoding="utf-8")

        if self._want_screenshots:
            try:
                await page.screenshot(path=directory / "page.png", full_page=True)
            except PlaywrightError as exc:
                log.debug("Could not screenshot the page: {}", exc)

        trace = self._move_pending(directory)
        failure = Failure(
            reason=reason,
            directory=directory,
            url=url,
            title=title,
            issues=tuple(issues),
            note=note,
            trace=trace,
        )
        (directory / "failure.json").write_text(
            json.dumps(
                {
                    "when": moment.isoformat(timespec="seconds"),
                    "reason": reason,
                    "url": url,
                    "title": title,
                    "issues": list(issues),
                    "note": note,
                    "trace": trace.name if trace else None,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        self._failures.append(failure)
        log.warning("Saved the failing page: {}", failure.describe())
        return failure

    # ── Inspection ──────────────────────────────────────────────────────

    @property
    def failures(self) -> tuple[Failure, ...]:
        """Everything captured so far, oldest first."""
        return tuple(self._failures)

    def __len__(self) -> int:
        return len(self._failures)

    @property
    def root(self) -> Path:
        return self._root

    # ── Internals ───────────────────────────────────────────────────────

    def _new_path(self) -> Path:
        """A place for a trace chunk that is being kept."""
        path = self._root / f"{uuid.uuid4().hex}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _move_pending(self, directory: Path) -> Path | None:
        """Move the trace of a failed chunk into its failure directory."""
        pending = self._pending
        self._pending = None
        if pending is None or not pending.is_file():
            return None
        destination = directory / "trace.zip"
        pending.replace(destination)
        return destination

    def _discard_pending(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None:
            pending.unlink(missing_ok=True)


def _slug(reason: str) -> str:
    """A short, filesystem-safe form of *reason*."""
    slug = _SLUG.sub("-", reason.lower()).strip("-")
    return slug[:40] or "failure"


__all__ = ["Failure", "Recorder"]
