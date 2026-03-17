"""
Flask entrypoint for Opika Leads — serves the web UI and orchestrates
the scraping/validation pipeline via background threads + SSE streaming.

Routes:
  GET  /                  → serve the form UI
  POST /run               → start a scraping run, return run_id
  GET  /stream/<run_id>   → SSE log stream for a running job
  GET  /download/<filename> → serve output files for download
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from loguru import logger

import config
from icp_parser import parse_icp
from sources.google_maps import scrape_google_maps
from sources.yelp import scrape_yelp
from sources.google_dork import scrape_google_dorks
from sources.website_crawler import crawl_domains
from enrichment.permutator import enrich_with_permutations, generate_permutations
from validation.syntax import is_valid_syntax, clean_email
from validation.mx_check import has_mx
from validation.smtp_check import verify_email, verify_emails_batch
from pipeline.dedup import deduplicate
from pipeline.scoring import score_all
from pipeline.filters import apply_filters, infer_company_signals
from output.exporter import export, save_partial

app = Flask(__name__)

# In-memory run state: run_id → {"queue": Queue, "status": str, "files": [], "summary": {}}
_runs: dict[str, dict] = {}

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def start_run():
    """Accept form data, start pipeline in background, return run_id."""
    run_id = uuid.uuid4().hex[:12]
    log_queue: queue.Queue = queue.Queue()

    form = request.form.to_dict()
    titles = request.form.getlist("titles")
    if not titles:
        # Try comma-separated fallback
        titles = [t.strip() for t in form.get("titles", "Owner,CEO").split(",") if t.strip()]

    params = {
        "plain_text": form.get("plain_text", ""),
        "industry": form.get("industry", ""),
        "location": form.get("location", ""),
        "titles": titles,
        "target_count": int(form.get("target_count", 200)),
        "revenue_filter": form.get("revenue_filter", "Any"),
        "size_filter": form.get("size_filter", "Any"),
        "age_filter": form.get("age_filter", "Any"),
        "validate_level": form.get("validate_level", "deep"),
        "sources_filter": form.get("sources_filter", "All sources"),
        "output_format": form.get("output_format", "CSV"),
        "output_filename": form.get("output_filename", "leads_output") or "leads_output",
        "domain_list": form.get("domain_list", ""),
    }

    _runs[run_id] = {
        "queue": log_queue,
        "status": "running",
        "files": [],
        "summary": {},
    }

    thread = threading.Thread(target=_run_pipeline, args=(run_id, params), daemon=True)
    thread.start()

    return jsonify({"run_id": run_id})


@app.route("/stream/<run_id>")
def stream(run_id: str):
    """SSE endpoint — streams log lines to the browser."""
    run = _runs.get(run_id)
    if not run:
        return jsonify({"error": "Unknown run_id"}), 404

    def generate():
        q = run["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") == "complete":
                    break
            except queue.Empty:
                # Send keepalive to prevent connection timeout
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/download/<path:filename>")
def download(filename: str):
    """Serve output files for download."""
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Pipeline orchestration (runs in a background thread)
# ---------------------------------------------------------------------------

def _run_pipeline(run_id: str, params: dict):
    """Main scraping + validation pipeline — runs in its own thread."""
    run = _runs[run_id]
    q = run["queue"]

    def log(msg: str, status: str = "info"):
        q.put({"type": "log", "msg": msg, "status": status, "ts": datetime.now().strftime("%H:%M:%S")})
        logger.info(msg)

    try:
        # --- Step 0: Parse ICP ---
        log("Parsing ICP parameters…")
        icp = parse_icp(
            params["plain_text"],
            industry_override=params["industry"],
            location_override=params["location"],
            titles_override=params["titles"],
        )
        industry = icp.industry or params["industry"] or "business"
        location = icp.location or params["location"] or ""
        target_titles = icp.target_titles or ["owner", "ceo"]
        target_count = params["target_count"]

        log(f"ICP: industry={industry}, location={location}, titles={target_titles}", "success")

        # Async event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        all_companies: list[dict] = []
        all_contacts: list[dict] = []
        discovered_domains: set[str] = set()

        # --- Domain list mode (skip discovery if provided) ---
        domain_list_path = params.get("domain_list", "").strip()
        user_domains: list[str] = []
        if domain_list_path and os.path.exists(domain_list_path):
            with open(domain_list_path) as f:
                user_domains = [line.strip() for line in f if line.strip()]
            log(f"Loaded {len(user_domains)} domains from {domain_list_path}", "success")

        sources_filter = params.get("sources_filter", "All sources")

        # --- Source 1: Google Maps ---
        if not user_domains and sources_filter in ("All sources", "Google Maps only"):
            try:
                maps_results = loop.run_until_complete(
                    scrape_google_maps(industry, location, target_count, log)
                )
                all_companies.extend(maps_results)
                for r in maps_results:
                    if r.get("website_url"):
                        domain = _extract_domain(r["website_url"])
                        if domain:
                            discovered_domains.add(domain)
            except Exception as exc:
                log(f"[Google Maps] Failed: {exc}", "error")

        # --- Source 2: Yelp ---
        if not user_domains and sources_filter in ("All sources", "Yelp only"):
            try:
                yelp_results = loop.run_until_complete(
                    scrape_yelp(industry, location, target_count, discovered_domains, log)
                )
                all_companies.extend(yelp_results)
                for r in yelp_results:
                    if r.get("website_url"):
                        domain = _extract_domain(r["website_url"])
                        if domain:
                            discovered_domains.add(domain)
            except Exception as exc:
                log(f"[Yelp] Failed: {exc}", "error")

        # --- Source 3: Google Dorks ---
        if not user_domains and sources_filter in ("All sources",):
            try:
                dork_companies, dork_domains = loop.run_until_complete(
                    scrape_google_dorks(industry, location, discovered_domains, log)
                )
                all_contacts.extend(dork_companies)
                for url in dork_domains:
                    domain = _extract_domain(url)
                    if domain:
                        discovered_domains.add(domain)
            except Exception as exc:
                log(f"[Google Dork] Failed: {exc}", "error")

        # --- Source 4: Website Crawler ---
        crawl_targets = list(discovered_domains)
        if user_domains:
            crawl_targets = user_domains
        if sources_filter in ("All sources", "Website crawl only") and crawl_targets:
            try:
                crawl_contacts = loop.run_until_complete(
                    crawl_domains(crawl_targets, log)
                )
                all_contacts.extend(crawl_contacts)
            except Exception as exc:
                log(f"[Website Crawler] Failed: {exc}", "error")

        # Merge company info into contacts
        _merge_company_data(all_companies, all_contacts, industry, location)

        total_domains = len(discovered_domains) + len(user_domains)
        raw_emails = len([c for c in all_contacts if c.get("email")])
        log(f"Discovery complete: {total_domains} domains, {raw_emails} raw emails")

        # --- Save partial progress ---
        if len(all_contacts) > 0:
            save_partial(all_contacts, run_id, OUTPUT_DIR)

        # --- Syntax validation ---
        log("Running syntax validation…")
        valid_contacts = []
        for c in all_contacts:
            email = c.get("email", "")
            if email and is_valid_syntax(email):
                c["email"] = clean_email(email)
                valid_contacts.append(c)
            elif not email and (c.get("first_name") and c.get("last_name")):
                valid_contacts.append(c)  # name-only contacts for permutation

        log(f"Syntax check: {len(valid_contacts)} valid / {len(all_contacts)} total")

        # --- MX validation ---
        log("Checking MX records…")
        mx_valid = []
        for c in valid_contacts:
            email = c.get("email", "")
            if not email:
                mx_valid.append(c)
                continue
            domain = email.split("@")[1]
            if has_mx(domain):
                c["validation_status"] = "MX_ONLY"
                mx_valid.append(c)
            else:
                c["validation_status"] = "INVALID"
                log(f"[MX] ✕ {domain} — no MX records", "error")

        log(f"MX check: {len(mx_valid)} passed")

        # --- SMTP validation (deep mode) ---
        validate_level = params.get("validate_level", "deep")
        if validate_level == "deep":
            emails_to_verify = [c["email"] for c in mx_valid if c.get("email")]
            if emails_to_verify:
                log(f"Starting SMTP verification of {len(emails_to_verify)} emails…")
                smtp_results = loop.run_until_complete(
                    verify_emails_batch(emails_to_verify, log)
                )
                for c in mx_valid:
                    email = c.get("email", "")
                    if email in smtp_results:
                        c["validation_status"] = smtp_results[email]

            # --- Permutation enrichment (deep mode only) ---
            name_only = [c for c in mx_valid if not c.get("email") and c.get("first_name")]
            if name_only:
                log(f"Running email permutation for {len(name_only)} name-only contacts…")
                new_contacts = loop.run_until_complete(
                    enrich_with_permutations(name_only, verify_email, log)
                )
                mx_valid.extend(new_contacts)

        # Only keep contacts that have an email at this point
        contacts_with_email = [c for c in mx_valid if c.get("email")]

        # --- Infer company signals ---
        log("Inferring company signals…")
        for c in contacts_with_email:
            infer_company_signals(c)

        # --- Apply ICP filters ---
        filtered = apply_filters(
            contacts_with_email,
            revenue_filter=params.get("revenue_filter"),
            size_filter=params.get("size_filter"),
            age_filter=params.get("age_filter"),
            log_callback=log,
        )

        # --- Deduplicate ---
        deduped = deduplicate(filtered, log)

        # --- Score ---
        scored = score_all(deduped, target_titles, log)

        # --- Save partial ---
        if len(scored) > 0:
            save_partial(scored, run_id, OUTPUT_DIR)

        # --- Export ---
        log("Exporting results…")
        files = export(
            scored,
            filename=params.get("output_filename", "leads_output"),
            output_format=params.get("output_format", "CSV"),
            output_dir=OUTPUT_DIR,
            log_callback=log,
        )

        # --- Build summary ---
        valid_count = sum(1 for c in scored if c.get("validation_status") == "VALID")
        catchall_count = sum(1 for c in scored if c.get("validation_status") == "CATCH_ALL")
        invalid_count = sum(1 for c in scored if c.get("validation_status") in ("INVALID", "UNKNOWN", "DISPOSABLE"))
        deliverability = round(valid_count / max(len(scored), 1) * 100, 1)

        summary = {
            "domains_discovered": total_domains,
            "raw_emails_found": raw_emails,
            "after_dedup": len(deduped),
            "validated_smtp": valid_count,
            "catch_all": catchall_count,
            "invalid_unknown": invalid_count,
            "deliverability_pct": deliverability,
            "total_contacts": len(scored),
        }

        run["files"] = [os.path.basename(f) for f in files]
        run["summary"] = summary
        run["status"] = "complete"

        log(f"Run complete — {len(scored)} contacts exported", "success")

        # Send completion event
        q.put({
            "type": "complete",
            "summary": summary,
            "files": [os.path.basename(f) for f in files],
        })

        loop.close()

    except Exception as exc:
        logger.exception(f"Pipeline error: {exc}")
        log(f"Pipeline error: {exc}", "error")
        run["status"] = "error"
        q.put({"type": "complete", "summary": {"error": str(exc)}, "files": []})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        return parsed.netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _merge_company_data(companies: list[dict], contacts: list[dict], industry: str, location: str):
    """
    Merge business info from discovery sources into contact records.
    Also create stub contact records for companies that have no contacts yet.
    """
    domain_to_company: dict[str, dict] = {}
    for co in companies:
        url = co.get("website_url", "")
        if url:
            d = _extract_domain(url)
            if d:
                domain_to_company[d] = co

    # Enrich existing contacts with company data
    for c in contacts:
        domain = c.get("company_domain", "")
        if domain in domain_to_company:
            co = domain_to_company[domain]
            c.setdefault("company_name", co.get("business_name", ""))
            c.setdefault("phone", co.get("phone", ""))
            c.setdefault("address", co.get("address", ""))
        c.setdefault("industry", industry)
        c.setdefault("location", location)
        c.setdefault("job_title", "")
        c.setdefault("company_name", "")

    # Create stub contacts for companies with domains but no contacts yet
    contact_domains = {c.get("company_domain", "") for c in contacts}
    for co in companies:
        url = co.get("website_url", "")
        if not url:
            continue
        d = _extract_domain(url)
        if d and d not in contact_domains:
            contacts.append({
                "email": "",
                "first_name": "",
                "last_name": "",
                "company_name": co.get("business_name", ""),
                "company_domain": d,
                "phone": co.get("phone", ""),
                "address": co.get("address", ""),
                "industry": industry,
                "location": location,
                "job_title": "",
                "source": co.get("source", ""),
            })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger.info(f"Starting Opika Leads on {config.FLASK_HOST}:{config.FLASK_PORT}")
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
        threaded=True,
    )
