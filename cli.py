"""
CLI entry point for Opika Leads — supports --resume to pick up from a
partial CSV, and direct command-line runs without the web UI.

Usage:
    python cli.py --query "HVAC owners in Florida" --count 200
    python cli.py --resume <run_id>
    python cli.py --domains path/to/domains.txt --titles "Owner,CEO"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

from loguru import logger

import config
from icp_parser import parse_icp
from sources.google_maps import scrape_google_maps
from sources.yelp import scrape_yelp
from sources.google_dork import scrape_google_dorks
from sources.website_crawler import crawl_domains
from enrichment.permutator import enrich_with_permutations
from validation.syntax import is_valid_syntax, clean_email
from validation.mx_check import has_mx
from validation.smtp_check import verify_email, verify_emails_batch
from pipeline.dedup import deduplicate
from pipeline.scoring import score_all
from pipeline.filters import apply_filters, infer_company_signals
from output.exporter import export, save_partial, load_partial

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


def log(msg: str, status: str = "info"):
    """Console log with status icon."""
    icons = {"success": "✓", "error": "✕", "info": "→"}
    icon = icons.get(status, "→")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  {ts}  {icon}  {msg}")


def main():
    parser = argparse.ArgumentParser(description="Opika Leads — CLI email lead generator")
    parser.add_argument("--query", "-q", type=str, help="Plain English ICP description")
    parser.add_argument("--industry", type=str, default="")
    parser.add_argument("--location", type=str, default="")
    parser.add_argument("--titles", type=str, default="Owner,CEO", help="Comma-separated job titles")
    parser.add_argument("--count", type=int, default=200, help="Target lead count")
    parser.add_argument("--validate", choices=["deep", "basic"], default="deep")
    parser.add_argument("--sources", choices=["all", "maps", "yelp", "crawl"], default="all")
    parser.add_argument("--format", choices=["csv", "json", "both"], default="csv")
    parser.add_argument("--filename", type=str, default="leads_output")
    parser.add_argument("--domains", type=str, help="Path to domain list file")
    parser.add_argument("--resume", type=str, help="Resume from a partial run (run_id)")
    parser.add_argument("--debug", action="store_true", help="Run playwright in non-headless mode")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- Resume mode ---
    if args.resume:
        log(f"Resuming from partial run: {args.resume}")
        contacts = load_partial(args.resume, OUTPUT_DIR)
        if not contacts:
            log(f"No partial file found for run_id={args.resume}", "error")
            sys.exit(1)
        log(f"Loaded {len(contacts)} contacts from partial file", "success")
        # Skip to scoring and export
        scored = score_all(contacts, args.titles.split(","), log)
        files = export(scored, args.filename, args.format.upper(), OUTPUT_DIR, log)
        log(f"Export complete: {', '.join(files)}", "success")
        return

    if not args.query and not args.domains:
        parser.error("Either --query or --domains is required")

    # --- Parse ICP ---
    log("Parsing ICP parameters…")
    icp = parse_icp(
        args.query or "",
        industry_override=args.industry,
        location_override=args.location,
        titles_override=args.titles.split(","),
    )
    industry = icp.industry or args.industry or "business"
    location = icp.location or args.location or ""
    target_titles = icp.target_titles or ["owner", "ceo"]
    log(f"ICP: industry={industry}, location={location}, titles={target_titles}", "success")

    # --- Run async pipeline ---
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    all_companies: list[dict] = []
    all_contacts: list[dict] = []
    discovered_domains: set[str] = set()

    # Domain list mode
    user_domains: list[str] = []
    if args.domains and os.path.exists(args.domains):
        with open(args.domains) as f:
            user_domains = [line.strip() for line in f if line.strip()]
        log(f"Loaded {len(user_domains)} domains from {args.domains}", "success")

    sources_map = {"all": "All sources", "maps": "Google Maps only", "yelp": "Yelp only", "crawl": "Website crawl only"}
    sources_filter = sources_map.get(args.sources, "All sources")

    # Google Maps
    if not user_domains and sources_filter in ("All sources", "Google Maps only"):
        try:
            results = loop.run_until_complete(scrape_google_maps(industry, location, args.count, log, debug=args.debug))
            all_companies.extend(results)
            for r in results:
                d = _extract_domain(r.get("website_url", ""))
                if d:
                    discovered_domains.add(d)
        except Exception as exc:
            log(f"[Google Maps] Failed: {exc}", "error")

    # Yelp
    if not user_domains and sources_filter in ("All sources", "Yelp only"):
        try:
            results = loop.run_until_complete(scrape_yelp(industry, location, args.count, discovered_domains, log))
            all_companies.extend(results)
            for r in results:
                d = _extract_domain(r.get("website_url", ""))
                if d:
                    discovered_domains.add(d)
        except Exception as exc:
            log(f"[Yelp] Failed: {exc}", "error")

    # Google Dorks
    if not user_domains and sources_filter in ("All sources",):
        try:
            companies, domains = loop.run_until_complete(scrape_google_dorks(industry, location, discovered_domains, log))
            all_contacts.extend(companies)
            for url in domains:
                d = _extract_domain(url)
                if d:
                    discovered_domains.add(d)
        except Exception as exc:
            log(f"[Google Dork] Failed: {exc}", "error")

    # Website Crawler
    crawl_targets = user_domains or list(discovered_domains)
    if sources_filter in ("All sources", "Website crawl only") and crawl_targets:
        try:
            contacts = loop.run_until_complete(crawl_domains(crawl_targets, log))
            all_contacts.extend(contacts)
        except Exception as exc:
            log(f"[Website Crawler] Failed: {exc}", "error")

    # Merge company data
    for c in all_contacts:
        c.setdefault("industry", industry)
        c.setdefault("location", location)
        c.setdefault("job_title", "")
        c.setdefault("company_name", "")

    log(f"Discovery: {len(discovered_domains)} domains, {len(all_contacts)} contacts")

    # Syntax check
    valid = [c for c in all_contacts if c.get("email") and is_valid_syntax(c["email"])]
    for c in valid:
        c["email"] = clean_email(c["email"])

    # MX check
    mx_valid = []
    for c in valid:
        domain = c["email"].split("@")[1]
        if has_mx(domain):
            c["validation_status"] = "MX_ONLY"
            mx_valid.append(c)

    # SMTP
    if args.validate == "deep" and mx_valid:
        emails = [c["email"] for c in mx_valid]
        log(f"SMTP verifying {len(emails)} emails…")
        results = loop.run_until_complete(verify_emails_batch(emails, log))
        for c in mx_valid:
            if c["email"] in results:
                c["validation_status"] = results[c["email"]]

    # Infer signals, filter, dedup, score, export
    for c in mx_valid:
        infer_company_signals(c)

    filtered = apply_filters(mx_valid, log_callback=log)
    deduped = deduplicate(filtered, log)
    scored = score_all(deduped, target_titles, log)
    files = export(scored, args.filename, args.format.upper(), OUTPUT_DIR, log)

    log(f"Done — {len(scored)} contacts exported to {', '.join(files)}", "success")
    loop.close()


def _extract_domain(url: str) -> str:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        return parsed.netloc.lower().replace("www.", "")
    except Exception:
        return ""


if __name__ == "__main__":
    main()
