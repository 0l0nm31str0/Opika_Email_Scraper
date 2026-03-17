"""
Post-scrape ICP filters — revenue, company size, and company age.

These filters work on *inferred* signals, not verified data.  When a
filter is set to "Any" or blank it is skipped entirely.  Contacts with
"unknown" data are always kept — we only drop contacts when the data
is present AND definitively falls outside the selected range.
"""

from __future__ import annotations

import re
import datetime
from typing import Callable, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Revenue inference helpers
# ---------------------------------------------------------------------------

_REVENUE_RANGES: dict[str, tuple[float, float]] = {
    "Under $1M": (0, 1_000_000),
    "$1M – $5M": (1_000_000, 5_000_000),
    "$5M – $10M": (5_000_000, 10_000_000),
    "$10M – $25M": (10_000_000, 25_000_000),
    "$25M – $50M": (25_000_000, 50_000_000),
    "$50M – $100M": (50_000_000, 100_000_000),
    "Over $100M": (100_000_000, float("inf")),
}

_SIZE_RANGES: dict[str, tuple[int, int]] = {
    "1 – 5": (1, 5),
    "6 – 20": (6, 20),
    "21 – 50": (21, 50),
    "51 – 100": (51, 100),
    "101 – 250": (101, 250),
    "251 – 500": (251, 500),
    "500+": (500, 999_999),
}

_AGE_RANGES: dict[str, tuple[int, int]] = {
    "Less than 1 year": (0, 1),
    "1 – 3 years": (1, 3),
    "3 – 5 years": (3, 5),
    "5 – 10 years": (5, 10),
    "10+ years": (10, 999),
}


def apply_filters(
    contacts: list[dict],
    revenue_filter: Optional[str] = None,
    size_filter: Optional[str] = None,
    age_filter: Optional[str] = None,
    log_callback: Optional[Callable] = None,
) -> list[dict]:
    """
    Filter contacts by revenue, size, and age.  "Any"/blank = skip.
    Unknown values are always kept.
    """
    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    before = len(contacts)
    filtered = contacts

    if revenue_filter and revenue_filter != "Any":
        filtered = _filter_revenue(filtered, revenue_filter)
    if size_filter and size_filter != "Any":
        filtered = _filter_size(filtered, size_filter)
    if age_filter and age_filter != "Any":
        filtered = _filter_age(filtered, age_filter)

    dropped = before - len(filtered)
    if dropped:
        _log(f"[Filters] Dropped {dropped} contacts outside ICP criteria ({len(filtered)} remaining)")
    else:
        _log(f"[Filters] All {len(filtered)} contacts passed ICP filters")

    return filtered


def _filter_revenue(contacts: list[dict], label: str) -> list[dict]:
    rng = _REVENUE_RANGES.get(label)
    if not rng:
        return contacts
    lo, hi = rng
    result = []
    for c in contacts:
        rev = c.get("annual_revenue_est")
        if rev is None or rev == "unknown" or rev == "":
            result.append(c)  # unknown = keep
            continue
        try:
            val = float(str(rev).replace("$", "").replace(",", ""))
            if lo <= val <= hi:
                result.append(c)
        except (ValueError, TypeError):
            result.append(c)  # can't parse = keep
    return result


def _filter_size(contacts: list[dict], label: str) -> list[dict]:
    rng = _SIZE_RANGES.get(label)
    if not rng:
        return contacts
    lo, hi = rng
    result = []
    for c in contacts:
        size = c.get("employee_count_est")
        if size is None or size == "unknown" or size == "":
            result.append(c)
            continue
        try:
            val = int(str(size).replace(",", ""))
            if lo <= val <= hi:
                result.append(c)
        except (ValueError, TypeError):
            result.append(c)
    return result


def _filter_age(contacts: list[dict], label: str) -> list[dict]:
    rng = _AGE_RANGES.get(label)
    if not rng:
        return contacts
    lo, hi = rng
    result = []
    for c in contacts:
        age = c.get("company_age_est")
        if age is None or age == "unknown" or age == "":
            result.append(c)
            continue
        try:
            val = int(str(age).replace(" years", "").replace("+", ""))
            if lo <= val <= hi:
                result.append(c)
        except (ValueError, TypeError):
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Revenue / size / age inference — called during enrichment phase
# ---------------------------------------------------------------------------

def infer_company_signals(contact: dict) -> dict:
    """
    Infer revenue, size, and age from scraped signals.
    Sets *_est fields to a value or "unknown".
    """
    # Revenue inference from Yelp review count (rough proxy)
    review_count = contact.get("_yelp_review_count", 0)
    if review_count and int(review_count) > 0:
        rc = int(review_count)
        if rc < 20:
            contact["annual_revenue_est"] = "Under $1M"
        elif rc < 100:
            contact["annual_revenue_est"] = "$1M – $5M"
        elif rc < 500:
            contact["annual_revenue_est"] = "$5M – $10M"
        else:
            contact["annual_revenue_est"] = "$10M – $25M"
    else:
        contact.setdefault("annual_revenue_est", "unknown")

    # Employee count — try to extract from scraped data
    contact.setdefault("employee_count_est", "unknown")

    # Company age — from domain WHOIS or copyright year
    copyright_year = contact.get("_copyright_year")
    if copyright_year:
        try:
            age = datetime.datetime.now().year - int(copyright_year)
            contact["company_age_est"] = str(max(age, 0))
        except (ValueError, TypeError):
            contact.setdefault("company_age_est", "unknown")
    else:
        contact.setdefault("company_age_est", "unknown")

    return contact
