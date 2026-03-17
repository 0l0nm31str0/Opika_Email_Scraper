"""
Centralized retry logic for all external HTTP calls and SMTP checks.

Retries on 429 (rate limit), 500 (server error), and 529 (overloaded).
Waits 45 seconds between attempts. Max 3 retries.
Both sync and async versions provided.
"""

import asyncio
import functools
import time

import httpx
from loguru import logger

RETRYABLE_STATUS_CODES = {429, 500, 529}
RETRY_DELAY = 45  # seconds
MAX_RETRIES = 3


def with_retry(func):
    """
    Decorator that retries a function on retryable HTTP errors.
    Waits 45 seconds between attempts. Max 3 retries.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        attempts = 0
        while attempts <= MAX_RETRIES:
            try:
                return func(*args, **kwargs)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in RETRYABLE_STATUS_CODES and attempts < MAX_RETRIES:
                    attempts += 1
                    logger.warning(
                        f"API error {code} on attempt {attempts}/"
                        f"{MAX_RETRIES}. Retrying in {RETRY_DELAY}s..."
                    )
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(
                        f"API error {code} — giving up after "
                        f"{attempts} attempts."
                    )
                    raise
            except httpx.RequestError as e:
                if attempts < MAX_RETRIES:
                    attempts += 1
                    logger.warning(
                        f"Request failed ({e}). Retrying in {RETRY_DELAY}s..."
                    )
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"Request failed after {attempts} attempts.")
                    raise
    return wrapper


def with_retry_async(func):
    """
    Async version of the retry decorator.
    Use this for all asyncio-based scrapers and SMTP checks.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        attempts = 0
        while attempts <= MAX_RETRIES:
            try:
                return await func(*args, **kwargs)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in RETRYABLE_STATUS_CODES and attempts < MAX_RETRIES:
                    attempts += 1
                    logger.warning(
                        f"API error {code} on attempt {attempts}/"
                        f"{MAX_RETRIES}. Retrying in {RETRY_DELAY}s..."
                    )
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    logger.error(f"API error {code} — giving up.")
                    raise
            except (httpx.RequestError, ConnectionRefusedError) as e:
                if attempts < MAX_RETRIES:
                    attempts += 1
                    logger.warning(
                        f"Request failed ({e}). Retrying in {RETRY_DELAY}s..."
                    )
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    logger.error(f"Request failed after {attempts} attempts.")
                    raise
    return wrapper
