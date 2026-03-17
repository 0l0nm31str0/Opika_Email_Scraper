"""
Yelp scraper — uses httpx (no JS rendering needed) to paginate through
Yelp search results and extract business listings.

Rate-limiting:
  - 3–5 second random delay between page requests
  - Rotating User-Agent strings
  - 45-second retry on 429/500/529, up to 3 retries per page
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


async def scrape_yelp(
    industry: str,
    location: str,
    target_count: int = 200,
    existing_domains: Optional[set[str]] = None,
    log_callback: Optional[Callable] = None,
) -> list[dict]:
    """
    Scrape Yelp search results for *industry* in *location*.

    *existing_domains* is a set of domains already found by other sources —
    duplicates (same domain) are skipped.
    """
    results: list[dict] = []
    existing_domains = existing_domains or set()

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    _log(f"[Yelp] Searching: {industry} in {location}")

    base_url = "https://www.yelp.com/search"
    offset = 0
    page_size = 24  # Yelp's default page size

    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT, follow_redirects=True) as client:
        while len(results) < target_count:
            params = {
                "find_desc": industry,
                "find_loc": location,
                "start": offset,
            }
            headers = {
                "User-Agent": random.choice(config.USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }

            try:
                # 45-second retry logic for retryable status codes (429, 500, 529)
                resp = None
                for retry_attempt in range(MAX_RETRIES + 1):
                    resp = await client.get(base_url, params=params, headers=headers)
                    if resp.status_code in RETRYABLE_STATUS_CODES:
                        if retry_attempt < MAX_RETRIES:
                            _log(
                                f"[Yelp] HTTP {resp.status_code} on attempt {retry_attempt+1}/{MAX_RETRIES+1} "
                                f"— retrying in {UTIL_RETRY_DELAY}s…",
                                "error",
                            )
                            await asyncio.sleep(UTIL_RETRY_DELAY)
                            continue
                        else:
                            _log(f"[Yelp] HTTP {resp.status_code} after {MAX_RETRIES+1} attempts — skipping Yelp", "error")
                            break
                    else:
                        break  # non-retryable status or success

                if resp is None or resp.status_code in RETRYABLE_STATUS_CODES:
                    break

                if resp.status_code != 200:
                    _log(f"[Yelp] HTTP {resp.status_code} — skipping", "error")
                    break
                soup = BeautifulSoup(resp.text, "lxml")

                # Yelp renders business cards in search results
                listings = soup.select('div[data-testid="serp-ia-card"]') or soup.select('li .css-1m051bw')
                if not listings:
                    # Try broader selector as Yelp changes markup often
                    listings = soup.select('div.container__09f24__mpR8_ a') or []
                    if not listings:
                        _log(f"[Yelp] No listings found on page offset={offset} — stopping", "info")
                        break

                page_results = 0
                for card in listings:
                    try:
                        # Business name
                        name_el = card.select_one('a[href*="/biz/"]')
                        if not name_el:
                            continue
                        name = name_el.get_text(strip=True)
                        biz_url = name_el.get("href", "")

                        # Website (not always on search page — often on biz detail)
                        website = ""
                        phone = ""
                        address = ""

                        # Try to get address from the card
                        addr_el = card.select_one('address') or card.select_one('.css-e81eai')
                        if addr_el:
                            address = addr_el.get_text(strip=True)

                        # Extract domain from Yelp biz link for dedup
                        domain = ""
                        if website:
                            domain = _extract_domain(website)
                            if domain in existing_domains:
                                continue
                            existing_domains.add(domain)

                        results.append({
                            "business_name": name,
                            "website_url": website,
                            "phone": phone,
                            "address": address,
                            "category": industry,
                            "source": "yelp",
                            "_yelp_biz_url": biz_url,
                        })
                        page_results += 1

                    except Exception:
                        continue

                _log(f"[Yelp] Page offset={offset}: {page_results} new listings (total: {len(results)})")

                if page_results == 0:
                    break

                offset += page_size
                # Rate-limit: 3-5 second delay between pages
                await asyncio.sleep(random.uniform(3.0, 5.0))

            except httpx.TimeoutException:
                _log(f"[Yelp] Timeout on offset={offset} — retrying in {UTIL_RETRY_DELAY}s", "error")
                offset += page_size
                await asyncio.sleep(UTIL_RETRY_DELAY)
                continue
            except Exception as exc:
                _log(f"[Yelp] Error: {exc}", "error")
                break

    # Clean up internal keys
    for r in results:
        r.pop("_yelp_biz_url", None)

    _log(f"[Yelp] Extracted {len(results)} businesses", "success")
    return results


def _extract_domain(url: str) -> str:
    """Pull the bare domain from a URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        return parsed.netloc.lower().replace("www.", "")
    except Exception:
        return url.lower()
