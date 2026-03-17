"""
Deduplication pipeline — exact email dedup + fuzzy name+company dedup.

Uses rapidfuzz token_sort_ratio for fuzzy matching.
When two records are fuzzy-duplicates, the one with the higher
confidence_score is kept.
"""

from __future__ import annotations

from loguru import logger
from rapidfuzz import fuzz


def deduplicate(contacts: list[dict], log_callback=None) -> list[dict]:
    """
    Two-pass deduplication:
      1. Exact email dedup — drop exact duplicate email addresses.
      2. Fuzzy name+company dedup — if first_name + last_name + company_name
         match >= 85% (token_sort_ratio), keep the higher-scored record.
    """
    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    original_count = len(contacts)

    # --- Pass 1: Exact email dedup ---
    seen_emails: set[str] = set()
    unique: list[dict] = []
    for c in contacts:
        email = c.get("email", "").lower().strip()
        if not email:
            unique.append(c)  # keep contacts without emails (name-only)
            continue
        if email not in seen_emails:
            seen_emails.add(email)
            unique.append(c)

    after_exact = len(unique)
    _log(f"[Dedup] Exact email dedup: {original_count} → {after_exact}")

    # --- Pass 2: Fuzzy name+company dedup ---
    # Build a composite key string for each contact
    def _composite(c: dict) -> str:
        parts = [
            c.get("first_name", ""),
            c.get("last_name", ""),
            c.get("company_name", "") or c.get("business_name", ""),
        ]
        return " ".join(p.lower().strip() for p in parts if p)

    # Sort by confidence_score descending so we keep the best record first
    unique.sort(key=lambda c: c.get("confidence_score", 0), reverse=True)

    final: list[dict] = []
    for c in unique:
        comp = _composite(c)
        if not comp.strip():
            final.append(c)
            continue
        is_dup = False
        for existing in final:
            existing_comp = _composite(existing)
            if not existing_comp.strip():
                continue
            ratio = fuzz.token_sort_ratio(comp, existing_comp)
            if ratio >= 85:
                is_dup = True
                break
        if not is_dup:
            final.append(c)

    _log(f"[Dedup] Fuzzy name+company dedup: {after_exact} → {len(final)}", "success")
    return final
