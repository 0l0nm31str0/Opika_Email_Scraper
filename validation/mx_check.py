"""
MX record validation — confirms a domain has valid mail exchange records.
Results are cached per domain to avoid redundant DNS lookups.
Retries DNS lookups up to 3 times with 45-second delay on transient failures.
"""

from __future__ import annotations

import time
from typing import Optional

import dns.resolver
from loguru import logger

from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES

# Domain → list[MX host] cache.  None means "already looked up, no MX found".
_mx_cache: dict[str, Optional[list[str]]] = {}


def get_mx_hosts(domain: str) -> Optional[list[str]]:
    """
    Look up MX records for *domain*.

    Returns a sorted list of MX hostnames (lowest priority first),
    or None if the domain has no MX records.

    Results are cached — the same domain is never queried twice.
    """
    domain = domain.lower().strip()

    if domain in _mx_cache:
        return _mx_cache[domain]

    # Retry loop for transient DNS failures (timeout, network blip)
    for attempt in range(MAX_RETRIES + 1):
        try:
            answers = dns.resolver.resolve(domain, "MX")
            hosts = sorted(
                [(r.preference, str(r.exchange).rstrip(".")) for r in answers],
                key=lambda x: x[0],
            )
            mx_list = [h for _, h in hosts]
            _mx_cache[domain] = mx_list
            logger.debug(f"[MX] {domain} → {mx_list}")
            return mx_list

        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            # Definitive "no records" — no point retrying
            logger.debug(f"[MX] {domain} → no MX records")
            _mx_cache[domain] = None
            return None

        except dns.resolver.NoNameservers:
            logger.debug(f"[MX] {domain} → no nameservers")
            _mx_cache[domain] = None
            return None

        except (dns.resolver.LifetimeTimeout, dns.exception.Timeout) as exc:
            # Transient timeout — retry with 45s delay
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"[MX] {domain} DNS timeout (attempt {attempt+1}/{MAX_RETRIES+1}) "
                    f"— retrying in {UTIL_RETRY_DELAY}s…"
                )
                time.sleep(UTIL_RETRY_DELAY)
                continue
            logger.debug(f"[MX] {domain} → timeout after {MAX_RETRIES+1} attempts")
            _mx_cache[domain] = None
            return None

        except Exception as exc:
            logger.debug(f"[MX] {domain} → error: {exc}")
            _mx_cache[domain] = None
            return None

    _mx_cache[domain] = None
    return None


def has_mx(domain: str) -> bool:
    """Quick check: does this domain have at least one MX record?"""
    hosts = get_mx_hosts(domain)
    return hosts is not None and len(hosts) > 0


def clear_cache():
    """Clear the MX cache (useful for testing)."""
    _mx_cache.clear()
