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
    python manage.py sync_quotations_api
    python manage.py sync_quotations_api --days-back 7
    python manage.py sync_quotations_api --local-only  # Only save to local DB (for testing)
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

# Configuration - EDIT THESE
VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')  # Production VPS URL
VPS_API_KEY = os.getenv('VPS_API_KEY', 'rLEkUZQiljwQWPS5ZJ8m6zawpsr9QUvRqYka-hj7fBw')  # Must match VPS

# Log file configuration
# Calculate log directory from project root
# __file__ is in: salesorder/so/management/commands/sync_quotations_api.py
# Go up 4 levels to get to salesorder/ directory
BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_quotations.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5  # Keep 5 backup files

# Ensure log directory exists
LOG_DIR.mkdir(exist_ok=True)

# Configure logging
logger = logging.getLogger('sync_quotations')
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
    help = 'Sync quotations from SAP API to VPS via HTTP API (runs on PC, sends to VPS)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days-back',
            type=int,
            default=getattr(settings, 'SAP_SYNC_DAYS_BACK', 3),
            help='Number of days to fetch for new quotations (default: 3, i.e., today + last 2 days = 3 days total)'
        )
        parser.add_argument(
            '--local-only',
            action='store_true',
            help='Only save to local database (for testing, does not sync to VPS)'
        )

    def handle(self, *args, **options):
        days_back = options['days_back']
        local_only = options['local_only']
        
        sync_start_time = datetime.now()
        
        logger.info('=' * 70)
        logger.info('SAP Quotation Sync (PC -> VPS via HTTP)')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start_time.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'Local API: {getattr(settings, "SAP_QUOTATION_API_URL", "")}')
        logger.info(f'VPS URL: {VPS_BASE_URL}')
        logger.info(f'Mode: {"LOCAL ONLY (Testing)" if local_only else "VPS SYNC"}')
        logger.info(f'Days back: {days_back}')
        logger.info('-' * 70)
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('SAP Quotation Sync (PC -> VPS via HTTP)'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(f'Local API: {getattr(settings, "SAP_QUOTATION_API_URL", "")}')
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
        api_calls = 0
        
        try:
            # Fetch open quotations (last 15 pages) + last N days
            logger.info(f'  Fetching quotations (open last 15 pages + last {days_back} days)...')
            self.stdout.write(f'  Fetching quotations (open last 15 pages + last {days_back} days)...')
            all_quotations = client.sync_all_quotations(days_back=days_back)
            api_calls = 1 + days_back  # 1 for open quotations + N for days
            
            logger.info(f'  Found {len(all_quotations)} quotations')
            self.stdout.write(self.style.SUCCESS(f'\n  ✓ Fetched {len(all_quotations)} quotations'))
            
            if not all_quotations:
                logger.warning('  No quotations found.')
                self.stdout.write(self.style.WARNING('  No quotations found.'))
                return
            
            # Step 2: Map API responses to model format
            logger.info('[STEP 2] Mapping API responses...')
            self.stdout.write('\n[STEP 2] Mapping API responses...')
            mapped_quotations = []
            mapping_errors = []
            for api_quotation in all_quotations:
                try:
                    mapped = client._map_quotation_api_response_to_model(api_quotation)
                    mapped_quotations.append(mapped)
                except Exception as e:
                    error_msg = f"Error mapping quotation {api_quotation.get('DocNum')}: {e}"
                    logger.error(error_msg)
                    logger.exception(f"Error mapping quotation {api_quotation.get('DocNum')}")
                    self.stdout.write(self.style.ERROR(f"  {error_msg}"))
                    mapping_errors.append(error_msg)
            
            if not mapped_quotations:
                logger.error('  No quotations could be mapped successfully.')
                self.stdout.write(self.style.ERROR('  No quotations could be mapped successfully.'))
                return
            
            if mapping_errors:
                logger.warning(f'  {len(mapping_errors)} quotations failed to map (out of {len(all_quotations)})')
            
            logger.info(f'  Successfully mapped {len(mapped_quotations)} quotations')
            self.stdout.write(self.style.SUCCESS(f'  ✓ Mapped {len(mapped_quotations)} quotations'))
            
            # Get list of quotation numbers from API response (for closing missing quotations)
            api_q_numbers = [m['q_number'] for m in mapped_quotations if m.get('q_number')]
            
            # Step 3: Serialize dates to strings for JSON
            def serialize_quotation(quotation):
                """Convert date objects to strings for JSON serialization"""
                serialized = quotation.copy()
                if 'posting_date' in serialized and serialized['posting_date']:
                    if hasattr(serialized['posting_date'], 'isoformat'):
                        serialized['posting_date'] = serialized['posting_date'].isoformat()
                    elif isinstance(serialized['posting_date'], str):
                        pass  # Already a string
                    else:
                        serialized['posting_date'] = None
                return serialized
            
            serialized_quotations = [serialize_quotation(q) for q in mapped_quotations]
            
            # Step 4: Send to VPS via HTTP API or save locally
            if local_only:
                logger.info('[STEP 3] Saving to LOCAL database only (testing mode)...')
                self.stdout.write('\n[STEP 3] Saving to LOCAL database only (testing mode)...')
                
                # Import models and transaction
                from so.models import SAPQuotation, SAPQuotationItem
                from django.db import transaction
                from decimal import Decimal
                import pandas as pd
                
                stats = {
                    'created': 0,
                    'updated': 0,
                    'closed': 0,
                    'total_items': 0
                }
                
                q_numbers = [m['q_number'] for m in mapped_quotations if m.get('q_number')]
                api_q_numbers_set = set(api_q_numbers)
                
                with transaction.atomic():
                    # Fetch existing quotations
                    try:
                        existing_map = {q.q_number: q for q in SAPQuotation.objects.filter(q_number__in=q_numbers)}
                    except Exception:
                        existing_map = {}
                    
                    to_create = []
                    to_update = []
                    
                    def _dec2(x) -> Decimal:
                        try:
                            if x is None or (isinstance(x, float) and pd.isna(x)):
                                return Decimal("0.00")
                            return Decimal(str(x)).quantize(Decimal("0.01"))
                        except Exception:
                            return Decimal("0.00")
                    
                    # Process each mapped quotation
                    for mapped in mapped_quotations:
                        q_no = mapped.get('q_number')
                        if not q_no:
                            continue
                        
                        # Parse posting_date if it's a string
                        posting_date = mapped.get('posting_date')
                        if isinstance(posting_date, str):
                            try:
                                posting_date = datetime.strptime(posting_date, '%Y-%m-%d').date()
                            except (ValueError, TypeError):
                                posting_date = None
                        elif posting_date and hasattr(posting_date, 'date'):
                            posting_date = posting_date.date() if hasattr(posting_date, 'date') else posting_date
                        
                        defaults = {
                            "posting_date": posting_date,
                            "customer_code": mapped.get('customer_code', ''),
                            "customer_name": mapped.get('customer_name', ''),
                            "bp_reference_no": mapped.get('bp_reference_no', ''),
                            "salesman_name": mapped.get('salesman_name', ''),
                            "document_total": _dec2(mapped.get('document_total', 0)),
                            "vat_sum": _dec2(mapped.get('vat_sum', 0)),
                            "total_discount": _dec2(mapped.get('total_discount', 0)),
                            "rounding_diff_amount": _dec2(mapped.get('rounding_diff_amount', 0)),
                            "discount_percent": _dec2(mapped.get('discount_percent', 0)),
                            "status": mapped.get('status', 'CLOSED'),
                            "bill_to": mapped.get('bill_to', '') or '',
                            "remarks": mapped.get('remarks', '') or '',
                        }
                        
                        if mapped.get('internal_number'):
                            defaults["internal_number"] = mapped.get('internal_number')
                        
                        obj = existing_map.get(q_no)
                        if obj is None:
                            to_create.append(SAPQuotation(q_number=q_no, **defaults))
                            stats['created'] += 1
                        else:
                            for k, v in defaults.items():
                                setattr(obj, k, v)
                            to_update.append(obj)
                            stats['updated'] += 1
                    
                    # Bulk create/update
                    if to_create:
                        SAPQuotation.objects.bulk_create(to_create, batch_size=5000)
                        logger.info(f'  Created {len(to_create)} quotations')
                        self.stdout.write(self.style.SUCCESS(f'  Created {len(to_create)} quotations'))
                    
                    if to_update:
                        update_fields = [
                            "posting_date", "customer_code", "customer_name", "bp_reference_no",
                            "salesman_name", "document_total", "vat_sum", "total_discount",
                            "rounding_diff_amount", "discount_percent", "status", "bill_to",
                            "remarks", "internal_number"
                        ]
                        SAPQuotation.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)
                        logger.info(f'  Updated {len(to_update)} quotations')
                        self.stdout.write(self.style.SUCCESS(f'  Updated {len(to_update)} quotations'))
                    
                    # Re-fetch ids for FK mapping
                    quotation_id_map = dict(
                        SAPQuotation.objects.filter(q_number__in=q_numbers).values_list("q_number", "id")
                    )
                    
                    # Delete existing items for these quotations
                    SAPQuotationItem.objects.filter(quotation__q_number__in=q_numbers).delete()
                    
                    # Build items list + bulk insert
                    items_to_create = []
                    
                    def _dec_any(x) -> Decimal:
                        try:
                            if x is None or (isinstance(x, float) and pd.isna(x)):
                                return Decimal("0")
                            return Decimal(str(x))
                        except Exception:
                            return Decimal("0")
                    
                    for mapped in mapped_quotations:
                        q_no = mapped.get('q_number')
                        q_id = quotation_id_map.get(q_no)
                        if not q_id:
                            continue
                        
                        for item_data in mapped.get('items', []):
                            items_to_create.append(
                                SAPQuotationItem(
                                    quotation_id=q_id,
                                    item_no=item_data.get('item_no', ''),
                                    description=item_data.get('description', ''),
                                    quantity=_dec_any(item_data.get('quantity', 0)),
                                    price=_dec_any(item_data.get('price', 0)),
                                    row_total=_dec_any(item_data.get('row_total', 0)),
                                )
                            )
                            
                            if len(items_to_create) >= 20000:
                                SAPQuotationItem.objects.bulk_create(items_to_create, batch_size=20000)
                                items_to_create = []
                    
                    if items_to_create:
                        SAPQuotationItem.objects.bulk_create(items_to_create, batch_size=20000)
                    
                    stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_quotations)
                    
                    # Close missing quotations
                    previously_open_quotations = SAPQuotation.objects.filter(
                        status__in=['O', 'OPEN', 'Open', 'open'],
                        q_number__isnull=False
                    ).exclude(q_number__in=api_q_numbers_set)
                    
                    closed_count = 0
                    for quotation in previously_open_quotations:
                        quotation.status = 'CLOSED'
                        quotation.save(update_fields=['status'])
                        closed_count += 1
                    
                    stats['closed'] = closed_count
                    
                    logger.info(f'  Closed {closed_count} quotations')
                    self.stdout.write(self.style.SUCCESS(f'  Closed {closed_count} quotations'))
                    
                    # Summary
                    sync_end_time = datetime.now()
                    sync_duration = (sync_end_time - sync_start_time).total_seconds()
                    
                    logger.info('=' * 70)
                    logger.info('LOCAL SYNC COMPLETED SUCCESSFULLY')
                    logger.info('=' * 70)
                    logger.info(f'Duration: {sync_duration:.2f} seconds')
                    logger.info(f'API Calls: {api_calls}')
                    logger.info(f'Total Quotations: {len(mapped_quotations)}')
                    logger.info(f'Created: {stats["created"]}')
                    logger.info(f'Updated: {stats["updated"]}')
                    logger.info(f'Closed: {stats["closed"]}')
                    logger.info(f'Total Items: {stats["total_items"]}')
                    logger.info('=' * 70)
                    
                    self.stdout.write(self.style.SUCCESS('\n' + '=' * 70))
                    self.stdout.write(self.style.SUCCESS('LOCAL SYNC COMPLETED SUCCESSFULLY'))
                    self.stdout.write(self.style.SUCCESS('=' * 70))
                    self.stdout.write(f'Duration: {sync_duration:.2f} seconds')
                    self.stdout.write(f'API Calls: {api_calls}')
                    self.stdout.write(f'Total Quotations: {len(mapped_quotations)}')
                    self.stdout.write(f'Created: {stats["created"]}')
                    self.stdout.write(f'Updated: {stats["updated"]}')
                    self.stdout.write(f'Closed: {stats["closed"]}')
                    self.stdout.write(f'Total Items: {stats["total_items"]}')
                    self.stdout.write('=' * 70)
            else:
                logger.info('[STEP 3] Sending data to VPS via HTTP API...')
                self.stdout.write('\n[STEP 3] Sending data to VPS via HTTP API...')
                try:
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
                    
                    logger.info(f'  Sending {len(serialized_quotations)} quotations to VPS...')
                    logger.info(f'  VPS URL: {vps_url}')
                    self.stdout.write(f'  Sending {len(serialized_quotations)} quotations to VPS...')
                    
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
                        
                        self.stdout.write(self.style.SUCCESS(f'\n  ✓ Successfully synced to VPS (took {send_duration:.2f}s)'))
                        self.stdout.write(f'    Created: {created}')
                        self.stdout.write(f'    Updated: {updated}')
                        self.stdout.write(f'    Closed: {closed}')
                        self.stdout.write(f'    Total Items: {total_items}')
                        
                        # Summary
                        sync_end_time = datetime.now()
                        sync_duration = (sync_end_time - sync_start_time).total_seconds()
                        
                        logger.info('=' * 70)
                        logger.info('SYNC COMPLETED SUCCESSFULLY')
                        logger.info('=' * 70)
                        logger.info(f'Duration: {sync_duration:.2f} seconds')
                        logger.info(f'API Calls: {api_calls}')
                        logger.info(f'Total Quotations: {len(mapped_quotations)}')
                        logger.info('=' * 70)
                        
                        self.stdout.write(self.style.SUCCESS('\n' + '=' * 70))
                        self.stdout.write(self.style.SUCCESS('SYNC COMPLETED SUCCESSFULLY'))
                        self.stdout.write(self.style.SUCCESS('=' * 70))
                        self.stdout.write(f'Duration: {sync_duration:.2f} seconds')
                        self.stdout.write(f'API Calls: {api_calls}')
                        self.stdout.write(f'Total Quotations: {len(mapped_quotations)}')
                        self.stdout.write('=' * 70)
                    else:
                        error_msg = f"VPS sync failed: {error}"
                        logger.error(f'  ✗ {error_msg}')
                        self.stdout.write(self.style.ERROR(f'\n  ✗ {error_msg}'))
                        
                except requests.HTTPError as e:
                    resp = getattr(e, "response", None)
                    status_code = getattr(resp, "status_code", None)
                    resp_text = ""
                    try:
                        resp_text = resp.text if resp is not None else ""
                    except Exception:
                        resp_text = ""
                    
                    error_msg = f"Failed to send to VPS: HTTP {status_code}"
                    logger.error(f'  ✗ {error_msg}')
                    if resp_text:
                        logger.error(f'  VPS response (truncated): {resp_text[:1000]}')
                    self.stdout.write(self.style.ERROR(f'\n  ✗ {error_msg}'))
                    
                except requests.RequestException as e:
                    error_msg = f"Failed to send to VPS: {str(e)}"
                    logger.error(f'  ✗ {error_msg}')
                    self.stdout.write(self.style.ERROR(f'\n  ✗ {error_msg}'))
                    
        except Exception as e:
            sync_end_time = datetime.now()
            sync_duration = (sync_end_time - sync_start_time).total_seconds()
            
            logger.exception('Error in sync_quotations_api')
            logger.error('=' * 70)
            logger.error('SYNC FAILED')
            logger.error('=' * 70)
            logger.error(f'Error: {str(e)}')
            logger.error(f'Duration: {sync_duration:.2f} seconds')
            logger.error('=' * 70)
            
            self.stdout.write(self.style.ERROR('\n' + '=' * 70))
            self.stdout.write(self.style.ERROR('SYNC FAILED'))
            self.stdout.write(self.style.ERROR('=' * 70))
            self.stdout.write(self.style.ERROR(f'Error: {str(e)}'))
            self.stdout.write(self.style.ERROR(f'Duration: {sync_duration:.2f} seconds'))
            self.stdout.write('=' * 70)
