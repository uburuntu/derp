"""Retry utilities with API key rotation for rate limit handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import logfire
from google.genai.errors import ClientError

T = TypeVar("T")


def is_rate_limit_error(exception: Exception) -> bool:
    """Check if exception is a rate limit (429) error."""
    if isinstance(exception, ClientError):
        # Check for 429 status code
        if hasattr(exception, "status_code") and exception.status_code == 429:
            return True
        # Check error message for rate limit indicators
        error_msg = str(exception).lower()
        return any(
            indicator in error_msg
            for indicator in ["429", "rate limit", "quota exceeded", "too many requests"]
        )
    return False


def is_retryable_error(exception: Exception) -> bool:
    """Check if exception is retryable (rate limit or transient error)."""
    if is_rate_limit_error(exception):
        return True

    # Check for other transient errors
    if isinstance(exception, (asyncio.TimeoutError, ConnectionError)):
        return True

    if isinstance(exception, ClientError):
        # 5xx errors are typically transient
        if hasattr(exception, "status_code") and 500 <= exception.status_code < 600:
            return True

    return False


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 16.0,
        exponential_base: float = 2.0,
        rotate_key_on_rate_limit: bool = True,
    ) -> None:
        """Initialize retry configuration.

        Args:
            max_attempts: Maximum number of retry attempts
            initial_delay: Initial delay in seconds before first retry
            max_delay: Maximum delay in seconds between retries
            exponential_base: Base for exponential backoff calculation
            rotate_key_on_rate_limit: Whether to rotate API key on rate limit errors
        """
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.rotate_key_on_rate_limit = rotate_key_on_rate_limit


def retry_with_key_rotation(
    config: RetryConfig | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for retrying async functions with API key rotation on rate limits.

    Args:
        config: Retry configuration, uses defaults if None

    Returns:
        Decorated function with retry logic

    Example:
        @retry_with_key_rotation(RetryConfig(max_attempts=4))
        async def make_api_call(client: genai.Client) -> Response:
            return await client.generate(...)
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception = None
            attempt = 0

            while attempt < config.max_attempts:
                try:
                    # Execute the function
                    result = await func(*args, **kwargs)

                    # Log success if this was a retry
                    if attempt > 0:
                        logfire.info(
                            "retry_succeeded",
                            function=func.__name__,
                            attempt=attempt + 1,
                            total_attempts=config.max_attempts,
                        )

                    return result

                except Exception as e:
                    last_exception = e
                    attempt += 1

                    # Check if we should retry
                    if not is_retryable_error(e):
                        logfire.warning(
                            "non_retryable_error",
                            function=func.__name__,
                            error_type=type(e).__name__,
                            error=str(e),
                        )
                        raise

                    # Check if we've exhausted attempts
                    if attempt >= config.max_attempts:
                        logfire.error(
                            "retry_exhausted",
                            function=func.__name__,
                            attempts=attempt,
                            error_type=type(e).__name__,
                            error=str(e),
                        )
                        raise

                    # Handle rate limit with key rotation
                    if is_rate_limit_error(e) and config.rotate_key_on_rate_limit:
                        new_key = next(settings.google_api_key_iter)
                        logfire.warning(
                            "rate_limit_rotating_key",
                            function=func.__name__,
                            attempt=attempt,
                            new_key_prefix=new_key[:8] + "...",
                        )

                        # Update the client in args/kwargs if present
                        # This is a bit hacky but necessary for key rotation
                        if args and hasattr(args[0], "api_key"):
                            # If first arg is client-like object with api_key
                            args[0].api_key = new_key
                        elif "client" in kwargs and hasattr(kwargs["client"], "api_key"):
                            kwargs["client"].api_key = new_key

                    # Calculate delay with exponential backoff
                    delay = min(
                        config.initial_delay * (config.exponential_base ** (attempt - 1)),
                        config.max_delay,
                    )

                    logfire.info(
                        "retrying_after_delay",
                        function=func.__name__,
                        attempt=attempt,
                        max_attempts=config.max_attempts,
                        delay_seconds=delay,
                        error_type=type(e).__name__,
                        is_rate_limit=is_rate_limit_error(e),
                    )

                    await asyncio.sleep(delay)

            # Should never reach here, but just in case
            if last_exception:
                raise last_exception
            raise RuntimeError(f"Retry logic failed unexpectedly for {func.__name__}")

        return wrapper

    return decorator
