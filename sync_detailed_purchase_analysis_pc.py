#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC script: fetch OPEN + CLOSED purchase orders (with WarehouseCode) from the SAP API
and push them to the VPS for the "Detailed Purchase Analysis" page.

Separate from sync_purchaseorders_pc.py (which keeps syncing open POs only).
This script only ADDS/UPDATES rows in the Detailed Purchase Analysis tables.

Usage:
    python sync_detailed_purchase_analysis_pc.py --local               # 2026-01-01 -> today, into local DB
    python sync_detailed_purchase_analysis_pc.py --days 60
    python sync_detailed_purchase_analysis_pc.py --from 2025-01-01 --to 2025-12-31
    python sync_detailed_purchase_analysis_pc.py --days 30 --status close   # send DocumentStatus=bost_Close too
    python sync_detailed_purchase_analysis_pc.py --days 7 --dry-run          # fetch + print status counts, send nothing
"""

import os
import sys
import argparse
import traceback
from collections import Counter
from datetime import date, datetime, timedelta

import requests

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'salesorder.settings')
import django
django.setup()

from so.api_client import SAPAPIClient
from so.detailed_purchase_analysis_services import map_api_purchaseorder

VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')
VPS_API_KEY = os.getenv('VPS_API_KEY', 'test')
BATCH_SIZE = 200
CHUNK_DAYS = 7          # the SAP API is slow on wide ranges; small chunks are reliable
API_TIMEOUT = 180       # seconds per request
RETRIES = 3


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def date_chunks(start: date, end: date, step_days: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=step_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def with_retry(fn, *args):
    for attempt in range(1, RETRIES + 1):
        try:
            return fn(*args)
        except RuntimeError as e:
            log(f"    attempt {attempt}/{RETRIES} failed: {e}")
            if attempt == RETRIES:
                raise


def fetch_range(client, from_date: str, to_date: str, status: str):
    """Date-range fetch; optional DocumentStatus filter (all / open / close)."""
    payload = {"FromDate": from_date, "ToDate": to_date}
    if status == 'open':
        payload["DocumentStatus"] = "bost_Open"
    elif status == 'close':
        payload["DocumentStatus"] = "bost_Close"
    return client._fetch_all_pages_with_url(payload, client._get_po_base_url())


def main():
    parser = argparse.ArgumentParser(description="Sync open+closed POs (with warehouse) to VPS")
    parser.add_argument('--days', type=int, default=None, help='Fetch POs of the last N days (default: from 2026-01-01)')
    parser.add_argument('--from', dest='from_date', help='Start date YYYY-MM-DD (overrides --days)')
    parser.add_argument('--to', dest='to_date', help='End date YYYY-MM-DD (default today)')
    parser.add_argument('--status', choices=['all', 'open', 'close'], default='all',
                        help="DocumentStatus filter sent with the date range (default: none = all)")
    parser.add_argument('--no-open', action='store_true', help="Skip the extra fetch of all currently open POs")
    parser.add_argument('--local', action='store_true', help="Save straight into this PC's local database (localhost testing) instead of sending to the VPS")
    parser.add_argument('--dry-run', action='store_true', help="Fetch and report only; do not send to VPS")
    args = parser.parse_args()

    end = datetime.strptime(args.to_date, '%Y-%m-%d').date() if args.to_date else date.today()
    if args.from_date:
        start = datetime.strptime(args.from_date, '%Y-%m-%d').date()
    elif args.days:
        start = end - timedelta(days=args.days)
    else:
        start = date(2026, 1, 1)

    log(f"Range: {start} -> {end} | status filter: {args.status} | VPS: {VPS_BASE_URL}")

    try:
        client = SAPAPIClient()
        client.timeout = API_TIMEOUT
        by_docnum = {}

        for c_start, c_end in date_chunks(start, end):
            orders = with_retry(fetch_range, client, c_start.isoformat(), c_end.isoformat(), args.status)
            log(f"  {c_start} -> {c_end}: {len(orders)} POs")
            for o in orders:
                if o.get('DocNum'):
                    by_docnum[o['DocNum']] = o

        if not args.no_open:
            open_orders = with_retry(client.fetch_open_purchaseorders)
            log(f"  All currently open POs: {len(open_orders)}")
            for o in open_orders:
                if o.get('DocNum'):
                    by_docnum[o['DocNum']] = o

        status_counts = Counter(o.get('DocumentStatus', '?') for o in by_docnum.values())
        log(f"Total unique POs: {len(by_docnum)} | DocumentStatus breakdown: {dict(status_counts)}")
        wh_counts = Counter(
            (l.get('WarehouseCode') or '-') for o in by_docnum.values() for l in (o.get('DocumentLines') or [])
        )
        log(f"Line WarehouseCode breakdown: {dict(wh_counts)}")

        if args.dry_run:
            log("Dry run - nothing sent.")
            return 0
        if not by_docnum:
            log("Nothing to send.")
            return 0

        mapped = []
        for o in by_docnum.values():
            try:
                mapped.append(map_api_purchaseorder(o))
            except Exception as e:
                log(f"  Mapping error for DocNum {o.get('DocNum')}: {e}")

        if args.local:
            from so.detailed_purchase_analysis_services import save_detailed_purchaseorders
            stats = save_detailed_purchaseorders(mapped)
            log(f"Saved to LOCAL database: {stats}")
            return 0

        url = f"{VPS_BASE_URL}/detailed-purchase-analysis/sync-receive/"
        totals = Counter()
        for i in range(0, len(mapped), BATCH_SIZE):
            batch = mapped[i:i + BATCH_SIZE]
            resp = requests.post(url, json={"purchase_orders": batch, "api_key": VPS_API_KEY}, timeout=300)
            resp.raise_for_status()
            result = resp.json()
            if not result.get('success'):
                log(f"  VPS error: {result.get('error')}")
                return 1
            totals.update(result.get('stats', {}))
            log(f"  Sent {min(i + BATCH_SIZE, len(mapped))}/{len(mapped)}")

        log(f"Done. VPS stats: {dict(totals)}")
        return 0

    except Exception as e:
        log(f"FAILED: {e}")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
