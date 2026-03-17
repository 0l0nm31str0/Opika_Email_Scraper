"""
Confidence scoring — assigns a 0–100 score to each contact based on
source quality, validation status, profile completeness, and title match.
"""

from __future__ import annotations

from loguru import logger

# --- Point tables ---

SOURCE_POINTS: dict[str, int] = {
    "google_maps": 25,
    "yelp": 20,
    "google_dork": 15,
    "website_crawl": 20,
    "permutation": 10,
}

VALIDATION_POINTS: dict[str, int] = {
    "VALID": 40,
    "CATCH_ALL": 8,
    "UNKNOWN": 5,
    "DISPOSABLE": 0,
    "INVALID": 0,
    "MX_ONLY": 20,  # basic validation mode (no SMTP)
}


def score_contact(contact: dict, target_titles: list[str] | None = None) -> int:
    """
    Score a single contact dict and return an integer 0–100.

    The contact dict is also mutated: contact["confidence_score"] is set.
    """
    score = 0

    # --- Source points (max 30, capped) ---
    source = contact.get("source", "")
    score += min(SOURCE_POINTS.get(source, 0), 30)

    # --- Validation points (max 40) ---
    v_status = contact.get("validation_status", "UNKNOWN")
    score += min(VALIDATION_POINTS.get(v_status, 0), 40)

    # --- Profile completeness bonus (max 20) ---
    if contact.get("first_name") and contact.get("last_name"):
        score += 8
    if contact.get("job_title"):
        score += 6
    if contact.get("phone"):
        score += 4
    if contact.get("company_name") or contact.get("business_name"):
        score += 2

    # --- Title match bonus (max 10) ---
    if target_titles:
        job = (contact.get("job_title") or "").lower()
        for title in target_titles:
            if title.lower() in job:
                score += 10
                break

    score = min(score, 100)
    contact["confidence_score"] = score
    return score


def score_all(
    contacts: list[dict],
    target_titles: list[str] | None = None,
    log_callback=None,
) -> list[dict]:
    """Score all contacts and return them sorted by score descending."""
    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    for c in contacts:
        score_contact(c, target_titles)

    contacts.sort(key=lambda c: c.get("confidence_score", 0), reverse=True)

    avg = sum(c.get("confidence_score", 0) for c in contacts) / max(len(contacts), 1)
    _log(f"[Scoring] Scored {len(contacts)} contacts (avg: {avg:.1f})", "success")

    return contacts
