"""Retrying, graded by what actually went wrong.

The legacy tool retried everything identically: one exception type, three attempts,
no delay between them. Failures are not that uniform — a dropped connection is
worth a quick retry, a timeout is worth a longer one, an expired session needs a
login rather than a retry, and a torrent that is gone or a page whose selectors no
longer match is not worth retrying at all.

:func:`call` therefore picks its policy from the exception that was actually
raised. That is also why tenacity is not used here: its retry predicate cannot
switch the wait strategy per exception. tenacity still backs the uniform retries —
the SQLite lock and :func:`ricercar.browser.navigation.goto`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ricercar.config import RetryConfig
from ricercar.log import get_logger
from ricercar.sources.base import SiteUnderMaintenanceError


@dataclass(frozen=True, slots=True)
class Policy:
    """How one class of failure is retried."""

    name: str
    attempts: int
    base_delay: float
    max_delay: float
    backoff: float
    retry_on: tuple[type[BaseException], ...]

    def delay_before_retry(self, attempt: int) -> float:
        """Seconds to wait after *attempt* (1-based) failed."""
        return min(self.max_delay, self.base_delay * self.backoff ** (attempt - 1))


@dataclass(frozen=True, slots=True)
class Policies:
    """The policies in use, built from ``retry:`` in the configuration."""

    transient: Policy
    """A browser or network hiccup: retry soon."""
    timeout: Policy
    """A slow or unresponsive page: retry, but back off further."""
    maintenance: Policy
    """Site under maintenance: retry with a much longer back-off."""

    @classmethod
    def from_config(cls, cfg: RetryConfig) -> Policies:
        return cls(
            transient=Policy(
                name="transient",
                attempts=cfg.max_attempts,
                base_delay=cfg.base_delay,
                max_delay=cfg.max_delay,
                backoff=cfg.backoff_factor,
                retry_on=(PlaywrightError,),
            ),
            timeout=Policy(
                name="timeout",
                attempts=cfg.max_attempts,
                base_delay=cfg.base_delay * 2,
                max_delay=cfg.max_delay,
                backoff=cfg.backoff_factor,
                retry_on=(PlaywrightTimeout,),
            ),
            maintenance=Policy(
                name="maintenance",
                attempts=cfg.max_attempts,
                base_delay=cfg.base_delay,
                max_delay=cfg.maintenance_max_delay,
                backoff=cfg.backoff_factor,
                retry_on=(SiteUnderMaintenanceError,),
            ),
        )

    def for_exception(self, exc: BaseException) -> Policy | None:
        """The policy covering *exc*, or ``None`` when it must not be retried.

        Timeouts are checked first because :class:`PlaywrightTimeout` is a
        :class:`PlaywrightError`. Anything that is not a browser error — an expired
        session, an unavailable torrent, a page whose selectors no longer match —
        falls through to ``None`` and is reported rather than retried.
        """
        for policy in (self.maintenance, self.timeout, self.transient):
            if isinstance(exc, policy.retry_on):
                return policy
        return None


async def call[T](
    what: str,
    operation: Callable[[], Awaitable[T]],
    *,
    policies: Policies,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run *operation*, retrying the failures that are worth retrying.

    ``what`` only names the operation in the log. ``sleep`` is injectable so a test
    can assert the backoff without waiting for it.

    Raises:
        Exception: the original failure, once its policy is exhausted or when no
            policy covers it.
    """
    log = get_logger()
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as exc:
            policy = policies.for_exception(exc)
            attempt += 1
            if policy is None or attempt >= policy.attempts:
                raise
            delay = policy.delay_before_retry(attempt)
            log.warning(
                "{} failed ({}: {}), retrying in {:.1f}s ({}/{})",
                what,
                type(exc).__name__,
                exc,
                delay,
                attempt + 1,
                policy.attempts,
            )
            await sleep(delay)


__all__ = ["Policies", "Policy", "call"]
