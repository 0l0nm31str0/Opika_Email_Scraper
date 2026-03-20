"""
Google Search dork scraper — extracts URLs and inline email addresses
from Google SERP pages using Playwright for JS rendering.

Uses Playwright (headless Chromium) to handle Google's consent pages,
JavaScript rendering, and anti-bot measures.

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

from loguru import logger

import config
from utils.retry import RETRY_DELAY as UTIL_RETRY_DELAY, MAX_RETRIES

# Email regex — matches most standard email formats
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _build_queries(industry: str, location: str, titles: list[str] | None = None) -> list[str]:
    """Generate Google dork query templates for the given ICP."""
    queries = [
        f'"{industry}" "{location}" email "@" contact',
        f'"{industry}" "{location}" "owner" OR "ceo" email',
        f'"{industry}" "{location}" "@gmail.com" OR "@yahoo.com" OR "@outlook.com"',
        f'site:facebook.com "{industry}" "{location}" email',
        f'"{industry}" near me "{location}" contact email phone',
    ]
    if titles:
        title_str = " OR ".join(f'"{t}"' for t in titles[:3])
        queries.append(f'"{industry}" "{location}" {title_str} email')
    return queries


async def scrape_google_dorks(
    industry: str,
    location: str,
    existing_domains: Optional[set[str]] = None,
    log_callback: Optional[Callable] = None,
    titles: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """
    Run dork queries against Google Search and return:
      1. List of discovered company dicts (with any inline emails found)
      2. List of new domain URLs for the website crawler

    Returns (companies, new_domains).
    """
    companies: list[dict] = []
    new_domains: list[str] = []
    found_emails: set[str] = set()
    existing_domains = existing_domains or set()

    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    queries = _build_queries(industry, location, titles)
    _log(f"[Google Dork] Running {len(queries)} dork queries")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        _log("[Google Dork] playwright not installed — skipping", "error")
        return companies, new_domains

    async with async_playwright() as pw:
        viewport = random.choice(config.VIEWPORT_SIZES)
        ua = random.choice(config.USER_AGENTS)

        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport=viewport, user_agent=ua)
        page = await context.new_page()

        for i, query in enumerate(queries):
            _log(f"[Google Dork] Query {i+1}/{len(queries)}: {query}")

            encoded = urllib.parse.quote_plus(query)
            url = f"https://www.google.com/search?q={encoded}&num=50"

            try:
                gave_up = False
                for retry_attempt in range(MAX_RETRIES + 1):
                    await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.0, 4.0))

                    content = await page.content()
                    is_captcha = "captcha" in content.lower() or "unusual traffic" in content.lower()
                    is_consent = "consent.google" in content.lower() or "Before you continue" in content.lower()

                    # Handle Google consent page
                    if is_consent:
                        try:
                            accept_btn = await page.query_selector('button:has-text("Accept all")')
                            if not accept_btn:
                                accept_btn = await page.query_selector('button:has-text("I agree")')
                            if not accept_btn:
                                accept_btn = await page.query_selector('button:has-text("Accept")')
                            if accept_btn:
                                await accept_btn.click()
                                await asyncio.sleep(3)
                                content = await page.content()
                                is_captcha = "captcha" in content.lower() or "unusual traffic" in content.lower()
                        except Exception:
                            pass

                    if is_captcha:
                        if retry_attempt < MAX_RETRIES:
                            _log(
                                f"[Google Dork] CAPTCHA on attempt {retry_attempt+1}/{MAX_RETRIES+1} "
                                f"— retrying in {UTIL_RETRY_DELAY}s…",
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
                    break

                # Extract result URLs from the rendered page
                links = await page.query_selector_all("a[href]")
                query_new_domains = 0
                for link in links:
                    href = await link.get_attribute("href") or ""

                    # Google wraps results in /url?q=... redirects
                    actual_url = ""
                    if href.startswith("/url?q="):
                        actual_url = href.split("/url?q=")[1].split("&")[0]
                        actual_url = urllib.parse.unquote(actual_url)
                    elif href.startswith("http") and "google.com" not in href:
                        actual_url = href

                    if actual_url:
                        domain = _extract_domain(actual_url)
                        if domain and domain not in existing_domains:
                            existing_domains.add(domain)
                            new_domains.append(actual_url)
                            query_new_domains += 1

                # Extract emails from the rendered page text
                page_text = await page.inner_text("body")
                emails_on_page = EMAIL_RE.findall(page_text)
                new_email_count = 0
                for email in emails_on_page:
                    email = email.lower()
                    # Skip obvious non-emails
                    if email.endswith((".png", ".jpg", ".js", ".css", ".svg")):
                        continue
                    if email not in found_emails:
                        found_emails.add(email)
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
                        new_email_count += 1

                _log(f"[Google Dork] Query {i+1}: {new_email_count} emails, {query_new_domains} new domains")

            except Exception as exc:
                _log(f"[Google Dork] Error on query {i+1}: {exc}", "error")

            # Rate-limit: at least GOOGLE_DORK_DELAY seconds + some jitter
            delay = config.GOOGLE_DORK_DELAY + random.uniform(2.0, 6.0)
            await asyncio.sleep(delay)

        await browser.close()

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
            "maps.google.com", "accounts.google.com", "support.google.com",
            "play.google.com", "translate.google.com",
        }
        if domain in skip_domains or not domain:
            return ""
        return domain
    except Exception:
        return ""
