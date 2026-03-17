# Opika Leads

A complete Python web application that finds and validates real business email addresses using only free sources. Enter your Ideal Customer Profile (ICP) parameters via a web form, watch live progress logs, and download a validated CSV of contacts.

## Features

- **Plain English ICP parsing** — describe your target in natural language
- **Multi-source discovery** — Google Maps, Yelp, Google Search dorks, website crawling
- **Email permutation enrichment** — generates and verifies name-based email guesses
- **3-layer validation** — syntax check → MX records → SMTP verification
- **Catch-all & disposable detection** — flags unreliable domains
- **Fuzzy deduplication** — exact email + fuzzy name+company matching
- **Confidence scoring** — 0–100 score based on source, validation, and profile completeness
- **Live SSE streaming** — real-time log output in the browser
- **CSV/JSON export** — clean output ready for cold email tools

## Requirements

- Python 3.11+
- System dependencies for Playwright (Chromium)

## Setup

```bash
# 1. Clone and enter the project
cd opika-leads

# 2. Create a virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install spaCy language model
python -m spacy download en_core_web_sm

# 5. Install Playwright browsers
playwright install chromium

# 6. Copy env config
cp .env.example .env
```

## Usage

### Web UI (recommended)

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

1. Describe your target audience in plain English
2. Adjust industry, location, titles, and filters as needed
3. Click **Generate leads →**
4. Watch the live log as sources are scraped and emails are validated
5. Download your CSV when the run completes

### CLI

```bash
# Simple query
python cli.py --query "HVAC company owners in Florida" --count 200

# With specific options
python cli.py --query "Personal injury law firms in Texas" \
  --count 150 --validate deep --format both

# Domain list mode (skip discovery, just crawl + validate)
python cli.py --domains path/to/domains.txt --titles "Owner,CEO" --count 100

# Resume a partial run
python cli.py --resume <run_id>
```

## Configuration

All settings are in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_DORK_DELAY` | 10 | Seconds between Google search queries |
| `MAPS_SCROLL_DELAY_MIN` | 2 | Min delay between Maps scroll actions |
| `MAPS_SCROLL_DELAY_MAX` | 5 | Max delay between Maps scroll actions |
| `REQUEST_TIMEOUT` | 8 | httpx request timeout (seconds) |
| `SMTP_TIMEOUT` | 6 | SMTP socket timeout (seconds) |
| `SMTP_CONCURRENCY` | 15 | Parallel SMTP verification connections |
| `CRAWLER_CONCURRENCY` | 10 | Parallel website crawl connections |
| `MAX_PERMUTATIONS` | 5 | Email permutations per contact |
| `RETRY_DELAY` | 30 | Retry delay on rate-limited requests |

## Output

CSV columns (in order):
```
first_name, last_name, email, company_name, company_domain,
job_title, industry, location, phone, annual_revenue_est,
employee_count_est, company_age_est, confidence_score,
validation_status, source, date_scraped
```

Sorted by `confidence_score` descending. Output saved to the `output/` directory.

## Architecture

```
app.py                    Flask entrypoint + pipeline orchestration
config.py                 .env loader + defaults
icp_parser.py             spaCy NER + keyword ICP parser
cli.py                    CLI entry point with --resume support
sources/
  google_maps.py          Playwright-based Maps scraper
  yelp.py                 httpx Yelp search scraper
  google_dork.py          Google SERP dork scraper
  website_crawler.py      Async domain crawler + email extractor
enrichment/
  permutator.py           Name-based email permutation generator
validation/
  syntax.py               Regex email syntax check
  mx_check.py             DNS MX record lookup (cached)
  smtp_check.py           Async SMTP RCPT TO verification
pipeline/
  dedup.py                Exact + fuzzy deduplication
  scoring.py              0–100 confidence scoring
  filters.py              Revenue/size/age ICP filters
output/
  exporter.py             CSV + JSON writer
```

## Anti-Detection

- Randomised viewport sizes and User-Agent strings
- Configurable delays between all requests
- 30-second retry with backoff on rate limits (429/CAPTCHA)
- Graceful failure — blocked sources are skipped, never crash the run
- SMTP connections always cleanly closed with QUIT
