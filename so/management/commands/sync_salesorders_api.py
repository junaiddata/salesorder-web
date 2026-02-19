#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync script to fetch sales orders from SAP API and push to VPS.
This script runs on your local PC and can access http://192.168.1.103/IntegrationApi/api/SalesOrder

WORKFLOW:
1. PC script fetches data from local SAP API (192.168.1.103)
2. PC script sends data to VPS via HTTP API endpoint
3. VPS updates its database

Usage:
    python manage.py sync_salesorders_api
    python manage.py sync_salesorders_api --days-back 7
    python manage.py sync_salesorders_api --date 2026-01-21
    python manage.py sync_salesorders_api --local-only  # Only save to local DB (for testing)
"""

import sys
import os
import requests
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from django.core.management.base import BaseCommand
from so.api_client import SAPAPIClient
from django.conf import settings
import logging
from logging.handlers import RotatingFileHandler
import pandas as pd
from decimal import Decimal

# Configuration - EDIT THESE
VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')  # Production VPS URL
VPS_API_KEY = os.getenv('VPS_API_KEY', 'rLEkUZQiljwQWPS5ZJ8m6zawpsr9QUvRqYka-hj7fBw')  # Must match VPS

# Log file configuration
# Calculate log directory from project root
# __file__ is in: salesorder/so/management/commands/sync_salesorders_api.py
# Go up 4 levels to get to salesorder/ directory
BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_salesorders.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5  # Keep 5 backup files

# Ensure log directory exists
LOG_DIR.mkdir(exist_ok=True)

# Configure logging
logger = logging.getLogger('sync_salesorders')
logger.setLevel(logging.INFO)

# Remove existing handlers to avoid duplicates
logger.handlers = []

# File handler with rotation
file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Console handler (for stdout)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = 'Sync sales orders from SAP API to VPS via HTTP API (runs on PC, sends to VPS)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days-back',
            type=int,
            default=getattr(settings, 'SAP_SYNC_DAYS_BACK', 3),
            help='Number of days to fetch for new orders (default: 3, i.e., today + last 3 days = 4 days total)'
        )
        parser.add_argument(
            '--date',
            type=str,
            default=None,
            help='Single date to fetch (YYYY-MM-DD format)'
        )
        parser.add_argument(
            '--docnum',
            type=int,
            default=None,
            help='Single document number to fetch'
        )
        parser.add_argument(
            '--local-only',
            action='store_true',
            help='Only save to local database (for testing, does not sync to VPS)'
        )

    def handle(self, *args, **options):
        days_back = options['days_back']
        specific_date = options['date']
        docnum = options['docnum']
        local_only = options['local_only']
        
        sync_start_time = datetime.now()
        
        logger.info('=' * 70)
        logger.info('SAP Sales Order Sync (PC -> VPS via HTTP)')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start_time.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'Local API: {getattr(settings, "SAP_API_BASE_URL", "")}')
        logger.info(f'VPS URL: {VPS_BASE_URL}')
        logger.info(f'Mode: {"LOCAL ONLY (Testing)" if local_only else "VPS SYNC"}')
        if specific_date:
            logger.info(f'Date filter: {specific_date}')
        elif docnum:
            logger.info(f'DocNum filter: {docnum}')
        else:
            logger.info(f'Days back: {days_back}')
        logger.info('-' * 70)
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('SAP Sales Order Sync (PC -> VPS via HTTP)'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(f'Local API: {getattr(settings, "SAP_API_BASE_URL", "")}')
        self.stdout.write(f'VPS URL: {VPS_BASE_URL}')
        self.stdout.write(f'Log file: {LOG_FILE}')
        self.stdout.write('-' * 70)
        
        # Check configuration
        if VPS_API_KEY == 'your-secret-api-key':
            error_msg = 'ERROR: Please configure VPS_API_KEY!'
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(f'\n{error_msg}'))
            self.stdout.write('Set environment variable: VPS_API_KEY=your-key')
            self.stdout.write('Or edit this script and set VPS_API_KEY')
            return
        
        # Step 1: Fetch from SAP API (on PC)
        logger.info('[STEP 1] Fetching data from SAP API...')
        self.stdout.write('\n[STEP 1] Fetching data from SAP API...')
        client = SAPAPIClient()
        all_orders = []
        api_calls = 0
        
        try:
            if docnum:
                # Single DocNum query
                logger.info(f'  Fetching sales order by DocNum: {docnum}...')
                self.stdout.write(f'  Fetching sales order by DocNum: {docnum}...')
                orders = client.fetch_salesorders_by_docnum(docnum)
                all_orders.extend(orders)
                api_calls = 1
                logger.info(f'  Found {len(orders)} orders for DocNum {docnum}')
            elif specific_date:
                # Single date query
                logger.info(f'  Fetching sales orders for date: {specific_date}...')
                self.stdout.write(f'  Fetching sales orders for date: {specific_date}...')
                orders = client.fetch_salesorders_by_date(specific_date)
                all_orders.extend(orders)
                api_calls = 1
                logger.info(f'  Found {len(orders)} orders for date {specific_date}')
            else:
                # Default: sync all (open orders + last N days)
                logger.info('  Fetching open orders (with pagination)...')
                self.stdout.write(f'  Fetching open orders (with pagination)...')
                open_orders = client.fetch_open_salesorders()
                seen_docnums = set()
                for order in open_orders:
                    docnum_val = order.get('DocNum')
                    if docnum_val and docnum_val not in seen_docnums:
                        all_orders.append(order)
                        seen_docnums.add(docnum_val)
                # Note: api_calls count is now handled inside fetch_open_salesorders (multiple pages)
                # We'll estimate based on total records / 20 per page
                api_calls += max(1, (len(open_orders) + 19) // 20)  # Ceiling division
                logger.info(f'  Found {len(open_orders)} open orders ({len(all_orders)} unique)')
                
                logger.info(f'  Fetching new orders from last {days_back} days (with pagination)...')
                self.stdout.write(f'  Fetching new orders from last {days_back} days (with pagination)...')
                for i in range(days_back):
                    date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                    logger.info(f'    Day {i+1}/{days_back}: {date}...')
                    self.stdout.write(f'    Day {i+1}/{days_back}: {date}...')
                    orders = client.fetch_salesorders_by_date(date)
                    for order in orders:
                        docnum_val = order.get('DocNum')
                        if docnum_val and docnum_val not in seen_docnums:
                            all_orders.append(order)
                            seen_docnums.add(docnum_val)
                    # Estimate API calls based on records (20 per page)
                    api_calls += max(1, (len(orders) + 19) // 20)  # Ceiling division
                    logger.info(f'      Found {len(orders)} orders for {date} ({len([o for o in orders if o.get("DocNum") not in seen_docnums])} new)')
            
            # Filter by HO customers
            orders_before_filter = len(all_orders)
            all_orders = client._filter_ho_customers(all_orders)
            logger.info(f'  Filtered: {orders_before_filter} -> {len(all_orders)} orders (HO customers only)')
            self.stdout.write(self.style.SUCCESS(f'\n  ✓ Fetched {len(all_orders)} orders (after HO filter)'))
            
            if not all_orders:
                logger.warning('  No sales orders found (after filtering by HO customers).')
                self.stdout.write(self.style.WARNING('  No sales orders found (after filtering by HO customers).'))
                return
            
            # Step 2: Map API responses to model format
            logger.info('[STEP 2] Mapping API responses...')
            self.stdout.write('\n[STEP 2] Mapping API responses...')
            mapped_orders = []
            mapping_errors = []
            for api_order in all_orders:
                try:
                    mapped = client._map_api_response_to_model(api_order)
                    mapped_orders.append(mapped)
                except Exception as e:
                    error_msg = f"Error mapping order {api_order.get('DocNum')}: {e}"
                    logger.error(error_msg)
                    logger.exception(f"Error mapping order {api_order.get('DocNum')}")
                    self.stdout.write(self.style.ERROR(f"  {error_msg}"))
                    mapping_errors.append(error_msg)
            
            if not mapped_orders:
                logger.error('  No orders could be mapped successfully.')
                self.stdout.write(self.style.ERROR('  No orders could be mapped successfully.'))
                return
            
            if mapping_errors:
                logger.warning(f'  {len(mapping_errors)} orders failed to map (out of {len(all_orders)})')
            
            logger.info(f'  Successfully mapped {len(mapped_orders)} orders')
            self.stdout.write(self.style.SUCCESS(f'  ✓ Mapped {len(mapped_orders)} orders'))
            
            # Get list of SO numbers from API response (for closing missing orders)
            api_so_numbers = [m['so_number'] for m in mapped_orders if m.get('so_number')]
            
            # Step 3: Serialize dates to strings for JSON
            def serialize_order(order):
                """Convert date objects to strings for JSON serialization"""
                serialized = order.copy()
                if 'posting_date' in serialized and serialized['posting_date']:
                    if hasattr(serialized['posting_date'], 'isoformat'):
                        serialized['posting_date'] = serialized['posting_date'].isoformat()
                    elif isinstance(serialized['posting_date'], str):
                        pass  # Already a string
                    else:
                        serialized['posting_date'] = None
                if 'sap_pi_lpo_date' in serialized and serialized['sap_pi_lpo_date']:
                    if hasattr(serialized['sap_pi_lpo_date'], 'isoformat'):
                        serialized['sap_pi_lpo_date'] = serialized['sap_pi_lpo_date'].isoformat()
                    elif isinstance(serialized['sap_pi_lpo_date'], str):
                        pass
                    else:
                        serialized['sap_pi_lpo_date'] = None
                return serialized
            
            serialized_orders = [serialize_order(order) for order in mapped_orders]
            
            # Step 4: Send to VPS via HTTP API
            if local_only:
                logger.info('[STEP 3] Saving to LOCAL database only (testing mode)...')
                self.stdout.write('\n[STEP 3] Saving to LOCAL database only (testing mode)...')
                from so.sap_salesorder_views import sync_salesorders_from_api
                # For local-only, we can use the existing view logic or save directly
                logger.warning('  Local-only mode: Use web UI sync or implement local save logic')
                self.stdout.write(self.style.WARNING('  Local-only mode: Use web UI sync or implement local save logic'))
            else:
                logger.info('[STEP 3] Sending data to VPS via HTTP API...')
                self.stdout.write('\n[STEP 3] Sending data to VPS via HTTP API...')
                try:
                    vps_url = f"{VPS_BASE_URL}/sapsalesorders/sync-api-receive/"
                    payload = {
                        "orders": serialized_orders,
                        "api_so_numbers": api_so_numbers,
                        "api_key": VPS_API_KEY,
                        "sync_metadata": {
                            "api_calls": api_calls,
                            "days_back": days_back,
                            "sync_time": datetime.now().isoformat(),
                        }
                    }
                    
                    logger.info(f'  Sending {len(serialized_orders)} orders to VPS...')
                    logger.info(f'  VPS URL: {vps_url}')
                    self.stdout.write(f'  Sending {len(serialized_orders)} orders to VPS...')
                    
                    send_start = datetime.now()
                    response = requests.post(vps_url, json=payload, timeout=300)  # 5 min timeout
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
                        
                        logger.info(f'  ✓ Successfully synced to VPS (took {send_duration:.2f}s)')
                        logger.info(f'    Created: {created}')
                        logger.info(f'    Updated: {updated}')
                        logger.info(f'    Closed: {closed}')
                        logger.info(f'    Total Items: {total_items}')
                        
                        self.stdout.write(self.style.SUCCESS(f'  ✓ Successfully synced to VPS'))
                        self.stdout.write(f'    Created: {created}')
                        self.stdout.write(f'    Updated: {updated}')
                        self.stdout.write(f'    Closed: {closed}')
                        self.stdout.write(f'    Total Items: {total_items}')
                    else:
                        logger.error(f'  ✗ VPS sync failed: {error}')
                        self.stdout.write(self.style.ERROR(f'  ✗ Failed: {error}'))
                        return
                        
                except requests.HTTPError as e:
                    # Server returned 4xx/5xx. Log the response body for debugging.
                    resp = getattr(e, "response", None)
                    status_code = getattr(resp, "status_code", None)
                    resp_text = ""
                    try:
                        resp_text = resp.text if resp is not None else ""
                    except Exception:
                        resp_text = ""

                    error_msg = f"Failed to send to VPS: HTTP {status_code} for url: {vps_url}"
                    logger.error(f'  ✗ {error_msg}')
                    if resp_text:
                        logger.error('  VPS response body (truncated):')
                        logger.error(resp_text[:4000])
                    self.stdout.write(self.style.ERROR(f'  ✗ {error_msg}'))
                    if resp_text:
                        self.stdout.write(self.style.ERROR('  VPS response body (truncated):'))
                        # Keep terminal output reasonable
                        self.stdout.write(resp_text[:1000])
                    return
                except requests.RequestException as e:
                    error_msg = f"Failed to send to VPS: {str(e)}"
                    logger.error(f'  ✗ {error_msg}')
                    logger.exception('Error sending to VPS')
                    self.stdout.write(self.style.ERROR(f'  ✗ {error_msg}'))
                    return
            
            # Summary
            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()
            
            logger.info('=' * 70)
            logger.info('SYNC SUMMARY')
            logger.info('=' * 70)
            logger.info(f'Started: {sync_start_time.strftime("%Y-%m-%d %H:%M:%S")}')
            logger.info(f'Ended: {sync_end_time.strftime("%Y-%m-%d %H:%M:%S")}')
            logger.info(f'Duration: {sync_duration:.2f} seconds')
            logger.info(f'API Calls: {api_calls}')
            logger.info(f'Total Orders Processed: {len(mapped_orders)}')
            logger.info('=' * 70)
            logger.info('')  # Empty line for readability
            
            self.stdout.write(self.style.SUCCESS('\n' + '=' * 70))
            self.stdout.write(self.style.SUCCESS('SYNC SUMMARY'))
            self.stdout.write(self.style.SUCCESS('=' * 70))
            self.stdout.write(f'API Calls: {api_calls}')
            self.stdout.write(f'Total Orders Processed: {len(mapped_orders)}')
            self.stdout.write(f'Duration: {sync_duration:.2f} seconds')
            self.stdout.write(f'Log file: {LOG_FILE}')
            self.stdout.write(self.style.SUCCESS('=' * 70))
            
        except Exception as e:
            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()
            
            logger.error('=' * 70)
            logger.error('SYNC FAILED')
            logger.error('=' * 70)
            logger.error(f'Error: {str(e)}')
            logger.error(f'Duration: {sync_duration:.2f} seconds')
            logger.error('=' * 70)
            logger.exception('Full error traceback:')
            logger.info('')  # Empty line for readability
            
            self.stdout.write(self.style.ERROR(f'\n✗ Error during sync: {e}'))
            logger.exception('Error during sync')
            raise
