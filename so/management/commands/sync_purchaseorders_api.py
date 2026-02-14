#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync to fetch purchase orders from SAP API and push to VPS.
Runs on local PC; can access http://192.168.1.103/IntegrationApi/api/PurchaseOrder

Usage:
    python manage.py sync_purchaseorders_api
    python manage.py sync_purchaseorders_api --docnum 12345
    python manage.py sync_purchaseorders_api --local-only  # Testing only
"""

import sys
import os
import requests
from datetime import datetime
from pathlib import Path
from django.core.management.base import BaseCommand
from so.api_client import SAPAPIClient
from django.conf import settings
import logging
from logging.handlers import RotatingFileHandler

VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')
VPS_API_KEY = os.getenv('VPS_API_KEY', 'rLEkUZQiljwQWPS5ZJ8m6zawpsr9QUvRqYka-hj7fBw')

# __file__ is in: salesorder/so/management/commands/sync_purchaseorders_api.py
BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_purchaseorders.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('sync_purchaseorders')
logger.setLevel(logging.INFO)
logger.handlers = []

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = 'Sync purchase orders from SAP API to VPS via HTTP API (runs on PC, sends to VPS)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--docnum',
            type=int,
            default=None,
            help='Single document number to fetch (default: fetch all open POs)'
        )
        parser.add_argument(
            '--local-only',
            action='store_true',
            help='Only save to local database (for testing, does not sync to VPS)'
        )

    def handle(self, *args, **options):
        docnum = options['docnum']
        local_only = options['local_only']

        sync_start_time = datetime.now()

        logger.info('=' * 70)
        logger.info('SAP Open Purchase Order Sync (PC -> VPS via HTTP)')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start_time.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'Local API: {getattr(settings, "SAP_PURCHASE_ORDER_API_URL", "")}')
        logger.info(f'VPS URL: {VPS_BASE_URL}')
        logger.info(f'Mode: {"LOCAL ONLY (Testing)" if local_only else "VPS SYNC"}')
        if docnum:
            logger.info(f'DocNum filter: {docnum}')
        else:
            logger.info('Fetching: DocumentStatus=bost_Open (all pages)')
        logger.info('-' * 70)

        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('SAP Open Purchase Order Sync (PC -> VPS via HTTP)'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(f'Local API: {getattr(settings, "SAP_PURCHASE_ORDER_API_URL", "")}')
        self.stdout.write(f'VPS URL: {VPS_BASE_URL}')
        self.stdout.write(f'Log file: {LOG_FILE}')
        self.stdout.write('-' * 70)

        if VPS_API_KEY == 'your-secret-api-key':
            error_msg = 'ERROR: Please configure VPS_API_KEY!'
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(f'\n{error_msg}'))
            self.stdout.write('Set environment variable: VPS_API_KEY=your-key')
            return

        logger.info('[STEP 1] Fetching data from SAP API...')
        self.stdout.write('\n[STEP 1] Fetching data from SAP API...')
        client = SAPAPIClient()
        all_orders = []
        api_calls = 0

        try:
            if docnum:
                logger.info(f'  Fetching purchase order by DocNum: {docnum}...')
                self.stdout.write(f'  Fetching purchase order by DocNum: {docnum}...')
                orders = client.fetch_purchaseorders_by_docnum(docnum)
                all_orders.extend(orders)
                api_calls = 1
                logger.info(f'  Found {len(orders)} orders for DocNum {docnum}')
            else:
                logger.info('  Fetching OPEN purchase orders (DocumentStatus=bost_Open, all pages)...')
                self.stdout.write('  Fetching OPEN purchase orders (all pages)...')
                open_orders = client.fetch_open_purchaseorders()
                seen_docnums = set()
                for order in open_orders:
                    docnum_val = order.get('DocNum')
                    if docnum_val and docnum_val not in seen_docnums:
                        all_orders.append(order)
                        seen_docnums.add(docnum_val)
                api_calls = max(1, (len(open_orders) + 19) // 20)
                logger.info(f'  Found {len(all_orders)} open purchase orders')

            self.stdout.write(self.style.SUCCESS(f'\n  ✓ Fetched {len(all_orders)} open purchase orders'))

            if not all_orders:
                logger.warning('  No purchase orders found.')
                self.stdout.write(self.style.WARNING('  No purchase orders found.'))
                return

            logger.info('[STEP 2] Mapping API responses...')
            self.stdout.write('\n[STEP 2] Mapping API responses...')
            mapped_orders = []
            mapping_errors = []
            for api_order in all_orders:
                try:
                    mapped = client._map_purchaseorder_api_response(api_order)
                    mapped_orders.append(mapped)
                except Exception as e:
                    error_msg = f"Error mapping order {api_order.get('DocNum')}: {e}"
                    logger.error(error_msg)
                    logger.exception(error_msg)
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

            api_po_numbers = [m['po_number'] for m in mapped_orders if m.get('po_number')]

            def serialize_order(order):
                serialized = order.copy()
                if 'posting_date' in serialized and serialized['posting_date']:
                    if hasattr(serialized['posting_date'], 'isoformat'):
                        serialized['posting_date'] = serialized['posting_date'].isoformat()
                    elif isinstance(serialized['posting_date'], str):
                        pass
                    else:
                        serialized['posting_date'] = None
                return serialized

            serialized_orders = [serialize_order(order) for order in mapped_orders]

            if local_only:
                logger.info('[STEP 3] Saving to LOCAL database only (testing mode)...')
                self.stdout.write('\n[STEP 3] Saving to LOCAL database only (testing mode)...')
                from so.sap_purchaseorder_views import save_purchaseorders_locally
                stats = save_purchaseorders_locally(serialized_orders, api_po_numbers)
                logger.info(f'  ✓ Saved locally: replaced={stats.get("replaced", 0)}, items={stats.get("total_items", 0)}')
                self.stdout.write(self.style.SUCCESS(f'  ✓ Saved to local DB: Replaced={stats.get("replaced", 0)}, Items={stats.get("total_items", 0)}'))
            else:
                logger.info('[STEP 3] Sending data to VPS via HTTP API...')
                self.stdout.write('\n[STEP 3] Sending data to VPS via HTTP API...')
                try:
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

                    logger.info(f'  Sending {len(serialized_orders)} purchase orders to VPS...')
                    self.stdout.write(f'  Sending {len(serialized_orders)} purchase orders to VPS...')

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

                        logger.info(f'  ✓ Successfully synced to VPS (took {send_duration:.2f}s)')
                        self.stdout.write(self.style.SUCCESS(f'  ✓ Successfully synced to VPS'))
                        self.stdout.write(f'    Replaced: {replaced} open purchase orders')
                        self.stdout.write(f'    Total Items: {total_items}')
                    else:
                        logger.error(f'  ✗ VPS sync failed: {error}')
                        self.stdout.write(self.style.ERROR(f'  ✗ Failed: {error}'))
                        return

                except requests.HTTPError as e:
                    resp = getattr(e, "response", None)
                    status_code = getattr(resp, "status_code", None)
                    try:
                        resp_text = resp.text if resp is not None else ""
                    except Exception:
                        resp_text = ""
                    error_msg = f"Failed to send to VPS: HTTP {status_code} for url: {vps_url}"
                    logger.error(f'  ✗ {error_msg}')
                    if resp_text:
                        logger.error(resp_text[:4000])
                    self.stdout.write(self.style.ERROR(f'  ✗ {error_msg}'))
                    if resp_text:
                        self.stdout.write(self.style.ERROR(resp_text[:1000]))
                    return
                except requests.RequestException as e:
                    error_msg = f"Failed to send to VPS: {str(e)}"
                    logger.error(f'  ✗ {error_msg}')
                    self.stdout.write(self.style.ERROR(f'  ✗ {error_msg}'))
                    return

            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()

            logger.info('=' * 70)
            logger.info('SYNC SUMMARY')
            logger.info('=' * 70)
            logger.info(f'Duration: {sync_duration:.2f} seconds')
            logger.info(f'API Calls: {api_calls}')
            logger.info(f'Total Purchase Orders Processed: {len(mapped_orders)}')
            logger.info('=' * 70)

            self.stdout.write(self.style.SUCCESS('\n' + '=' * 70))
            self.stdout.write(self.style.SUCCESS('SYNC SUMMARY'))
            self.stdout.write(self.style.SUCCESS('=' * 70))
            self.stdout.write(f'API Calls: {api_calls}')
            self.stdout.write(f'Total Purchase Orders Processed: {len(mapped_orders)}')
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
            logger.exception('Full error traceback:')
            self.stdout.write(self.style.ERROR(f'\n✗ Error during sync: {e}'))
            raise
