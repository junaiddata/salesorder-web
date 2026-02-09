#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync script to fetch Customer Finance Summary from SAP API and push to VPS.
This script runs on your local PC and can access http://192.168.1.103/IntegrationApi/api/FinanceSummary

WORKFLOW:
1. PC script fetches data from local SAP API (192.168.1.103)
2. PC script sends data to VPS via HTTP API endpoint
3. VPS updates its database

Usage:
    python sync_customer_finance_pc.py           # Runs every 1 hour (background service)
    python sync_customer_finance_pc.py --once    # Single run (for testing)
"""

import sys
import os
import requests
import json
import traceback
import time
import schedule
import argparse
from datetime import datetime
from typing import Dict, List, Optional, Any

# Add Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'salesorder.settings')

# Setup Django
import django
django.setup()

from django.conf import settings

# Configuration - EDIT THESE
VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')  # Production VPS URL
VPS_API_KEY = os.getenv('VPS_API_KEY', 'test')  # Must match VPS

# Sync interval in minutes
SYNC_INTERVAL_MINUTES = 60  # 1 hour

# Log file path (in logs directory)
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "sync_customer_finance.log")


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


def sync_customer_finance() -> Dict[str, Any]:
    """
    Fetch Customer Finance Summary from SAP API and sync to VPS.
    
    Returns:
        dict with success, stats, error keys
    """
    sync_start_time = datetime.now()
    
    log_message("=" * 70)
    log_message("SAP Customer Finance Summary Sync (PC -> VPS via HTTP)")
    log_message("=" * 70)
    log_message(f"Started at: {sync_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_message(f"Local API: http://192.168.1.103/IntegrationApi/api/FinanceSummary")
    log_message(f"VPS URL: {VPS_BASE_URL}")
    log_message("-" * 70)
    
    # Check configuration
    if VPS_API_KEY == 'test':
        error_msg = 'ERROR: Please configure VPS_API_KEY!'
        log_message(error_msg, level="ERROR")
        return {"success": False, "stats": {}, "error": error_msg}
    
    try:
        # Step 1: Fetch from SAP API (on PC)
        log_message("[STEP 1] Fetching data from SAP API...")
        
        api_url = "http://192.168.1.103/IntegrationApi/api/FinanceSummary"
        timeout = 30
        
        try:
            response = requests.get(api_url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            
            # API returns data in format: {"Count": 4499, "Data": [...]}
            if isinstance(data, dict):
                finance_data = data.get('Data', [])
                count = data.get('Count', len(finance_data))
                log_message(f"  Fetched {len(finance_data)} customer finance records (Total: {count})")
            elif isinstance(data, list):
                finance_data = data
                log_message(f"  Fetched {len(finance_data)} customer finance records")
            else:
                log_message(f"  Unexpected API response format: {type(data)}", level="WARNING")
                finance_data = []
                
        except requests.exceptions.Timeout:
            error_msg = f"API request timeout after {timeout}s"
            log_message(f"  ✗ {error_msg}", level="ERROR")
            return {"success": False, "stats": {}, "error": error_msg}
        except requests.exceptions.RequestException as e:
            error_msg = f"API request error: {e}"
            log_message(f"  ✗ {error_msg}", level="ERROR")
            return {"success": False, "stats": {}, "error": error_msg}
        except Exception as e:
            error_msg = f"Unexpected error fetching from API: {e}"
            log_message(f"  ✗ {error_msg}", level="ERROR")
            return {"success": False, "stats": {}, "error": error_msg}
        
        if not finance_data:
            log_message("  No finance data received from API", level="WARNING")
            return {"success": False, "stats": {}, "error": "No data received from API"}
        
        # Step 2: Send to VPS
        log_message("[STEP 2] Sending data to VPS via HTTP API...")
        
        vps_url = f"{VPS_BASE_URL}/customers/sync-finance-api-receive/"
        payload = {
            "finance_data": finance_data,
            "api_key": VPS_API_KEY,
            "sync_metadata": {
                "total_count": len(finance_data),
                "sync_time": datetime.now().isoformat(),
            }
        }
        
        log_message(f"  Sending {len(finance_data)} customer finance records to VPS...")
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
            created = stats.get("created", 0)
            updated = stats.get("updated", 0)
            
            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()
            
            log_message("=" * 70)
            log_message("SYNC COMPLETED SUCCESSFULLY")
            log_message("=" * 70)
            log_message(f"Duration: {sync_duration:.2f} seconds")
            log_message(f"Total Records: {len(finance_data)}")
            log_message(f"Created: {created}")
            log_message(f"Updated: {updated}")
            log_message("=" * 70)
            
            return {
                "success": True,
                "stats": {
                    "created": created,
                    "updated": updated,
                    "total_records": len(finance_data),
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
        result = sync_customer_finance()
        if not result.get("success"):
            log_message(f"Scheduled sync completed with error: {result.get('error')}", level="WARNING")
    except Exception as e:
        log_message(f"Error in scheduled sync: {e}", level="ERROR")
        log_message(traceback.format_exc(), level="ERROR")


def main():
    """Main entry point with argument parsing."""
    
    parser = argparse.ArgumentParser(
        description="Sync Customer Finance Summary from SAP API to VPS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_customer_finance_pc.py               # Runs every 1 hour (service mode)
  python sync_customer_finance_pc.py --once        # Single run
        """
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (default: run as background service)'
    )
    
    args = parser.parse_args()
    
    if args.once:
        # Single run
        log_message("Running single sync...")
        result = sync_customer_finance()
        sys.exit(0 if result.get("success") else 1)
    else:
        # Service mode - run every hour
        log_message(f"Starting customer finance sync service (every {SYNC_INTERVAL_MINUTES} minutes)...")
        log_message("Press Ctrl+C to stop")
        log_message("")
        
        # Run immediately on startup
        run_scheduled_sync()
        
        # Schedule recurring runs
        schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(run_scheduled_sync)
        
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            log_message("\nService stopped by user")


if __name__ == "__main__":
    main()
