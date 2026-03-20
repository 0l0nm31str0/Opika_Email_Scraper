"""
Yelp scraper — uses Playwright (headless Chromium) to paginate through
Yelp search results and extract business listings.

Playwright is required because Yelp returns 403 to plain HTTP clients.

Rate-limiting:
  - 3–5 second random delay between page requests
  - Rotating User-Agent strings + random viewport
  - 45-second retry on blocks, up to 3 retries per page
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Callable, Optional
from urllib.parse import urlparse

from loguru import logger

import config
from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES


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

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        _log("[Yelp] playwright not installed — skipping", "error")
        return results

    async with async_playwright() as pw:
        viewport = random.choice(config.VIEWPORT_SIZES)
        ua = random.choice(config.USER_AGENTS)

        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport=viewport, user_agent=ua)
        page = await context.new_page()

        offset = 0
        page_size = 10  # Yelp shows ~10 results per page
        consecutive_empty = 0

        while len(results) < target_count and consecutive_empty < 2:
            url = f"https://www.yelp.com/search?find_desc={_url_encode(industry)}&find_loc={_url_encode(location)}&start={offset}"

            try:
                blocked = False
                for retry_attempt in range(MAX_RETRIES + 1):
                    resp = await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.0, 4.0))

                    status = resp.status if resp else 0
                    content = await page.content()

                    if status == 403 or status == 503 or "unusual activity" in content.lower():
                        if retry_attempt < MAX_RETRIES:
                            _log(
                                f"[Yelp] HTTP {status} on attempt {retry_attempt+1}/{MAX_RETRIES+1} "
                                f"— retrying in {UTIL_RETRY_DELAY}s…",
                                "error",
                            )
                            await asyncio.sleep(UTIL_RETRY_DELAY)
                            continue
                        else:
                            _log(f"[Yelp] HTTP {status} after {MAX_RETRIES+1} attempts — skipping Yelp", "error")
                            blocked = True
                            break
                    else:
                        break

                if blocked:
                    break

                if resp and resp.status != 200:
                    _log(f"[Yelp] HTTP {resp.status} — skipping", "error")
                    break

                # Extract business listings from the rendered page
                # Try multiple selectors since Yelp changes markup frequently
                cards = await page.query_selector_all('[data-testid="serp-ia-card"]')
                if not cards:
                    cards = await page.query_selector_all('div.container__09f24__mpR8_')
                if not cards:
                    # Broader fallback: any search result with a biz link
                    cards = await page.query_selector_all('li h3 a[href*="/biz/"], li h4 a[href*="/biz/"]')

                if not cards:
                    consecutive_empty += 1
                    _log(f"[Yelp] No listings found on page offset={offset}")
                    offset += page_size
                    await asyncio.sleep(random.uniform(3.0, 5.0))
                    continue

                consecutive_empty = 0
                page_results = 0

                for card in cards:
                    try:
                        # Try to find business name link
                        biz_link = await card.query_selector('a[href*="/biz/"]')
                        if not biz_link:
                            # Card itself might be the link
                            href = await card.get_attribute("href") or ""
                            if "/biz/" in href:
                                name = await card.inner_text()
                            else:
                                continue
                        else:
                            name = await biz_link.inner_text()
                            href = await biz_link.get_attribute("href") or ""

                        name = name.strip()
                        if not name:
                            continue

                        # Build full Yelp biz URL for detail enrichment
                        biz_url = ""
                        if href and "/biz/" in href:
                            biz_url = href if href.startswith("http") else f"https://www.yelp.com{href}"

                        results.append({
                            "business_name": name,
                            "website_url": "",
                            "phone": "",
                            "address": "",
                            "category": industry,
                            "source": "yelp",
                            "_yelp_biz_url": biz_url,
                        })
                        page_results += 1

                    except Exception:
                        continue

                _log(f"[Yelp] Page offset={offset}: {page_results} new listings (total: {len(results)})")

                if page_results == 0:
                    consecutive_empty += 1

                offset += page_size
                # Rate-limit: 3-5 second delay between pages
                await asyncio.sleep(random.uniform(3.0, 5.0))

            except Exception as exc:
                _log(f"[Yelp] Error at offset={offset}: {exc}", "error")
                break

        # --- Detail enrichment: visit biz pages for website/phone ---
        biz_with_urls = [r for r in results if r.get("_yelp_biz_url")]
        detail_limit = min(len(biz_with_urls), 30)
        if detail_limit > 0:
            _log(f"[Yelp] Enriching details for {detail_limit} businesses…")
            for r in biz_with_urls[:detail_limit]:
                try:
                    biz_url = r["_yelp_biz_url"]
                    await page.goto(biz_url, timeout=20_000, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                    # Check for blocking
                    content = await page.content()
                    if "unusual activity" in content.lower():
                        _log("[Yelp] Blocked during enrichment — stopping detail extraction", "error")
                        break

                    # Extract website
                    website_el = await page.query_selector('a[href*="biz_redir"]')
                    if not website_el:
                        website_el = await page.query_selector('p:has-text("Business website") + p a')
                    if not website_el:
                        # Try external link that's not yelp
                        all_links = await page.query_selector_all('a[href^="http"]')
                        for link in all_links:
                            href = await link.get_attribute("href") or ""
                            if href and "yelp.com" not in href and "google.com" not in href:
                                text = await link.inner_text()
                                if text and len(text) < 60:
                                    website_el = link
                                    break
                    if website_el:
                        website = await website_el.get_attribute("href") or ""
                        if website and "yelp.com" not in website:
                            r["website_url"] = website
                            domain = _extract_domain(website)
                            if domain:
                                existing_domains.add(domain)

                    # Extract phone
                    phone_el = await page.query_selector('p:has-text("Phone number") + p')
                    if not phone_el:
                        phone_el = await page.query_selector('[href^="tel:"]')
                    if phone_el:
                        phone_text = await phone_el.inner_text()
                        r["phone"] = phone_text.strip()

                    # Extract address
                    addr_el = await page.query_selector('address')
                    if not addr_el:
                        addr_el = await page.query_selector('a[href*="map"] p')
                    if addr_el:
                        r["address"] = (await addr_el.inner_text()).strip()

                except Exception:
                    continue

                await asyncio.sleep(random.uniform(2.0, 4.0))

        await browser.close()

    # Clean up internal keys
    for r in results:
        r.pop("_yelp_biz_url", None)

    _log(f"[Yelp] Extracted {len(results)} businesses", "success")
    return results


def _url_encode(text: str) -> str:
    """Simple URL encoding for query params."""
    import urllib.parse
    return urllib.parse.quote_plus(text)


def _extract_domain(url: str) -> str:
    """Pull the bare domain from a URL."""
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        return parsed.netloc.lower().replace("www.", "")
    except Exception:
        return url.lower()
