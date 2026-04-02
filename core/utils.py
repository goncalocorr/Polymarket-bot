"""
Shared utilities — retry decorator, helpers.
Ported from Jon-Becker retry_request pattern.
"""

import asyncio
import functools
from typing import TypeVar, Callable

from loguru import logger

F = TypeVar("F", bound=Callable)


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retryable: tuple[type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
    ),
):
    """
    Async retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts.
        base_delay: Initial delay in seconds (doubles each retry).
        max_delay: Maximum delay cap in seconds.
        retryable: Exception types that trigger a retry.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except retryable as exc:
                    last_error = exc
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(
                            f"{fn.__name__} failed (attempt {attempt + 1}/{max_attempts}): "
                            f"{exc!r} — retrying in {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
            raise last_error  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
