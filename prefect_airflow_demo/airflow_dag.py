"""
The SAME pipeline as `prefect_flow.py`, written for Apache Airflow.

Put this file in your Airflow `dags/` folder. Airflow's scheduler discovers it,
shows it in the web UI, and runs it on the `schedule` below.

The business logic (fetch -> transform -> write CSV) is IDENTICAL to the Prefect
version. Only the orchestration wrapper changes. Read this side by side with
prefect_flow.py to see exactly what differs:

  * Prefect: plain Python functions decorated with @task / @flow. You call them
    like normal functions; data is passed by returning values.
  * Airflow: each step is an "operator"/`@task` in a DAG graph. Steps pass data
    via XCom (small values) and you wire dependencies explicitly with `>>`.
    Airflow is heavier (needs a metadata DB + scheduler + webserver running).

This is a STANDALONE learning example — safe to delete with the folder.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import requests

from airflow.decorators import dag, task

API_URL = "https://stock.junaidworld.com/api/brand-margins"
OUTPUT_DIR = Path("/opt/airflow/powerbi_inbox")  # a path the Power BI gateway can read


@dag(
    dag_id="api_to_powerbi_csv",
    schedule="*/30 * * * *",          # every 30 min (cron) — Prefect used interval=
    start_date=datetime(2024, 1, 1),
    catchup=False,                    # don't backfill missed runs
    default_args={
        "retries": 3,                 # same retry policy as the Prefect @task
        "retry_delay": timedelta(seconds=10),
    },
    tags=["demo", "powerbi"],
)
def api_to_powerbi_csv():

    @task
    def fetch_api_data(url: str) -> dict | list:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    @task
    def transform(data: dict | list) -> list[dict]:
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
        else:
            for item in data:
                row = dict(item)
                row["pulled_at_utc"] = pulled_at
                rows.append(row)
        return rows

    @task
    def write_csv(rows: list[dict]) -> str:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        latest = OUTPUT_DIR / "latest.csv"
        if not rows:
            return str(latest)
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dated = OUTPUT_DIR / f"brand_margins_{stamp}.csv"
        fieldnames = list(rows[0].keys())
        for path in (dated, latest):
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        return str(latest)

    # Wire the dependency graph. In Prefect this ordering was implicit from the
    # function call order; in Airflow you express it by passing task outputs.
    data = fetch_api_data(API_URL)
    rows = transform(data)
    write_csv(rows)


dag = api_to_powerbi_csv()
