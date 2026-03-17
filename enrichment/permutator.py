"""
Email permutation generator — when a person's name is found on a website
but no email address, generate common email format permutations and verify
via SMTP.

Only runs when validate_level = "deep".
Capped at MAX_PERMUTATIONS attempts per contact per domain.
Stops at the first permutation that returns SMTP 250 OK.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

import config


def generate_permutations(
    first_name: str,
    last_name: str,
    domain: str,
    max_perms: Optional[int] = None,
) -> list[str]:
    """
    Generate common email permutations for a person at a domain.

    Example: first="John", last="Smith", domain="company.com" →
        john.smith@company.com
        johnsmith@company.com
        john@company.com
        jsmith@company.com
        j.smith@company.com
    """
    if not first_name or not last_name or not domain:
        return []

    first = first_name.lower().strip()
    last = last_name.lower().strip()
    domain = domain.lower().strip()
    max_perms = max_perms or config.MAX_PERMUTATIONS

    permutations = [
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first[0]}.{last}@{domain}",
    ]

    return permutations[:max_perms]


async def enrich_with_permutations(
    contacts: list[dict],
    smtp_verify_func,
    log_callback=None,
) -> list[dict]:
    """
    For contacts that have a name but no email, generate permutations
    and SMTP-verify them.  Returns new contact dicts for verified emails.

    *smtp_verify_func* should be an async function that takes an email
    and returns a status string: "VALID", "INVALID", "CATCH_ALL", "UNKNOWN".
    """
    new_contacts: list[dict] = []

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    # Filter to contacts with name but no email
    candidates = [
        c for c in contacts
        if c.get("first_name") and c.get("last_name")
        and not c.get("email")
        and c.get("company_domain")
    ]

    if not candidates:
        return new_contacts

    _log(f"[Permutator] Generating permutations for {len(candidates)} contacts")

    for contact in candidates:
        perms = generate_permutations(
            contact["first_name"],
            contact["last_name"],
            contact["company_domain"],
        )

        for perm in perms:
            try:
                status = await smtp_verify_func(perm)
                if status == "VALID":
                    _log(f"[Permutator] Verified: {perm}", "success")
                    new_contact = {**contact, "email": perm, "source": "permutation"}
                    new_contacts.append(new_contact)
                    break  # Stop at first valid permutation
            except Exception as exc:
                logger.debug(f"[Permutator] SMTP error for {perm}: {exc}")
                continue

    _log(f"[Permutator] Found {len(new_contacts)} verified emails via permutation", "success")
    return new_contacts
