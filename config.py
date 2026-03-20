"""
Central configuration — loads .env values with sensible defaults.
All timing values are in seconds.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


# --- Timing & rate-limiting ---
GOOGLE_DORK_DELAY = _float("GOOGLE_DORK_DELAY", 10)
MAPS_SCROLL_DELAY_MIN = _float("MAPS_SCROLL_DELAY_MIN", 2)
MAPS_SCROLL_DELAY_MAX = _float("MAPS_SCROLL_DELAY_MAX", 5)
REQUEST_TIMEOUT = _float("REQUEST_TIMEOUT", 8)
SMTP_TIMEOUT = _float("SMTP_TIMEOUT", 6)
RETRY_DELAY = _float("RETRY_DELAY", 45)  # 45-second retry on rate-limit

# --- Concurrency ---
SMTP_CONCURRENCY = _int("SMTP_CONCURRENCY", 15)
CRAWLER_CONCURRENCY = _int("CRAWLER_CONCURRENCY", 10)

# --- Limits ---
MAX_PERMUTATIONS = _int("MAX_PERMUTATIONS", 5)

# --- Flask ---
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = _int("FLASK_PORT", 5000)
FLASK_DEBUG = _bool("FLASK_DEBUG", False)

# --- User-agent rotation pool ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# --- Viewport sizes for playwright randomisation ---
VIEWPORT_SIZES = [
    {"width": 1280, "height": 800},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

# --- Generic email prefixes to filter out ---
GENERIC_PREFIXES = {
    "noreply", "info", "support", "admin", "webmaster",
    "postmaster", "hello", "contact", "office", "mail",
}

# --- Crawl paths for website email extraction ---
CRAWL_PATHS = [
    "/", "/contact", "/contact-us", "/about", "/about-us",
    "/team", "/staff", "/people", "/leadership", "/our-team",
    "/get-in-touch", "/connect", "/management", "/owners",
    "/company", "/who-we-are", "/meet-the-team", "/our-staff",
]
