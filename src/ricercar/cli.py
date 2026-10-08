"""Rich CLI entrypoint.

``ricercar`` on its own runs on a schedule (``schedule:`` in the config); ``--once``
runs a single cycle, which is what the README's examples use.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ricercar import __version__
from ricercar.config import Settings, get_settings
from ricercar.log import configure_logging, get_logger
from ricercar.repository import DatabaseSchemaError, check_database

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from ricercar.history import TaskHistory, TaskYield

INTERRUPTED_EXIT_CODE = 130
"""What a clean interrupt reports — the shell's convention for Ctrl-C."""

REVIEW_ROWS = 30
"""How many tasks `ricercar tasks` prints before it says how many it left out.

The task list can be thousands of entries long; the interesting rows are at the top, and
``--all`` (or ``--json``) is there when you want the rest.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ricercar",
        description="Ricercar — torrent monitoring & downloading tool",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        type=str,
        action="append",
        default=None,
        metavar="FILE",
        help=(
            "Path to an additional YAML config file, merged on top of the defaults; "
            "repeat the flag to layer several (later files win)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load config & log, then exit without doing real work",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (do not schedule)",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Only run this configured source (default: all of them)",
    )
    parser.add_argument(
        "--offline",
        nargs="?",
        const="tests/fixtures",
        default=None,
        metavar="DIR",
        help=(
            "Serve the source's pages from local fixtures instead of the network "
            "(default directory: tests/fixtures)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Store at most N torrents this run (a quick check)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Redo the tasks already completed today instead of skipping them",
    )

    sub = parser.add_subparsers(dest="command")

    # ── `show-config` ───────────────────────────────────────────────────
    show = sub.add_parser("show-config", help="Print the resolved configuration")
    show.add_argument("--json", action="store_true", help="Output as JSON")

    # ── `notify-test` ───────────────────────────────────────────────────
    sub.add_parser(
        "notify-test",
        help="Send a test notification to the configured URLs and exit",
    )

    # ── `tasks` ─────────────────────────────────────────────────────────
    tasks = sub.add_parser(
        "tasks",
        help="Show what each configured task has yielded and which look dead",
    )
    tasks.add_argument(
        "--source",
        dest="task_source",
        default=None,
        help="Only this configured source (same as the global --source)",
    )
    tasks.add_argument(
        "--stale",
        action="store_true",
        help="Only the tasks worth removing — nothing found within run.stale_task_days",
    )
    tasks.add_argument(
        "--all",
        action="store_true",
        help=f"List every task instead of the worst {REVIEW_ROWS}",
    )
    tasks.add_argument(
        "--prune",
        action="store_true",
        help="Drop the history of tasks that are no longer in the config",
    )
    tasks.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def cli(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ── Bootstrap ───────────────────────────────────────────────────────
    if args.config:
        from ricercar.config import set_extra_config_files

        set_extra_config_files(args.config)

    settings = get_settings()
    configure_logging(settings.log)
    logger = get_logger()

    logger.info("Ricercar v{} starting", __version__)
    logger.debug("Configured sources: {}", ", ".join(settings.sources) or "none")
    if not settings.sources:
        logger.warning(
            "No source is configured — add a `sources:` section, "
            "see config/config.yml for an example."
        )

    if args.dry_run and args.command in ("show-config", "notify-test", "tasks"):
        parser.error(f"--dry-run cannot be combined with `{args.command}`")

    # ── Subcommands ─────────────────────────────────────────────────────
    if args.command == "show-config":
        _show_config(args.json)
        return

    if args.command == "notify-test":
        raise SystemExit(_notify_test())

    if args.command == "tasks":
        raise SystemExit(_tasks_report(args))

    # A database from the previous era must not be written into by accident.
    _require_usable_database(settings)

    if args.dry_run:
        logger.info("Dry-run mode — exiting")
        return

    # ── Run ─────────────────────────────────────────────────────────────
    try:
        code = _run_once(args) if args.once else _run_scheduled(args)
    finally:
        # The browser was only borrowed for the run: leave nothing behind, whatever way
        # the run ended. Closing is graceful, so the signed-in profile — and its cookies —
        # survives for next time.
        _close_browsers(settings)
    raise SystemExit(code)


# ── Guards ────────────────────────────────────────────────────────────────


def _require_usable_database(settings: Settings) -> None:
    """Stop early, with an actionable hint, when the database needs migrating.

    A legacy database has to be imported (see ``ricercar.repository.migrate``);
    opening it as if it were current would add the new constraints to rows that were
    never checked against them.
    """
    try:
        check_database(settings.db_path)
    except DatabaseSchemaError as exc:
        get_logger().error("{}", exc)
        raise SystemExit(1) from exc


def _close_browsers(settings: Settings) -> None:
    """Close the browsers we attached to, so nothing is left running behind us.

    One endpoint per configured source, deduplicated: two trackers can share a browser.
    A browser that was never running is not an error — there is simply nothing to close.
    """
    from ricercar.browser import close_attached
    from ricercar.sources import UnknownSourceError, get_source

    if not settings.browser.close_on_exit:
        get_logger().debug("browser.close_on_exit is off — leaving the browser running")
        return

    urls = set()
    for name in settings.sources:
        try:
            urls.add(get_source(name, settings).settings.connect_url)
        except UnknownSourceError as exc:  # pragma: no cover - config validation catches it
            get_logger().debug("Not closing a browser for {}: {}", name, exc)
    for url in sorted(urls):
        asyncio.run(close_attached(url))


# ── Subcommands ───────────────────────────────────────────────────────────


def _show_config(as_json: bool = False) -> None:
    settings = get_settings()
    if as_json:
        import json

        json.dump(settings.model_dump(mode="json"), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        from rich import print as rprint

        rprint(settings.model_dump(mode="json"))


def _notify_test() -> int:
    from ricercar.notify import Notifier
    from ricercar.progress import CONSOLE

    logger = get_logger()
    notifier = Notifier.from_config()
    if not notifier.enabled:
        logger.warning(
            "No notification URL is configured — add `notify.urls` in "
            "config/config.yml (see the apprise link there for the URL schemes)."
        )
        return 1

    with CONSOLE.status("Sending the notification…", spinner="dots"):
        delivered = asyncio.run(
            notifier.send(
                f"Ricercar {__version__} test notification",
                "If you are reading this, notifications are configured correctly.",
            )
        )
    return 0 if delivered else 1


def _configured_keys(source: str, settings: Settings) -> set[str]:
    """The task keys *source*'s config lists right now, whether or not they have run."""
    from ricercar.history import configured_task_keys
    from ricercar.sources import UnknownSourceError, get_source

    try:
        source_settings = get_source(source, settings).settings
    except UnknownSourceError:
        return set()
    return configured_task_keys(source_settings)


_VERDICT_ORDER = {"remove?": 0, "nothing new": 1, "ok": 2, "never run": 3}
"""How the report sorts verdicts: what to remove first, what was never sampled last."""


def _select_tasks(
    history: TaskHistory,
    source: str,
    *,
    min_runs: int,
    min_days: int,
    stale_only: bool = False,
    day: str | None = None,
) -> list[TaskYield]:
    """The tasks of *source*, the emptiest first — in the order the report shows them.

    A task the config no longer lists is still shown (its numbers are a leftover worth
    seeing), which is why this takes history rather than the config.
    """
    verdicts = {
        key: stat.verdict(min_runs=min_runs, min_days=min_days, day=day)
        for key, stat in history.yields(source).items()
    }
    chosen = [
        stat
        for key, stat in history.yields(source).items()
        if not stale_only or verdicts[key] == "remove?"
    ]
    return sorted(
        chosen,
        key=lambda stat: (
            _VERDICT_ORDER[verdicts[stat.key]],
            stat.last_hit or "",
            stat.hits,
            stat.key,
        ),
    )


def _task_row(
    stat: TaskYield,
    *,
    configured: Collection[str],
    min_runs: int,
    min_days: int,
    day: str | None = None,
) -> tuple[str, ...]:
    """One report row: ``(task, runs, searches, rows seen, stored, last row, verdict)``."""
    verdict = stat.verdict(min_runs=min_runs, min_days=min_days, day=day)
    if stat.key not in configured:
        verdict += " (not in the config)"
    return (
        stat.description or stat.key,
        str(stat.runs),
        str(stat.searches),
        str(stat.hits),
        str(stat.stored),
        stat.last_hit or "never",
        verdict,
    )


def _tasks_report(args: argparse.Namespace) -> int:
    """`ricercar tasks` — what each configured task has yielded, worst first."""
    import json

    from ricercar.history import TaskHistory
    from ricercar.progress import CONSOLE

    settings = get_settings()
    log = get_logger()
    run = settings.run
    history = TaskHistory(Path(run.task_history_file)).load()

    wanted = args.task_source or args.source
    if wanted:
        names = [wanted]
    else:
        names = list(settings.sources) + [n for n in history.names() if n not in settings.sources]

    if args.prune:
        # Only sources this config knows: pruning from a partial `--config` file would
        # throw away the history of every task it happens not to mention.
        dropped = 0
        for name in settings.sources:
            known = set(history.yields(name))
            dropped += history.forget(name, known - _configured_keys(name, settings))
        history.save()
        log.info("Forgot {} task(s) that are no longer configured", dropped)

    payload: list[dict[str, object]] = []
    for name in names:
        configured = _configured_keys(name, settings)
        stats = _select_tasks(
            history,
            name,
            min_runs=run.stale_task_runs,
            min_days=run.stale_task_days,
            stale_only=args.stale,
        )
        if args.json:
            payload.extend(_task_json(name, stats, configured))
            continue

        if not stats:
            log.info(
                "{}: nothing to report{}",
                name,
                " — no task history yet" if not history.yields(name) else " (no stale task)",
            )
            continue

        from rich.table import Table

        table = Table(title=f"{name}: what each task yielded", title_justify="left")
        for column in ("task", "runs", "searches", "rows", "stored", "last row", "verdict"):
            table.add_column(
                column,
                justify="left" if column in ("task", "last row", "verdict") else "right",
                overflow="fold",
            )
        shown = stats if args.all else stats[:REVIEW_ROWS]
        for stat in shown:
            row = _task_row(
                stat,
                configured=configured,
                min_runs=run.stale_task_runs,
                min_days=run.stale_task_days,
            )
            table.add_row(row[0][:70], *row[1:])
        CONSOLE.print(table)
        if len(shown) < len(stats):
            log.info(
                "Showing {} of {} task(s) — `--all` (or `--json`) for the rest",
                len(shown),
                len(stats),
            )

    if args.json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


def _task_json(
    source: str,
    stats: Sequence[TaskYield],
    configured: Collection[str],
) -> list[dict[str, object]]:
    """The selected tasks with their raw numbers, for `--json`."""
    return [
        {
            "source": source,
            "key": stat.key,
            "configured": stat.key in configured,
            "description": stat.description,
            "runs": stat.runs,
            "searches": stat.searches,
            "rows": stat.hits,
            "stored": stat.stored,
            "first_run": stat.first_run,
            "last_run": stat.last_run,
            "last_hit": stat.last_hit,
            "last_stored": stat.last_stored,
            "days_without_hit": stat.days_without_hit(),
            "days_without_new": stat.days_without_new(),
        }
        for stat in stats
    ]


# ── Running ───────────────────────────────────────────────────────────────


def _runner_kwargs(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """The pipeline arguments the CLI can set."""
    from ricercar.sources import get_source

    return {
        "sources": [get_source(args.source, settings)] if args.source else None,
        "offline": Path(args.offline) if args.offline else None,
        "limit": args.limit,
        "resume": not args.no_resume,
    }


def _run_once(args: argparse.Namespace, *, shutdown: Any = None) -> int:
    """Run one cycle and turn the outcome into an exit code."""
    from ricercar.pipeline import Runner

    settings = get_settings()
    log = get_logger()
    runner = Runner(
        settings,
        shutdown=shutdown,
        **_runner_kwargs(args, settings),
    )
    try:
        outcome = asyncio.run(runner.run())
    except DatabaseSchemaError as exc:
        log.error("{}", exc)
        return 1

    log.info("{}", outcome.describe())
    if outcome.failed:
        return 1
    return INTERRUPTED_EXIT_CODE if outcome.interrupted else 0


class _Scheduler:
    """Repeats a run on an interval, and stops it politely on demand.

    The first Ctrl-C asks the run in progress to stop after the torrent it is
    handling; the second one leaves the schedule. That way an hour of work is not
    thrown away just because someone pressed a key.
    """

    def __init__(self, args: argparse.Namespace, settings: Settings) -> None:
        self._args = args
        self._settings = settings
        self._stop = threading.Event()
        self._current: Any = None
        self._previous: dict[int, Any] = {}

    def run(self) -> int:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        log = get_logger()
        interval = self._settings.schedule.interval_minutes
        timezone = datetime.now().astimezone().tzinfo

        scheduler = BackgroundScheduler(timezone=timezone)
        scheduler.start()
        scheduler.add_job(
            self._cycle,
            trigger=IntervalTrigger(minutes=interval),
            next_run_time=datetime.now(timezone) if self._settings.schedule.run_on_start else None,
            max_instances=1,
            coalesce=True,
            id="ricercar",
        )
        log.info(
            "Scheduled every {} minute(s){} — Ctrl-C stops the current run, twice leaves",
            interval,
            ", starting now" if self._settings.schedule.run_on_start else "",
        )

        self._install_handlers()
        try:
            while not self._stop.wait(1.0):
                pass
        finally:
            self._restore_handlers()
            scheduler.shutdown(wait=True)
        return 0

    def _cycle(self) -> None:
        from ricercar.pipeline import Shutdown

        log = get_logger()
        shutdown = Shutdown()
        self._current = shutdown
        try:
            _run_once(self._args, shutdown=shutdown)
        except Exception:  # noqa: BLE001 - a bad run must not stop the schedule
            log.exception("This run failed; the next one will try again")
        finally:
            self._current = None

    def _install_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError, AttributeError):
                continue

    def _restore_handlers(self) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous.clear()

    def _handle(self, signum: int, _frame: Any) -> None:
        log = get_logger()
        name = signal.Signals(signum).name
        running = self._current
        if running is not None and not running.requested:
            log.warning("{} — stopping after the torrent in hand", name)
            running.request(name)
        else:
            log.warning("{} again — leaving the schedule", name)
            self._stop.set()


def _run_scheduled(args: argparse.Namespace) -> int:
    return _Scheduler(args, get_settings()).run()
