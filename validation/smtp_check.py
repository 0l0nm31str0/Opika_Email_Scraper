"""
Async SMTP verification — verifies email deliverability by talking
directly to the domain's MX server over port 25.

Features:
  - Catch-all detection: sends two random fake addresses first;
    if both return 250, the domain is marked CATCH_ALL.
  - Async: runs SMTP_CONCURRENCY checks in parallel via asyncio.
  - Retries: max 3 retries, SMTP_TIMEOUT per attempt.
  - Always sends QUIT — never leaves connections hanging.
  - Disposable-email-domains check runs before SMTP.
  - 45-second retry delay on connection failures to avoid rate limits.
"""

from __future__ import annotations

import asyncio
import random
import string
from typing import Optional

from loguru import logger

import config
from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES
from validation.mx_check import get_mx_hosts

# ---------------------------------------------------------------------------
# Disposable email domain detection
# ---------------------------------------------------------------------------
_disposable_domains: Optional[set[str]] = None


def _load_disposable_domains() -> set[str]:
    global _disposable_domains
    if _disposable_domains is not None:
        return _disposable_domains
    try:
        from disposable_email_domains import blocklist
        _disposable_domains = set(blocklist)
        logger.debug(f"Loaded {len(_disposable_domains)} disposable email domains")
    except ImportError:
        logger.warning("disposable-email-domains not installed — skipping disposable check")
        _disposable_domains = set()
    return _disposable_domains


def is_disposable(domain: str) -> bool:
    """Check if a domain is a known disposable/temp email provider."""
    return domain.lower() in _load_disposable_domains()


# ---------------------------------------------------------------------------
# Catch-all detection cache
# ---------------------------------------------------------------------------
_catchall_cache: dict[str, bool] = {}


async def _check_catchall(mx_host: str, domain: str) -> bool:
    """
    Send two random fake addresses to detect catch-all domains.
    If BOTH return 250, the domain accepts everything → catch-all.
    """
    if domain in _catchall_cache:
        return _catchall_cache[domain]

    fake_addrs = [
        f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}@{domain}"
        for _ in range(2)
    ]

    results = []
    for fake in fake_addrs:
        code = await _smtp_rcpt_check(mx_host, fake)
        results.append(code)

    is_catchall = all(c == 250 for c in results)
    _catchall_cache[domain] = is_catchall
    if is_catchall:
        logger.debug(f"[SMTP] {domain} is a catch-all domain")
    return is_catchall


# ---------------------------------------------------------------------------
# Core SMTP RCPT TO check
# ---------------------------------------------------------------------------

async def _smtp_rcpt_check(mx_host: str, email: str, retries: int = MAX_RETRIES) -> int:
    """
    Open a raw SMTP connection and check RCPT TO response code.

    Returns the SMTP response code (250 = valid, 550/551/553 = invalid,
    0 = timeout/connection error).
    """
    for attempt in range(retries + 1):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(mx_host, 25),
                timeout=config.SMTP_TIMEOUT,
            )

            try:
                # Read greeting
                await asyncio.wait_for(reader.readline(), timeout=config.SMTP_TIMEOUT)

                # EHLO
                writer.write(b"EHLO verify.check\r\n")
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=config.SMTP_TIMEOUT)

                # MAIL FROM
                writer.write(b"MAIL FROM: <verify@opika-check.com>\r\n")
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=config.SMTP_TIMEOUT)

                # RCPT TO — this is the actual verification step
                writer.write(f"RCPT TO: <{email}>\r\n".encode())
                await writer.drain()
                response = await asyncio.wait_for(reader.readline(), timeout=config.SMTP_TIMEOUT)
                code = int(response[:3])

                # Always send QUIT — never leave connections hanging
                writer.write(b"QUIT\r\n")
                await writer.drain()

                return code

            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
            logger.debug(f"[SMTP] Attempt {attempt+1} for {email} via {mx_host}: {exc}")
            if attempt < retries:
                # 45-second retry delay to avoid hitting rate limits
                logger.warning(
                    f"[SMTP] Connection failed for {email} (attempt {attempt+1}/{retries+1}) "
                    f"— retrying in {UTIL_RETRY_DELAY}s…"
                )
                await asyncio.sleep(UTIL_RETRY_DELAY)
            continue
        except Exception as exc:
            logger.debug(f"[SMTP] Unexpected error for {email}: {exc}")
            return 0

    return 0  # all retries exhausted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def verify_email(email: str) -> str:
    """
    Verify a single email address via SMTP.

    Returns one of: VALID, INVALID, CATCH_ALL, UNKNOWN, DISPOSABLE
    """
    domain = email.split("@")[1].lower()

    # Check disposable first — no SMTP needed
    if is_disposable(domain):
        return "DISPOSABLE"

    mx_hosts = get_mx_hosts(domain)
    if not mx_hosts:
        return "INVALID"  # no MX = can't receive mail

    mx_host = mx_hosts[0]  # use highest-priority MX

    # Catch-all detection
    is_catchall = await _check_catchall(mx_host, domain)
    if is_catchall:
        return "CATCH_ALL"

    # Actual RCPT TO check
    code = await _smtp_rcpt_check(mx_host, email)

    if code == 250:
        return "VALID"
    elif code in (550, 551, 553):
        return "INVALID"
    else:
        return "UNKNOWN"


async def verify_emails_batch(
    emails: list[str],
    log_callback=None,
) -> dict[str, str]:
    """
    Verify a batch of emails with bounded concurrency.

    Returns {email: status} dict.
    """
    results: dict[str, str] = {}
    sem = asyncio.Semaphore(config.SMTP_CONCURRENCY)

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    async def _verify_one(email: str):
        async with sem:
            status = await verify_email(email)
            results[email] = status
            if status == "VALID":
                _log(f"[SMTP] ✓ {email} → VALID", "success")
            elif status == "INVALID":
                _log(f"[SMTP] ✕ {email} → INVALID", "error")
            else:
                _log(f"[SMTP] → {email} → {status}", "info")

    _log(f"[SMTP] Verifying {len(emails)} emails (concurrency={config.SMTP_CONCURRENCY})")
    tasks = [_verify_one(e) for e in emails]
    await asyncio.gather(*tasks, return_exceptions=True)

    return results


def clear_catchall_cache():
    """Clear the catch-all cache (useful for testing)."""
    _catchall_cache.clear()
