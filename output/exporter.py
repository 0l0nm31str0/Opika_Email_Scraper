"""
Output exporter — writes validated contacts to CSV and/or JSON.
Columns are ordered per spec; sorted by confidence_score descending.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

import pandas as pd
from loguru import logger

CSV_COLUMNS = [
    "first_name", "last_name", "email", "company_name", "company_domain",
    "job_title", "industry", "location", "phone", "annual_revenue_est",
    "employee_count_est", "company_age_est", "confidence_score",
    "validation_status", "source", "date_scraped",
]


def export(
    contacts: list[dict],
    filename: str = "leads_output",
    output_format: str = "CSV",
    output_dir: str = "output",
    log_callback=None,
) -> list[str]:
    """
    Write contacts to CSV and/or JSON.

    Returns a list of written file paths (relative).
    """
    def _log(msg: str, status: str = "info"):
        logger.info(msg)
        if log_callback:
            log_callback(msg, status)

    os.makedirs(output_dir, exist_ok=True)

    # Stamp date_scraped on every contact
    today = datetime.now().strftime("%Y-%m-%d")
    for c in contacts:
        c.setdefault("date_scraped", today)

    # Ensure all expected columns exist
    for c in contacts:
        for col in CSV_COLUMNS:
            c.setdefault(col, "")

    df = pd.DataFrame(contacts)

    # Keep only the spec columns, in order
    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[CSV_COLUMNS]

    # Sort by confidence_score descending
    df = df.sort_values("confidence_score", ascending=False).reset_index(drop=True)

    files: list[str] = []
    fmt = output_format.lower()

    if fmt in ("csv", "both"):
        path = os.path.join(output_dir, f"{filename}.csv")
        df.to_csv(path, index=False)
        files.append(path)
        _log(f"[Export] Wrote {len(df)} contacts to {path}", "success")

    if fmt in ("json", "both"):
        path = os.path.join(output_dir, f"{filename}.json")
        df.to_json(path, orient="records", indent=2)
        files.append(path)
        _log(f"[Export] Wrote {len(df)} contacts to {path}", "success")

    return files


def save_partial(
    contacts: list[dict],
    run_id: str,
    output_dir: str = "output",
):
    """Save intermediate progress to a partial CSV."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{run_id}_partial.csv")

    for c in contacts:
        for col in CSV_COLUMNS:
            c.setdefault(col, "")

    df = pd.DataFrame(contacts)
    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[CSV_COLUMNS]
    df.to_csv(path, index=False)
    logger.debug(f"[Export] Saved partial: {len(df)} contacts to {path}")
    return path


def load_partial(run_id: str, output_dir: str = "output") -> list[dict]:
    """Load a partial CSV for --resume functionality."""
    path = os.path.join(output_dir, f"{run_id}_partial.csv")
    if not os.path.exists(path):
        return []
    df = pd.read_csv(path)
    return df.to_dict("records")
