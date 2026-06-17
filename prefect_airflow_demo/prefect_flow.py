"""
Prefect demo flow: API  ->  CSV  ->  (Power BI reads the CSV)

This is a STANDALONE learning example. It does not import anything from the
Django project, so you can run it on its own and delete the whole
`prefect_airflow_demo/` folder later with zero side effects.

What it does
------------
1. `fetch_api_data`  – calls an HTTP JSON API and returns parsed rows.
2. `transform`       – flattens the JSON into a list of uniform dict rows.
3. `write_csv`       – writes those rows to a CSV file in OUTPUT_DIR.

Power BI then points at that CSV (or the OUTPUT_DIR folder) and refreshes on a
schedule. Prefect's only job here is to *orchestrate* steps 1-3 reliably:
retries, logging, scheduling, and a UI to see runs.

Run it once (no server needed):
    pip install -r requirements.txt
    python prefect_flow.py

Run it on a schedule with the Prefect UI/server:
    prefect server start                # in one terminal -> http://127.0.0.1:4200
    python prefect_flow.py --serve      # in another; registers a cron schedule
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from prefect import flow, task, get_run_logger

# ---------------------------------------------------------------------------
# Config (kept dead simple for the demo — in real life use env vars / Blocks)
# ---------------------------------------------------------------------------
# Example: the project's brand-margins API returns {manufacturer: margin_pct}.
API_URL = "https://stock.junaidworld.com/api/brand-margins"

# Where the CSV lands. Point Power BI at this folder.
OUTPUT_DIR = Path(__file__).parent / "powerbi_inbox"


# ---------------------------------------------------------------------------
# Tasks  (a "task" = one retryable unit of work)
# ---------------------------------------------------------------------------
@task(retries=3, retry_delay_seconds=10)
def fetch_api_data(url: str) -> dict | list:
    """Call the API and return parsed JSON. Retried up to 3x on failure."""
    logger = get_run_logger()
    logger.info("Fetching %s", url)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    logger.info("Got %s top-level entries", len(data))
    return data


@task
def transform(data: dict | list) -> list[dict]:
    """Flatten the API payload into uniform rows for the CSV.

    The brand-margins API returns a dict {manufacturer: margin_pct}; we turn it
    into rows. Adjust this one function for whatever shape your API returns.
    """
    rows: list[dict] = []
    pulled_at = datetime.utcnow().isoformat(timespec="seconds")

    if isinstance(data, dict):
        for manufacturer, margin_pct in data.items():
            rows.append(
                {
                    "manufacturer": str(manufacturer),
                    "min_margin_pct": margin_pct,
                    "pulled_at_utc": pulled_at,
                }
            )
    else:  # list of dicts -> pass through, just stamp the pull time
        for item in data:
            row = dict(item)
            row["pulled_at_utc"] = pulled_at
            rows.append(row)

    return rows


@task
def write_csv(rows: list[dict], output_dir: Path) -> Path:
    """Write rows to a timestamped CSV plus a stable `latest.csv` for Power BI."""
    logger = get_run_logger()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not rows:
        logger.warning("No rows to write.")
        return output_dir / "latest.csv"

    fieldnames = list(rows[0].keys())
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dated = output_dir / f"brand_margins_{stamp}.csv"
    latest = output_dir / "latest.csv"

    for path in (dated, latest):
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    logger.info("Wrote %d rows to %s (and latest.csv)", len(rows), dated.name)
    return latest


# ---------------------------------------------------------------------------
# Flow  (a "flow" = the orchestrated pipeline that wires tasks together)
# ---------------------------------------------------------------------------
@flow(name="api-to-powerbi-csv")
def api_to_csv(url: str = API_URL, output_dir: Path = OUTPUT_DIR) -> str:
    """Fetch -> transform -> write CSV. Returns the path Power BI should read."""
    data = fetch_api_data(url)
    rows = transform(data)
    latest = write_csv(rows, output_dir)
    return str(latest)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        # Register this flow with a schedule (needs `prefect server start`).
        # Runs every 30 minutes; the UI shows each run, its logs and retries.
        api_to_csv.serve(
            name="api-to-powerbi-csv-every-30m",
            interval=timedelta(minutes=30),
        )
    else:
        # One-off local run, no server required.
        print("CSV ready for Power BI at:", api_to_csv())
