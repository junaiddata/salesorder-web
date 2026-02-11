#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync script to fetch quotations from SAP API and push to VPS.
This script runs on your local PC and can access http://192.168.1.103/IntegrationApi/api/SalesQuotations

WORKFLOW:
1. PC script fetches data from local SAP API (192.168.1.103)
2. PC script sends data to VPS via HTTP API endpoint
3. VPS updates its database

Usage:
    python sync_quotations_pc.py           # Runs every 10 minutes (background service)
    python sync_quotations_pc.py --once    # Single run (for testing)
    python sync_quotations_pc.py --days-back 8  # Run once with custom days
"""

import sys
import os
import requests
import json
import traceback
import time
import schedule
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# Add Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'salesorder.settings')

# Setup Django
import django
django.setup()

from django.conf import settings
from so.api_client import SAPAPIClient

# Configuration - EDIT THESE
VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')  # Production VPS URL
VPS_API_KEY = os.getenv('VPS_API_KEY', 'test')  # Must match VPS

# Sync interval in minutes
SYNC_INTERVAL_MINUTES = 10

# Default days back to fetch
DEFAULT_DAYS_BACK = getattr(settings, 'SAP_SYNC_DAYS_BACK', 3)

# Log file path (in logs directory)
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "sync_quotations.log")

# Track if logging failed to avoid spam
_logging_failed = False


def log_message(message: str, also_print: bool = True, level: str = "INFO"):
    """Write message to log file and optionally print to console."""
    global _logging_failed
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] [{level}] {message}\n"
    
    # Only try to log if we haven't failed before
    if not _logging_failed:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8', errors='replace') as f:
                f.write(log_entry)
        except (PermissionError, OSError, IOError) as e:
            # Mark logging as failed and show error once
            _logging_failed = True
            if also_print:
                try:
                    print(f"Warning: Cannot write to log file {LOG_FILE}. Logging to console only.", flush=True)
                except:
                    pass
        except Exception:
            # Other errors - silently fail
            _logging_failed = True
    
    if also_print:
        print(message, flush=True)


def serialize_quotation(quotation: Dict) -> Dict:
    """Convert date objects to strings for JSON serialization."""
    serialized = quotation.copy()
    if 'posting_date' in serialized and serialized['posting_date']:
        if hasattr(serialized['posting_date'], 'isoformat'):
            serialized['posting_date'] = serialized['posting_date'].isoformat()
        elif isinstance(serialized['posting_date'], str):
            pass  # Already a string
        else:
            serialized['posting_date'] = None
    return serialized


def sync_quotations(days_back: int = DEFAULT_DAYS_BACK) -> Dict[str, Any]:
    """
    Fetch quotations from SAP API and sync to VPS.
    
    Returns:
        dict with success, stats, error keys
    """
    sync_start_time = datetime.now()
    
    log_message("=" * 70)
    log_message("SAP Quotation Sync (PC -> VPS via HTTP)")
    log_message("=" * 70)
    log_message(f"Started at: {sync_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_message(f"Local API: {getattr(settings, 'SAP_QUOTATION_API_URL', '')}")
    log_message(f"VPS URL: {VPS_BASE_URL}")
    log_message(f"Days back: {days_back}")
    log_message("-" * 70)
    
    # Check configuration
    if VPS_API_KEY == 'your-secret-api-key':
        error_msg = "ERROR: Please configure VPS_API_KEY!"
        log_message(error_msg, level="ERROR")
        return {"success": False, "error": error_msg}
    
    try:
        # Step 1: Fetch from SAP API
        log_message("[STEP 1] Fetching data from SAP API...")
        client = SAPAPIClient()
        api_calls = 0
        
        # Fetch open quotations (last 15 pages) + last N days
        log_message(f"  Fetching quotations (open last 15 pages + last {days_back} days)...")
        all_quotations = client.sync_all_quotations(days_back=days_back)
        api_calls = 1 + days_back  # 1 for open quotations + N for days
        
        log_message(f"  Found {len(all_quotations)} quotations")
        
        if not all_quotations:
            log_message("  No quotations found.", level="WARNING")
            return {"success": True, "stats": {"total": 0}, "error": None}
        
        log_message(f"  [OK] Fetched {len(all_quotations)} quotations")
        
        # Step 2: Map API responses
        log_message("[STEP 2] Mapping API responses...")
        mapped_quotations = []
        mapping_errors = []
        for api_quotation in all_quotations:
            try:
                mapped = client._map_quotation_api_response_to_model(api_quotation)
                mapped_quotations.append(mapped)
            except Exception as e:
                error_msg = f"Error mapping quotation {api_quotation.get('DocNum')}: {e}"
                log_message(f"  {error_msg}", level="ERROR")
                mapping_errors.append(error_msg)
        
        if not mapped_quotations:
            log_message("  No quotations could be mapped successfully.", level="ERROR")
            return {"success": False, "error": "No quotations could be mapped"}
        
        if mapping_errors:
            log_message(f"  {len(mapping_errors)} quotations failed to map", level="WARNING")
        
        log_message(f"  [OK] Mapped {len(mapped_quotations)} quotations")
        
        # Get quotation numbers for closing logic
        api_q_numbers = [m['q_number'] for m in mapped_quotations if m.get('q_number')]
        
        # Step 3: Serialize and send to VPS
        log_message("[STEP 3] Sending data to VPS via HTTP API...")
        serialized_quotations = [serialize_quotation(q) for q in mapped_quotations]
        
        vps_url = f"{VPS_BASE_URL}/sapquotations/sync-api-receive/"
        payload = {
            "quotations": serialized_quotations,
            "api_q_numbers": api_q_numbers,
            "api_key": VPS_API_KEY,
            "sync_metadata": {
                "api_calls": api_calls,
                "days_back": days_back,
                "sync_time": datetime.now().isoformat(),
            }
        }
        
        log_message(f"  Sending {len(serialized_quotations)} quotations to VPS...")
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
            closed = stats.get("closed", 0)
            total_items = stats.get("total_items", 0)
            
            log_message(f"  [OK] Successfully synced to VPS (took {send_duration:.2f}s)")
            log_message(f"    Created: {created}")
            log_message(f"    Updated: {updated}")
            log_message(f"    Closed: {closed}")
            log_message(f"    Total Items: {total_items}")
            
            # Summary
            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()
            
            log_message("=" * 70)
            log_message("SYNC COMPLETED SUCCESSFULLY")
            log_message("=" * 70)
            log_message(f"Duration: {sync_duration:.2f} seconds")
            log_message(f"API Calls: {api_calls}")
            log_message(f"Total Quotations: {len(mapped_quotations)}")
            log_message("=" * 70)
            
            return {
                "success": True,
                "stats": {
                    "created": created,
                    "updated": updated,
                    "closed": closed,
                    "total_items": total_items,
                    "total_quotations": len(mapped_quotations),
                    "api_calls": api_calls,
                    "duration": sync_duration,
                },
                "error": None
            }
        else:
            log_message(f"  [ERROR] VPS sync failed: {error}", level="ERROR")
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
        log_message(f"  [ERROR] {error_msg}", level="ERROR")
        if resp_text:
            log_message(f"  VPS response (truncated): {resp_text[:1000]}", level="ERROR")
        return {"success": False, "stats": {}, "error": error_msg}
        
    except requests.RequestException as e:
        error_msg = f"Failed to send to VPS: {str(e)}"
        log_message(f"  [ERROR] {error_msg}", level="ERROR")
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


# Global variable to store days_back for scheduled runs
SCHEDULED_DAYS_BACK = DEFAULT_DAYS_BACK


def run_scheduled_sync():
    """Wrapper function for scheduled execution."""
    try:
        result = sync_quotations(days_back=SCHEDULED_DAYS_BACK)
        if not result.get("success"):
            log_message(f"Scheduled sync completed with error: {result.get('error')}", level="WARNING")
    except Exception as e:
        log_message(f"Error in scheduled sync: {e}", level="ERROR")
        log_message(traceback.format_exc(), level="ERROR")


def main():
    """Main entry point with argument parsing."""
    global SCHEDULED_DAYS_BACK
    
    parser = argparse.ArgumentParser(
        description="Sync quotations from SAP API to VPS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_quotations_pc.py               # Runs every 10 minutes (service mode)
  python sync_quotations_pc.py --once        # Single run
  python sync_quotations_pc.py --days-back 8 # Run once with 8 days
  python sync_quotations_pc.py --days-back 8 --service  # Run every 10 min with 8 days
        """
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (default: run as background service)'
    )
    parser.add_argument(
        '--days-back',
        type=int,
        default=DEFAULT_DAYS_BACK,
        help=f'Number of days to fetch (default: {DEFAULT_DAYS_BACK})'
    )
    parser.add_argument(
        '--service',
        action='store_true',
        help='Run as background service even when --days-back is specified'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=SYNC_INTERVAL_MINUTES,
        help=f'Sync interval in minutes for service mode (default: {SYNC_INTERVAL_MINUTES})'
    )
    
    args = parser.parse_args()
    
    # Store days_back for scheduled runs
    SCHEDULED_DAYS_BACK = args.days_back
    
    # Determine mode
    # If --once is provided, run once
    # If --days-back is provided without --service, run once
    # Otherwise, run as service
    run_once = args.once or (args.days_back != DEFAULT_DAYS_BACK and not args.service)
    
    if run_once:
        # One-time run mode
        log_message("=" * 70)
        log_message(f"Running ONE-TIME sync with {args.days_back} days back")
        log_message("=" * 70)
        
        result = sync_quotations(days_back=args.days_back)
        
        if result.get("success"):
            log_message("\n[OK] Sync completed successfully!")
            return 0
        else:
            log_message(f"\n[ERROR] Sync failed: {result.get('error')}", level="ERROR")
            return 1
    else:
        # Background service mode
        interval = args.interval
        
        log_message("=" * 70)
        log_message("PC Quotation Sync Service Started")
        log_message("=" * 70)
        log_message(f"Service will sync every {interval} minutes")
        log_message(f"Days back: {SCHEDULED_DAYS_BACK}")
        log_message(f"Log file: {LOG_FILE}")
        log_message("Press Ctrl+C to stop the service")
        log_message("=" * 70)
        
        # Schedule sync
        schedule.every(interval).minutes.do(run_scheduled_sync)
        
        # Run immediately on startup
        log_message("\nRunning initial sync...")
        run_scheduled_sync()
        
        # Keep running
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            log_message("\n" + "=" * 70)
            log_message("Service stopped by user")
            log_message("=" * 70)
            return 0


if __name__ == "__main__":
    sys.exit(main())
