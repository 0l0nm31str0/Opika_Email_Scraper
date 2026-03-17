"""
Google Search dork scraper — extracts URLs and inline email addresses
from Google SERP pages using httpx with realistic browser headers.

NO Google Custom Search API — direct SERP scraping only.

Rate-limiting:
  - Minimum GOOGLE_DORK_DELAY seconds between queries
  - 45-second retry on 429/500/529/CAPTCHA, up to 3 retries per query
  - Randomised delay added on top of the base delay
"""

from __future__ import annotations

import asyncio
import random
import re
import urllib.parse
from typing import Callable, Optional

import httpx
from bs4 import BeautifulSoup
from loguru import logger

import config
from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES, RETRYABLE_STATUS_CODES

# Email regex — matches most standard email formats
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _build_queries(industry: str, location: str) -> list[str]:
    """Generate Google dork query templates for the given ICP."""
    return [
        f"{industry} {location} contact email",
        f"{industry} {location} owner email site:.com",
        f"{industry} {location} -site:yelp.com -site:yellowpages.com",
    ]


async def scrape_google_dorks(
    industry: str,
    location: str,
    existing_domains: Optional[set[str]] = None,
    log_callback: Optional[Callable] = None,
) -> tuple[list[dict], list[str]]:
    """
    Run dork queries against Google Search and return:
      1. List of discovered company dicts (with any inline emails found)
      2. List of new domain URLs for the website crawler

    Returns (companies, new_domains).
    """
    companies: list[dict] = []
    new_domains: list[str] = []
    found_emails: list[str] = []
    existing_domains = existing_domains or set()

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    queries = _build_queries(industry, location)
    _log(f"[Google Dork] Running {len(queries)} dork queries")

    headers = {
        "User-Agent": random.choice(config.USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
    }

    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT, follow_redirects=True) as client:
        for i, query in enumerate(queries):
            _log(f"[Google Dork] Query {i+1}/{len(queries)}: {query}")

            encoded = urllib.parse.quote_plus(query)
            url = f"https://www.google.com/search?q={encoded}&num=50"

            try:
                # 45-second retry logic for retryable HTTP errors and CAPTCHAs
                resp = None
                gave_up = False
                for retry_attempt in range(MAX_RETRIES + 1):
                    resp = await client.get(url, headers=headers)

                    is_captcha = "captcha" in resp.text.lower()
                    is_retryable = resp.status_code in RETRYABLE_STATUS_CODES or is_captcha

                    if is_retryable:
                        if retry_attempt < MAX_RETRIES:
                            _log(
                                f"[Google Dork] {'CAPTCHA' if is_captcha else f'HTTP {resp.status_code}'} "
                                f"on attempt {retry_attempt+1}/{MAX_RETRIES+1} — retrying in {UTIL_RETRY_DELAY}s…",
                                "error",
                            )
                            await asyncio.sleep(UTIL_RETRY_DELAY)
                            continue
                        else:
                            _log(f"[Google Dork] Still blocked after {MAX_RETRIES+1} attempts — skipping remaining queries", "error")
                            gave_up = True
                            break
                    else:
                        break

                if gave_up:
                    break  # skip remaining dork queries entirely

                if resp is None or resp.status_code != 200:
                    _log(f"[Google Dork] HTTP {resp.status_code if resp else 'N/A'} — skipping query", "error")
                    continue

                soup = BeautifulSoup(resp.text, "lxml")

                # Extract result URLs
                for link in soup.select("a"):
                    href = link.get("href", "")
                    # Google wraps results in /url?q=... redirects
                    if href.startswith("/url?q="):
                        actual_url = href.split("/url?q=")[1].split("&")[0]
                        actual_url = urllib.parse.unquote(actual_url)
                        domain = _extract_domain(actual_url)
                        if domain and domain not in existing_domains:
                            existing_domains.add(domain)
                            new_domains.append(actual_url)

                # Extract emails from the entire page text (snippets)
                page_text = soup.get_text()
                emails_on_page = EMAIL_RE.findall(page_text)
                for email in emails_on_page:
                    email = email.lower()
                    if email not in found_emails:
                        found_emails.append(email)
                        domain = email.split("@")[1]
                        companies.append({
                            "business_name": "",
                            "website_url": "",
                            "email": email,
                            "company_domain": domain,
                            "phone": "",
                            "address": "",
                            "category": industry,
                            "source": "google_dork",
                        })

                _log(f"[Google Dork] Query {i+1}: {len(emails_on_page)} emails, {len(new_domains)} new domains")

            except httpx.TimeoutException:
                _log(f"[Google Dork] Timeout on query {i+1} — skipping", "error")
            except Exception as exc:
                _log(f"[Google Dork] Error on query {i+1}: {exc}", "error")

            # Rate-limit: at least GOOGLE_DORK_DELAY seconds + some jitter
            delay = config.GOOGLE_DORK_DELAY + random.uniform(1.0, 4.0)
            await asyncio.sleep(delay)

    _log(f"[Google Dork] Total: {len(companies)} email leads, {len(new_domains)} new domains discovered", "success")
    return companies, new_domains


def _extract_domain(url: str) -> str:
    """Pull the bare domain from a URL, filtering out noise."""
    try:
        parsed = urllib.parse.urlparse(url if url.startswith("http") else f"https://{url}")
        domain = parsed.netloc.lower().replace("www.", "")
        # Skip known non-company domains
        skip_domains = {
            "google.com", "youtube.com", "facebook.com", "yelp.com",
            "yellowpages.com", "bbb.org", "linkedin.com", "twitter.com",
            "instagram.com", "pinterest.com", "wikipedia.org", "reddit.com",
            "bing.com", "yahoo.com", "amazon.com", "apple.com",
        }
        if domain in skip_domains or not domain:
            return ""
        return domain
    except Exception:
        return ""
