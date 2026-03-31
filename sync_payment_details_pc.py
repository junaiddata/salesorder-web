#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync script to fetch pending invoice lines from GetPaymentDetails API and push to VPS.
This script runs on your local PC and can access http://192.168.1.103/IntegrationApi/api/GetPaymentDetails

WORKFLOW:
1. PC script fetches data from local SAP API (192.168.1.103)
2. PC script sends data to VPS via HTTP API endpoint
3. VPS updates its database pending invoice table

Usage:
    python sync_payment_details_pc.py           # Runs every 1 hour (background service)
    python sync_payment_details_pc.py --once    # Single run (for testing)
"""

import sys
import os
import requests
import traceback
import time
import schedule
import argparse
from datetime import date, datetime
from typing import Dict, Any

# Add Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'salesorder.settings')

# Setup Django
import django
django.setup()


VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')
VPS_API_KEY = os.getenv('VPS_API_KEY', 'test')
SYNC_INTERVAL_MINUTES = 60

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_FALLBACK_LOG_DIR = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", ".")), "salesorder_sync")
_LOG_FILE_RESOLVED = None
_LOG_WARNED = False


def _get_log_file():
    global _LOG_FILE_RESOLVED, _LOG_WARNED
    if _LOG_FILE_RESOLVED is not None:
        return _LOG_FILE_RESOLVED
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        test_path = os.path.join(LOG_DIR, "sync_payment_details.log")
        with open(test_path, "a", encoding="utf-8"):
            pass
        _LOG_FILE_RESOLVED = test_path
        return _LOG_FILE_RESOLVED
    except (OSError, PermissionError):
        pass
    try:
        os.makedirs(_FALLBACK_LOG_DIR, exist_ok=True)
        fallback = os.path.join(_FALLBACK_LOG_DIR, "sync_payment_details.log")
        with open(fallback, "a", encoding="utf-8"):
            pass
        _LOG_FILE_RESOLVED = fallback
        if not _LOG_WARNED:
            _LOG_WARNED = True
            print(f"Log file (project dir not writable): using {fallback}", flush=True)
        return _LOG_FILE_RESOLVED
    except (OSError, PermissionError):
        pass
    _LOG_FILE_RESOLVED = False
    if not _LOG_WARNED:
        _LOG_WARNED = True
        print("Logging to file disabled (permission denied). Output to console only.", flush=True)
    return None


def log_message(message: str, also_print: bool = True, level: str = "INFO"):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] [{level}] {message}\n"
    log_path = _get_log_file()
    if log_path:
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except (OSError, PermissionError):
            pass
    if also_print:
        print(message, flush=True)


def get_log_file_path_for_display():
    _get_log_file()
    return _LOG_FILE_RESOLVED if _LOG_FILE_RESOLVED else None


def sync_payment_details() -> Dict[str, Any]:
    sync_start_time = datetime.now()
    from_date = date(2020, 1, 1).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    log_message("=" * 70)
    log_message("SAP Payment Details Sync (PC -> VPS via HTTP)")
    log_message("=" * 70)
    _log_path = get_log_file_path_for_display()
    log_message("Log file: " + (_log_path if _log_path else "console only (no file)"))
    log_message(f"Date range: {from_date} to {to_date}")
    log_message(f"Local API: http://192.168.1.103/IntegrationApi/api/GetPaymentDetails")
    log_message(f"VPS URL: {VPS_BASE_URL}")
    log_message("-" * 70)

    if VPS_API_KEY == 'test':
        error_msg = 'ERROR: Please configure VPS_API_KEY!'
        log_message(error_msg, level="ERROR")
        return {"success": False, "stats": {}, "error": error_msg}

    try:
        log_message("[STEP 1] Fetching data from SAP API...")
        api_url = "http://192.168.1.103/IntegrationApi/api/GetPaymentDetails"
        timeout = 60
        payload = {"FromDate": from_date, "ToDate": to_date}

        try:
            response = requests.post(api_url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                payment_details_data = data.get('Data', [])
                count = data.get('Count', len(payment_details_data))
                log_message(f"  Fetched {len(payment_details_data)} payment detail records (Total: {count})")
            elif isinstance(data, list):
                payment_details_data = data
                log_message(f"  Fetched {len(payment_details_data)} payment detail records")
            else:
                payment_details_data = []
                log_message(f"  Unexpected API response format: {type(data)}", level="WARNING")
        except requests.exceptions.Timeout:
            error_msg = f"API request timeout after {timeout}s"
            log_message(f"  ✗ {error_msg}", level="ERROR")
            return {"success": False, "stats": {}, "error": error_msg}
        except requests.exceptions.RequestException as e:
            error_msg = f"API request error: {e}"
            log_message(f"  ✗ {error_msg}", level="ERROR")
            return {"success": False, "stats": {}, "error": error_msg}

        if not payment_details_data:
            return {"success": False, "stats": {}, "error": "No data received from API"}

        log_message("[STEP 2] Sending data to VPS via HTTP API...")
        vps_url = f"{VPS_BASE_URL}/customers/sync-payment-details-api-receive/"
        send_payload = {
            "payment_details_data": payment_details_data,
            "api_key": VPS_API_KEY,
            "sync_metadata": {
                "from_date": from_date,
                "to_date": to_date,
                "total_count": len(payment_details_data),
                "sync_time": datetime.now().isoformat(),
            },
        }

        response = requests.post(vps_url, json=send_payload, timeout=300)
        response.raise_for_status()
        result = response.json()
        success = result.get("success", False)
        stats = result.get("stats", {})
        error = result.get("error")

        if success:
            sync_duration = (datetime.now() - sync_start_time).total_seconds()
            log_message("=" * 70)
            log_message("SYNC COMPLETED SUCCESSFULLY")
            log_message("=" * 70)
            log_message(f"Duration: {sync_duration:.2f} seconds")
            log_message(f"Received: {stats.get('total_received', len(payment_details_data))}")
            log_message(f"Created: {stats.get('created', 0)}")
            log_message(f"Skipped (no customer): {stats.get('skipped_no_customer', 0)}")
            log_message("=" * 70)
            return {
                "success": True,
                "stats": stats,
                "error": None,
            }

        return {"success": False, "stats": stats, "error": error or "Unknown VPS sync error"}

    except Exception as e:
        log_message("SYNC FAILED", level="ERROR")
        log_message(f"Error: {str(e)}", level="ERROR")
        log_message(traceback.format_exc(), level="ERROR")
        return {"success": False, "stats": {}, "error": str(e)}


def run_scheduled_sync():
    try:
        result = sync_payment_details()
        if not result.get("success"):
            log_message(f"Scheduled sync completed with error: {result.get('error')}", level="WARNING")
    except Exception as e:
        log_message(f"Error in scheduled sync: {e}", level="ERROR")
        log_message(traceback.format_exc(), level="ERROR")


def main():
    parser = argparse.ArgumentParser(
        description="Sync Payment Details from SAP API to VPS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (default: run as background service)',
    )
    args = parser.parse_args()

    if args.once:
        result = sync_payment_details()
        sys.exit(0 if result.get("success") else 1)

    log_message(f"Starting payment details sync service (every {SYNC_INTERVAL_MINUTES} minutes)...")
    log_message("Press Ctrl+C to stop")
    run_scheduled_sync()
    schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(run_scheduled_sync)
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        log_message("\nService stopped by user")


if __name__ == "__main__":
    main()
