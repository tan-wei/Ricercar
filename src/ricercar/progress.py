"""Progress display, and the console the log is written through.

Carried over from the legacy tool's tqdm bars, with the same two habits: long
loops show where they are, and log records go through the *same* console so they
appear above the bar instead of tearing it apart (in the legacy code that was
``logger.add(lambda msg: tqdm.write(msg, end=""))``; here it is
:data:`CONSOLE` — see :func:`ricercar.log.configure_logging`).

Everything is on stderr, like the bars it replaces, so piping stdout stays clean.

**The rule:** every step whose duration is not obviously negligible gets a task
here — a bar when the amount of work is known up front, a spinner (``total=None``)
when it is not. That is why the migration counts rows per batch, and why the
search, download and notification steps report as they go.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

CONSOLE = Console(stderr=True)
"""Shared stderr console — the progress bars and the log both write through it."""


def new_progress() -> Progress:
    """A :class:`rich.progress.Progress` laid out the way this project shows work.

    One per run, with a task per loop::

        with new_progress() as progress:
            pages = progress.add_task("Searching", total=3)
            progress.update(pages, advance=1)

    ``total=None`` gives an indeterminate spinner, for steps whose size is only
    known once they are over. Create it once and pass it down: rich allows a
    single live display per console, so nesting two :class:`Progress` objects on
    the same console would raise.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=CONSOLE,
    )


@contextmanager
def phase(
    progress: Progress | None,
    description: str,
    total: int | None = None,
) -> Iterator[TaskID | None]:
    """Show one phase of an operation as a progress task, and drop it when done.

    Yields the task id (or ``None`` when there is no ``Progress``, so a library
    function can offer progress without requiring it). The task is removed rather
    than left completed because phases run one after another — on screen only the
    current one is interesting.

    Usage::

        with phase(progress, "Importing .torrent files", rows) as task:
            copy_batch(..., progress=progress, task=task)
    """
    if progress is None:
        yield None
        return

    task = progress.add_task(description, total=total)
    try:
        yield task
    finally:
        progress.remove_task(task)


__all__ = ["CONSOLE", "new_progress", "phase"]
