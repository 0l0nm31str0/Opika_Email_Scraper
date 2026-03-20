"""
Async website email crawler — visits common contact/team pages on each
discovered domain and extracts email addresses + person names.

Concurrency: CRAWLER_CONCURRENCY domains in parallel via asyncio.
Per-domain delay: 1–2 seconds between requests to the same domain.
Timeout: REQUEST_TIMEOUT per request.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

import config
from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES, RETRYABLE_STATUS_CODES

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Simple pattern to detect person names near emails (First Last format)
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20})\b")

# Domains that are never real company email domains
_FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "yandex.com", "zoho.com",
    "live.com", "msn.com", "me.com", "gmx.com", "fastmail.com",
}

# Image / asset file extensions to skip
_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".map",
    ".pdf", ".zip", ".mp4", ".mp3",
}


async def crawl_domains(
    domains: list[str],
    log_callback: Optional[Callable] = None,
) -> list[dict]:
    """
    Crawl a list of domains/URLs for email addresses and person names.

    Returns a list of contact dicts with keys:
        email, first_name, last_name, company_domain, source
    """
    contacts: list[dict] = []
    seen_emails: set[str] = set()

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    _log(f"[Website Crawler] Starting crawl of {len(domains)} domains")

    sem = asyncio.Semaphore(config.CRAWLER_CONCURRENCY)

    async def _crawl_one(domain: str):
        async with sem:
            domain_contacts = await _crawl_domain(domain, _log)
            for c in domain_contacts:
                if c["email"] not in seen_emails:
                    seen_emails.add(c["email"])
                    contacts.append(c)

    tasks = [_crawl_one(d) for d in domains]
    await asyncio.gather(*tasks, return_exceptions=True)

    _log(f"[Website Crawler] Found {len(contacts)} unique email contacts", "success")
    return contacts


async def _crawl_domain(domain: str, _log: Callable) -> list[dict]:
    """Crawl a single domain's contact/team pages."""
    contacts: list[dict] = []
    base_url = _normalise_url(domain)
    parsed_domain = urlparse(base_url).netloc.replace("www.", "")

    headers = {
        "User-Agent": random.choice(config.USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(
        timeout=15.0,  # Longer timeout for small business sites
        follow_redirects=True,
        headers=headers,
    ) as client:
        for path in config.CRAWL_PATHS:
            url = urljoin(base_url, path)
            try:
                # 45-second retry logic for retryable status codes
                resp = None
                for retry_attempt in range(MAX_RETRIES + 1):
                    resp = await client.get(url)
                    if resp.status_code in RETRYABLE_STATUS_CODES:
                        if retry_attempt < MAX_RETRIES:
                            logger.debug(
                                f"[Crawler] HTTP {resp.status_code} on {url} "
                                f"(attempt {retry_attempt+1}/{MAX_RETRIES+1}) — retrying in {UTIL_RETRY_DELAY}s"
                            )
                            await asyncio.sleep(UTIL_RETRY_DELAY)
                            continue
                        else:
                            break
                    else:
                        break

                if resp is None or resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "lxml")
                page_text = soup.get_text(separator=" ")

                # --- Extract mailto: links ---
                for a in soup.select("a[href^='mailto:']"):
                    raw = a["href"].replace("mailto:", "").split("?")[0].strip().lower()
                    if not EMAIL_RE.match(raw):
                        continue
                    email_domain = raw.split("@")[1]
                    is_company = email_domain == parsed_domain
                    is_generic = raw.split("@")[0] in config.GENERIC_PREFIXES
                    is_freemail = email_domain in _FREEMAIL_DOMAINS

                    if is_company and not is_generic:
                        # Personal email on company domain — best quality
                        name = _guess_name_from_context(a, soup)
                        contacts.append({
                            "email": raw,
                            "first_name": name[0],
                            "last_name": name[1],
                            "company_domain": parsed_domain,
                            "source": "website_crawl",
                        })
                    elif is_company and is_generic:
                        # Generic company email (info@, contact@) — still useful
                        contacts.append({
                            "email": raw,
                            "first_name": "",
                            "last_name": "",
                            "company_domain": parsed_domain,
                            "source": "website_crawl",
                        })
                    elif not is_freemail and not is_generic:
                        # Email on a different non-freemail domain — could be
                        # a subsidiary or partner, still capture it
                        name = _guess_name_from_context(a, soup)
                        contacts.append({
                            "email": raw,
                            "first_name": name[0],
                            "last_name": name[1],
                            "company_domain": email_domain,
                            "source": "website_crawl",
                        })

                # --- Regex extract from page text ---
                for email in EMAIL_RE.findall(page_text):
                    email = email.lower()
                    # Skip obvious non-emails (image filenames, CSS classes, etc.)
                    if any(email.endswith(ext) for ext in _SKIP_EXTENSIONS):
                        continue
                    email_domain = email.split("@")[1]
                    is_freemail = email_domain in _FREEMAIL_DOMAINS
                    prefix = email.split("@")[0]

                    # Already captured via mailto?
                    if any(c["email"] == email for c in contacts):
                        continue

                    if email_domain == parsed_domain:
                        # On company domain — accept all (personal + generic)
                        contacts.append({
                            "email": email,
                            "first_name": "",
                            "last_name": "",
                            "company_domain": parsed_domain,
                            "source": "website_crawl",
                        })
                    elif not is_freemail and prefix not in config.GENERIC_PREFIXES:
                        # Non-freemail, non-generic on different domain
                        contacts.append({
                            "email": email,
                            "first_name": "",
                            "last_name": "",
                            "company_domain": email_domain,
                            "source": "website_crawl",
                        })

                # --- Extract person names from team/staff sections ---
                _extract_names_near_emails(soup, contacts, parsed_domain)

            except (httpx.TimeoutException, httpx.ConnectError):
                continue
            except Exception as exc:
                logger.debug(f"[Crawler] Error on {url}: {exc}")
                continue

            # Per-domain delay: 1-2 seconds between requests
            await asyncio.sleep(random.uniform(1.0, 2.0))

    return contacts


def _normalise_url(domain: str) -> str:
    """Ensure domain is a full URL."""
    if domain.startswith("http"):
        return domain
    return f"https://{domain}"


def _guess_name_from_context(anchor_el, soup) -> tuple[str, str]:
    """Try to extract a person's name from near a mailto link."""
    # Check the anchor text itself
    text = anchor_el.get_text(strip=True)
    if text and "@" not in text:
        parts = text.split()
        if len(parts) >= 2:
            return (parts[0].title(), parts[-1].title())

    # Check parent element
    parent = anchor_el.parent
    if parent:
        siblings_text = parent.get_text(strip=True)
        match = NAME_RE.search(siblings_text)
        if match:
            return (match.group(1), match.group(2))

    return ("", "")


def _extract_names_near_emails(soup, contacts: list[dict], domain: str):
    """
    Look for name patterns near email addresses in the page HTML.
    Fills in first_name/last_name for contacts that are missing names.
    """
    body_text = soup.get_text(separator="\n")
    lines = body_text.split("\n")

    for contact in contacts:
        if contact["first_name"] and contact["last_name"]:
            continue
        email = contact["email"]
        # Find lines near where the email appears
        for i, line in enumerate(lines):
            if email in line.lower():
                # Search surrounding lines for a name
                context_start = max(0, i - 3)
                context_end = min(len(lines), i + 3)
                context = " ".join(lines[context_start:context_end])
                match = NAME_RE.search(context)
                if match:
                    contact["first_name"] = match.group(1)
                    contact["last_name"] = match.group(2)
                    break
