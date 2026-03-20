"""
Google Maps scraper — uses Playwright in headless mode to scroll through
the Maps search results panel and extract business listings.

Anti-detection measures:
  - Random viewport size per session (picked from config.VIEWPORT_SIZES)
  - Realistic desktop User-Agent string
  - Randomised scroll delay between MAPS_SCROLL_DELAY_MIN and _MAX
  - Stops after 3 consecutive scroll attempts that yield no new results

Retry logic:
  - 45-second delay + up to 3 retries on page load failures
"""

from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import Callable, Optional

from loguru import logger

import config
from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES


async def scrape_google_maps(
    industry: str,
    location: str,
    target_count: int = 200,
    log_callback: Optional[Callable] = None,
    *,
    debug: bool = False,
) -> list[dict]:
    """
    Search Google Maps for *industry* in *location* and return up to
    *target_count* business listings.

    Each result dict has keys:
        business_name, website_url, phone, address, category, source
    """
    results: list[dict] = []
    query = f"{industry} {location}"
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/maps/search/{encoded}"

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    _log(f"[Google Maps] Searching: {query}")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        _log("[Google Maps] playwright not installed — skipping", "error")
        return results

    # Retry wrapper: retries the entire Maps scrape on transient failures
    # (page load timeout, unexpected crash, etc.) with 45s delay between attempts
    for attempt in range(MAX_RETRIES + 1):
        try:
            results = await _do_maps_scrape(
                url, query, target_count, _log, debug, async_playwright
            )
            break  # success — exit retry loop
        except Exception as exc:
            if attempt < MAX_RETRIES:
                _log(
                    f"[Google Maps] Error on attempt {attempt+1}/{MAX_RETRIES+1}: {exc} "
                    f"— retrying in {UTIL_RETRY_DELAY}s…",
                    "error",
                )
                await asyncio.sleep(UTIL_RETRY_DELAY)
            else:
                _log(f"[Google Maps] All {MAX_RETRIES+1} attempts failed: {exc} — skipping", "error")
                return []

    # Clean up internal keys
    for r in results:
        r.pop("_maps_href", None)

    _log(f"[Google Maps] Extracted {len(results)} businesses", "success")
    return results


async def _do_maps_scrape(url, query, target_count, _log, debug, async_playwright):
    """Inner scrape logic — isolated so the retry loop can wrap it."""
    results = []

    async with async_playwright() as pw:
        viewport = random.choice(config.VIEWPORT_SIZES)
        ua = random.choice(config.USER_AGENTS)

        browser = await pw.chromium.launch(headless=not debug)
        context = await browser.new_context(viewport=viewport, user_agent=ua)
        page = await context.new_page()

        _log(f"[Google Maps] Navigating to Maps search ({viewport['width']}x{viewport['height']})")
        await page.goto(url, timeout=30_000, wait_until="domcontentloaded")

        # Wait for the results feed to appear
        feed_selector = 'div[role="feed"]'
        try:
            await page.wait_for_selector(feed_selector, timeout=15_000)
        except Exception:
            _log("[Google Maps] Results panel did not load — may be blocked or empty", "error")
            await browser.close()
            return results

        # Scroll the results panel to load more listings
        no_new_count = 0
        previous_count = 0

        while len(results) < target_count and no_new_count < 3:
            # Randomised delay between scrolls — anti-detection
            delay = random.uniform(config.MAPS_SCROLL_DELAY_MIN, config.MAPS_SCROLL_DELAY_MAX)
            await asyncio.sleep(delay)

            await page.evaluate("""
                const feed = document.querySelector('div[role="feed"]');
                if (feed) feed.scrollTop = feed.scrollHeight;
            """)
            await asyncio.sleep(2)

            items = await page.query_selector_all('div[role="feed"] > div > div > a')
            current_count = len(items)

            if current_count <= previous_count:
                no_new_count += 1
            else:
                no_new_count = 0
            previous_count = current_count

        _log(f"[Google Maps] Found {previous_count} raw listings, extracting details…")

        # Extract data from each listing link
        items = await page.query_selector_all('div[role="feed"] > div > div > a')
        for item in items[:target_count]:
            try:
                aria = await item.get_attribute("aria-label") or ""
                href = await item.get_attribute("href") or ""
                results.append({
                    "business_name": aria.strip(),
                    "website_url": "",
                    "phone": "",
                    "address": "",
                    "category": "",
                    "source": "google_maps",
                    "_maps_href": href,
                })
            except Exception:
                continue

        # Extract details from individual listings
        detail_limit = min(len(results), target_count)
        items_for_detail = await page.query_selector_all('div[role="feed"] > div > div > a')
        for i, item in enumerate(items_for_detail[:detail_limit]):
            try:
                await item.click()
                await asyncio.sleep(random.uniform(1.5, 3.0))

                website_el = await page.query_selector('a[data-item-id="authority"]')
                if website_el:
                    results[i]["website_url"] = (await website_el.get_attribute("href")) or ""

                phone_el = await page.query_selector('button[data-item-id^="phone:"]')
                if phone_el:
                    results[i]["phone"] = (await phone_el.inner_text()).strip()

                addr_el = await page.query_selector('button[data-item-id="address"]')
                if addr_el:
                    results[i]["address"] = (await addr_el.inner_text()).strip()

                cat_el = await page.query_selector('button[jsaction*="category"]')
                if cat_el:
                    results[i]["category"] = (await cat_el.inner_text()).strip()

            except Exception:
                continue

        await browser.close()

    return results
