"""
Logging system — built on loguru for maximum flexibility.

Provides:
- Coloured, developer-friendly console output
- Automatic log file rotation & retention
- Optional JSON-structured output (for log aggregation tools)
- Context-aware logging via ``bind()``

Console output goes through the shared rich console (see :mod:`ricercar.progress`)
so that log lines and progress bars coexist instead of overwriting each other.
"""

from __future__ import annotations

import json
import logging
import sys
from types import FrameType
from typing import Any

from loguru import logger as _loguru_logger

from ricercar.config import DATA_DIR, LogConfig
from ricercar.progress import CONSOLE

# ── Intercept standard logging ─────────────────────────────────────────────


class _InterceptHandler(logging.Handler):
    """Route standard-library ``logging`` calls through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = _loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno  # type: ignore[assignment]

        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        _loguru_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _console_sink(message: Any) -> None:
    """Write a formatted log record through the shared console.

    Going through the console rather than straight to stderr is what keeps a
    running progress bar intact — rich redraws the bar around the line. ``markup``
    stays off because loguru already colourises the record itself.
    """
    CONSOLE.print(str(message), markup=False, highlight=False, end="")


def _json_sink(message: Any) -> None:
    """Serialize a log record as JSON and write to stdout."""
    record = message.record
    entry: dict[str, Any] = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }
    if record.get("extra"):
        entry["extra"] = record["extra"]
    if record["exception"]:
        entry["exception"] = str(record["exception"])
    sys.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ── Public API ─────────────────────────────────────────────────────────────


def configure_logging(cfg: LogConfig | None = None) -> None:
    """Configure loguru sinks from a ``LogConfig`` object.

    Call once at application startup.  Idempotent — removes existing sinks
    and configures fresh ones.
    """
    if cfg is None:
        from ricercar.config import get_settings

        cfg = get_settings().log

    # Remove all pre-existing sinks (loguru's default + any previous calls)
    _loguru_logger.remove()

    # ── Console sink ────────────────────────────────────────────────────
    if cfg.json_output:
        _loguru_logger.add(
            _json_sink,
            level=cfg.level.value,
        )
    else:
        _loguru_logger.add(
            _console_sink,
            level=cfg.level.value,
            format=cfg.format,
            colorize=cfg.colorize,
            backtrace=True,
            diagnose=True,
        )

    # ── File sink (always active, always text) ──────────────────────────
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _loguru_logger.add(
        str(log_dir / "ricercar_{time:YYYY-MM-DD}.log"),
        level="TRACE",  # file captures everything
        rotation=cfg.file_rotation,
        retention=cfg.file_retention,
        compression="gz",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )

    # ── Intercept standard logging → loguru ─────────────────────────────
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)


def get_logger() -> Any:
    """Return the loguru logger — use as a module-level convenience.

    Usage::

        from ricercar.log import get_logger

        logger = get_logger()
        logger.info("Hello {}", name)
        logger.bind(torrent_id=42).debug("Processing…")
    """
    return _loguru_logger


def bind(**kwargs: Any) -> Any:
    """Return a bound logger with extra context fields.

    All subsequent log calls from the returned logger will include these
    fields (shown in console, always present in JSON mode).
    """
    return _loguru_logger.bind(**kwargs)
