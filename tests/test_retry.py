"""Retrying, graded by what actually went wrong."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ricercar.config import RetryConfig
from ricercar.retry import Policies, call
from ricercar.sources.base import SelectorsBrokenError


def _policies(*, attempts: int = 3) -> Policies:
    return Policies.from_config(
        RetryConfig(max_attempts=attempts, base_delay=1.0, max_delay=10.0, backoff_factor=2.0)
    )


def _sleeps() -> tuple[list[float], Callable[[float], Awaitable[None]]]:
    """A recorder standing in for ``asyncio.sleep``, so no test waits for a backoff."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    return recorded, fake_sleep


def test_the_policy_comes_from_the_exception_that_was_raised() -> None:
    policies = _policies()

    assert policies.for_exception(PlaywrightTimeout("slow")).name == "timeout"
    assert policies.for_exception(PlaywrightError("flaky")).name == "transient"
    assert policies.for_exception(SelectorsBrokenError(["no rows"])) is None
    assert policies.for_exception(ValueError("a bug of our own")) is None


def test_a_timeout_is_given_more_room_than_a_hiccup() -> None:
    policies = _policies()

    assert policies.timeout.base_delay == 2 * policies.transient.base_delay
    assert policies.timeout.delay_before_retry(1) == 2.0
    assert policies.timeout.delay_before_retry(2) == 4.0
    assert policies.timeout.delay_before_retry(20) == 10.0  # capped by max_delay


def test_call_retries_until_the_operation_succeeds() -> None:
    recorded, fake_sleep = _sleeps()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PlaywrightTimeout(f"attempt {calls}")
        return "ok"

    result = asyncio.run(call("search Bach", operation, policies=_policies(), sleep=fake_sleep))

    assert result == "ok"
    assert calls == 3
    assert recorded == [2.0, 4.0]


def test_call_gives_up_after_the_attempts_it_has() -> None:
    recorded, fake_sleep = _sleeps()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise PlaywrightError("connection closed")

    with pytest.raises(PlaywrightError, match="connection closed"):
        asyncio.run(call("search", operation, policies=_policies(attempts=2), sleep=fake_sleep))

    assert calls == 2
    assert recorded == [1.0]


def test_a_failure_that_retrying_cannot_fix_is_raised_at_once() -> None:
    recorded, fake_sleep = _sleeps()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise SelectorsBrokenError(["the header announces 50 result(s) but no row matches"])

    with pytest.raises(SelectorsBrokenError):
        asyncio.run(call("search", operation, policies=_policies(), sleep=fake_sleep))

    assert calls == 1
    assert recorded == []


def test_no_attempts_means_no_retry() -> None:
    recorded, fake_sleep = _sleeps()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise PlaywrightError("nope")

    with pytest.raises(PlaywrightError):
        asyncio.run(call("download", operation, policies=_policies(attempts=0), sleep=fake_sleep))

    assert calls == 1
    assert recorded == []
