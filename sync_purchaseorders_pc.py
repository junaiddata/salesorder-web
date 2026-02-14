#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync script to fetch purchase orders from SAP API and push to VPS.
This script runs on your local PC and can access http://192.168.1.103/IntegrationApi/api/PurchaseOrder

WORKFLOW:
1. PC script fetches data from local SAP API (192.168.1.103)
2. PC script sends data to VPS via HTTP API endpoint
3. VPS updates its database

Usage:
    python sync_purchaseorders_pc.py           # Runs every 7 minutes (background service)
    python sync_purchaseorders_pc.py --once    # Single run (for testing)
"""

import sys
import os
import requests
import traceback
import time
import schedule
import argparse
from datetime import datetime
from typing import Dict, List, Any

# Add Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'salesorder.settings')

# Setup Django
import django
django.setup()

from django.conf import settings
from so.api_client import SAPAPIClient

# Configuration - EDIT THESE
VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')
VPS_API_KEY = os.getenv('VPS_API_KEY', 'test')

# Sync interval in minutes
SYNC_INTERVAL_MINUTES = 7

# Log file path (in logs directory)
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "sync_purchaseorders.log")


def log_message(message: str, also_print: bool = True, level: str = "INFO"):
    """Write message to log file and optionally print to console."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] [{level}] {message}\n"

    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f"Logging error: {e}")

    if also_print:
        print(message, flush=True)


def serialize_order(order: Dict) -> Dict:
    """Convert date objects to strings for JSON serialization."""
    serialized = order.copy()
    if 'posting_date' in serialized and serialized['posting_date']:
        if hasattr(serialized['posting_date'], 'isoformat'):
            serialized['posting_date'] = serialized['posting_date'].isoformat()
        elif isinstance(serialized['posting_date'], str):
            pass
        else:
            serialized['posting_date'] = None
    return serialized


def sync_purchaseorders() -> Dict[str, Any]:
    """
    Fetch OPEN purchase orders from SAP API and sync to VPS.
    Uses only DocumentStatus=bost_Open (all pages). Replaces existing open POs with API data.

    Returns:
        dict with success, stats, error keys
    """
    sync_start_time = datetime.now()

    log_message("=" * 70)
    log_message("SAP Open Purchase Order Sync (PC -> VPS via HTTP)")
    log_message("=" * 70)
    log_message(f"Started at: {sync_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_message(f"Local API: {getattr(settings, 'SAP_PURCHASE_ORDER_API_URL', '')}")
    log_message(f"VPS URL: {VPS_BASE_URL}")
    log_message("Fetching: DocumentStatus=bost_Open (all pages)")
    log_message("-" * 70)

    if VPS_API_KEY == 'your-secret-api-key':
        error_msg = "ERROR: Please configure VPS_API_KEY!"
        log_message(error_msg, level="ERROR")
        return {"success": False, "error": error_msg}

    try:
        log_message("[STEP 1] Fetching OPEN purchase orders from SAP API...")
        client = SAPAPIClient()
        open_orders = client.fetch_open_purchaseorders()
        seen_docnums = set()
        all_orders = []
        for order in open_orders:
            docnum_val = order.get('DocNum')
            if docnum_val and docnum_val not in seen_docnums:
                all_orders.append(order)
                seen_docnums.add(docnum_val)
        api_calls = max(1, (len(open_orders) + 19) // 20)
        log_message(f"  Found {len(all_orders)} open purchase orders (all pages)")

        if not all_orders:
            log_message("  No purchase orders found.", level="WARNING")
            return {"success": True, "stats": {"total": 0}, "error": None}

        log_message(f"  ✓ Fetched {len(all_orders)} purchase orders")

        log_message("[STEP 2] Mapping API responses...")
        mapped_orders = []
        mapping_errors = []
        for api_order in all_orders:
            try:
                mapped = client._map_purchaseorder_api_response(api_order)
                mapped_orders.append(mapped)
            except Exception as e:
                error_msg = f"Error mapping order {api_order.get('DocNum')}: {e}"
                log_message(f"  {error_msg}", level="ERROR")
                mapping_errors.append(error_msg)

        if not mapped_orders:
            log_message("  No orders could be mapped successfully.", level="ERROR")
            return {"success": False, "error": "No orders could be mapped"}

        if mapping_errors:
            log_message(f"  {len(mapping_errors)} orders failed to map", level="WARNING")

        log_message(f"  ✓ Mapped {len(mapped_orders)} orders")

        api_po_numbers = [m['po_number'] for m in mapped_orders if m.get('po_number')]

        log_message("[STEP 3] Sending data to VPS via HTTP API...")
        serialized_orders = [serialize_order(order) for order in mapped_orders]

        vps_url = f"{VPS_BASE_URL}/sappurchaseorders/sync-api-receive/"
        payload = {
            "purchase_orders": serialized_orders,
            "api_po_numbers": api_po_numbers,
            "api_key": VPS_API_KEY,
            "sync_metadata": {
                "api_calls": api_calls,
                "sync_time": datetime.now().isoformat(),
            }
        }

        log_message(f"  Sending {len(serialized_orders)} open purchase orders to VPS...")
        log_message(f"  VPS URL: {vps_url}")

        send_start = datetime.now()
        response = requests.post(vps_url, json=payload, timeout=300)
        send_duration = (datetime.now() - send_start).total_seconds()

        response.raise_for_status()

        result = response.json()
        success = result.get("success", False)
        stats = result.get("stats", {})
        error = result.get("error")

        if success:
            replaced = stats.get("replaced", 0)
            total_items = stats.get("total_items", 0)

            log_message(f"  ✓ Successfully synced to VPS (took {send_duration:.2f}s)")
            log_message(f"    Replaced: {replaced} open purchase orders")
            log_message(f"    Total Items: {total_items}")

            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()

            log_message("=" * 70)
            log_message("SYNC COMPLETED SUCCESSFULLY")
            log_message("=" * 70)
            log_message(f"Duration: {sync_duration:.2f} seconds")
            log_message(f"API Calls: {api_calls}")
            log_message(f"Total Open Purchase Orders: {len(mapped_orders)}")
            log_message("=" * 70)

            return {
                "success": True,
                "stats": {
                    "replaced": replaced,
                    "total_items": total_items,
                    "total_orders": len(mapped_orders),
                    "api_calls": api_calls,
                    "duration": sync_duration,
                },
                "error": None
            }
        else:
            log_message(f"  ✗ VPS sync failed: {error}", level="ERROR")
            return {"success": False, "stats": {}, "error": error}

    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        status_code = getattr(resp, "status_code", None)
        resp_text = ""
        try:
            resp_text = resp.text if resp is not None else ""
        except Exception:
            resp_text = ""

        error_msg = f"Failed to send to VPS: HTTP {status_code}"
        log_message(f"  ✗ {error_msg}", level="ERROR")
        if resp_text:
            log_message(f"  VPS response (truncated): {resp_text[:1000]}", level="ERROR")
        return {"success": False, "stats": {}, "error": error_msg}

    except requests.RequestException as e:
        error_msg = f"Failed to send to VPS: {str(e)}"
        log_message(f"  ✗ {error_msg}", level="ERROR")
        return {"success": False, "stats": {}, "error": error_msg}

    except Exception as e:
        sync_end_time = datetime.now()
        sync_duration = (sync_end_time - sync_start_time).total_seconds()

        log_message("=" * 70, level="ERROR")
        log_message("SYNC FAILED", level="ERROR")
        log_message("=" * 70, level="ERROR")
        log_message(f"Error: {str(e)}", level="ERROR")
        log_message(f"Duration: {sync_duration:.2f} seconds", level="ERROR")
        log_message("=" * 70, level="ERROR")
        log_message(traceback.format_exc(), level="ERROR")

        return {"success": False, "stats": {}, "error": str(e)}


def run_scheduled_sync():
    """Wrapper function for scheduled execution."""
    try:
        result = sync_purchaseorders()
        if not result.get("success"):
            log_message(f"Scheduled sync completed with error: {result.get('error')}", level="WARNING")
    except Exception as e:
        log_message(f"Error in scheduled sync: {e}", level="ERROR")
        log_message(traceback.format_exc(), level="ERROR")


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Sync OPEN purchase orders from SAP API to VPS (DocumentStatus=bost_Open only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_purchaseorders_pc.py         # Runs every 7 minutes (service mode)
  python sync_purchaseorders_pc.py --once  # Single run
        """
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (default: run as background service)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=SYNC_INTERVAL_MINUTES,
        help=f'Sync interval in minutes for service mode (default: {SYNC_INTERVAL_MINUTES})'
    )

    args = parser.parse_args()

    if args.once:
        log_message("=" * 70)
        log_message("Running ONE-TIME sync (Open Purchase Orders only)")
        log_message("=" * 70)

        result = sync_purchaseorders()

        if result.get("success"):
            log_message("\n✓ Sync completed successfully!")
            return 0
        else:
            log_message(f"\n✗ Sync failed: {result.get('error')}", level="ERROR")
            return 1
    else:
        interval = args.interval

        log_message("=" * 70)
        log_message("PC Open Purchase Order Sync Service Started")
        log_message("=" * 70)
        log_message(f"Service will sync every {interval} minutes")
        log_message(f"Log file: {LOG_FILE}")
        log_message("Press Ctrl+C to stop the service")
        log_message("=" * 70)

        schedule.every(interval).minutes.do(run_scheduled_sync)

        log_message("\nRunning initial sync...")
        run_scheduled_sync()

        try:
            while True:
                schedule.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            log_message("\n" + "=" * 70)
            log_message("Service stopped by user")
            log_message("=" * 70)
            return 0


if __name__ == "__main__":
    sys.exit(main())
