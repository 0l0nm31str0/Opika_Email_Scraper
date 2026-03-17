"""
ICP (Ideal Customer Profile) parser.

Extracts industry, location, job titles, and company type from a plain-English
description string using spaCy NER + keyword matching.  Falls back gracefully
to form-field overrides when NLP extraction is incomplete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Lazy-load spaCy so the rest of the app can import this module even if the
# model isn't installed yet (e.g. during tests or first-time setup).
# ---------------------------------------------------------------------------
_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        try:
            import spacy
            _nlp = spacy.load("en_core_web_sm")
            logger.info("spaCy en_core_web_sm loaded successfully")
        except OSError:
            logger.warning(
                "spaCy model 'en_core_web_sm' not found — "
                "ICP parser will rely on keyword matching only. "
                "Install with: python -m spacy download en_core_web_sm"
            )
    return _nlp


# ---------------------------------------------------------------------------
# Keyword maps — used as a fallback (or supplement) to NER.
# ---------------------------------------------------------------------------

INDUSTRY_KEYWORDS: dict[str, str] = {
    "hvac": "HVAC / Home Services",
    "heating": "HVAC / Home Services",
    "air conditioning": "HVAC / Home Services",
    "home service": "HVAC / Home Services",
    "personal injury": "Personal Injury Law",
    "injury law": "Personal Injury Law",
    "injury attorney": "Personal Injury Law",
    "self storage": "Self Storage",
    "storage facility": "Self Storage",
    "roofing": "Roofing",
    "roofer": "Roofing",
    "plumbing": "Plumbing",
    "plumber": "Plumbing",
    "real estate": "Real Estate",
    "realtor": "Real Estate",
    "dental": "Dental / Medical",
    "dentist": "Dental / Medical",
    "medical": "Dental / Medical",
    "doctor": "Dental / Medical",
    "clinic": "Dental / Medical",
    "insurance": "Insurance",
    "insurer": "Insurance",
}

TITLE_KEYWORDS: list[str] = [
    "owner", "ceo", "cfo", "cto", "coo", "founder", "co-founder",
    "president", "vice president", "vp", "director", "manager",
    "operations manager", "general manager", "partner",
    "managing partner", "principal", "head of", "chief",
]

COMPANY_TYPE_KEYWORDS: list[str] = [
    "company", "companies", "firm", "firms", "agency", "agencies",
    "clinic", "clinics", "practice", "practices", "office", "offices",
    "business", "businesses", "shop", "shops", "store", "stores",
    "facility", "facilities",
]

# US state name → abbreviation (common ones; extend as needed)
STATE_ABBREVS: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


@dataclass
class ICPResult:
    """Structured output of the ICP parser."""
    industry: Optional[str] = None
    location: Optional[str] = None
    state_abbrev: Optional[str] = None
    target_titles: list[str] = field(default_factory=list)
    company_type: Optional[str] = None


def _extract_industry(text_lower: str) -> Optional[str]:
    """Match the longest industry keyword found in the text."""
    best_match: Optional[str] = None
    best_len = 0
    for kw, label in INDUSTRY_KEYWORDS.items():
        if kw in text_lower and len(kw) > best_len:
            best_match = label
            best_len = len(kw)
    return best_match


def _extract_titles(text_lower: str) -> list[str]:
    """Pull job-title phrases out of the text."""
    found: list[str] = []
    # Sort longest-first so "operations manager" matches before "manager"
    for kw in sorted(TITLE_KEYWORDS, key=len, reverse=True):
        if kw in text_lower:
            found.append(kw)
            # Remove matched keyword so shorter substrings don't double-match
            text_lower = text_lower.replace(kw, " ")
    return found


def _extract_company_type(text_lower: str) -> Optional[str]:
    for kw in COMPANY_TYPE_KEYWORDS:
        if kw in text_lower:
            return kw
    return None


def _extract_location_spacy(text: str) -> Optional[str]:
    """Use spaCy GPE entities to find a location mention."""
    nlp = _get_nlp()
    if nlp is None:
        return None
    doc = nlp(text)
    for ent in doc.ents:
        if ent.label_ == "GPE":
            return ent.text
    return None


def _extract_location_keyword(text_lower: str) -> Optional[str]:
    """Fallback: check if any US state name appears in the text."""
    for state_name in STATE_ABBREVS:
        if state_name in text_lower:
            return state_name.title()
    return None


def _state_abbrev(location: Optional[str]) -> Optional[str]:
    if location is None:
        return None
    return STATE_ABBREVS.get(location.lower())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_icp(
    plain_text: str,
    *,
    industry_override: Optional[str] = None,
    location_override: Optional[str] = None,
    titles_override: Optional[list[str]] = None,
) -> ICPResult:
    """
    Parse a plain-English ICP description into structured fields.

    Form-field overrides take priority over NLP extraction — this is the
    "dropdown wins" behaviour described in the spec.  When the NLP layer
    misses something the override fills the gap gracefully.
    """
    text_lower = plain_text.lower().strip()
    result = ICPResult()

    # --- Industry ---
    nlp_industry = _extract_industry(text_lower)
    if industry_override and industry_override not in ("", "— auto-detected —"):
        result.industry = industry_override  # dropdown wins
    elif nlp_industry:
        result.industry = nlp_industry
    logger.debug(f"Industry: NLP={nlp_industry}, override={industry_override}, final={result.industry}")

    # --- Location ---
    nlp_location = _extract_location_spacy(plain_text) or _extract_location_keyword(text_lower)
    if location_override and location_override.strip():
        result.location = location_override.strip()
    elif nlp_location:
        result.location = nlp_location
    result.state_abbrev = _state_abbrev(result.location)
    logger.debug(f"Location: NLP={nlp_location}, override={location_override}, final={result.location}")

    # --- Titles ---
    nlp_titles = _extract_titles(text_lower)
    if titles_override:
        result.target_titles = [t.strip().lower() for t in titles_override if t.strip()]
    elif nlp_titles:
        result.target_titles = nlp_titles
    else:
        result.target_titles = ["owner", "ceo"]  # sensible default
    logger.debug(f"Titles: NLP={nlp_titles}, override={titles_override}, final={result.target_titles}")

    # --- Company type ---
    result.company_type = _extract_company_type(text_lower) or "company"

    return result
