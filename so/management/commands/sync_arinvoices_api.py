#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC-based sync script to fetch AR Invoices from SAP API and push to VPS.
This script runs on your local PC and can access http://192.168.1.103/IntegrationApi/api/ARInvoice

WORKFLOW:
1. PC script fetches data from local SAP API (192.168.1.103)
2. PC script sends data to VPS via HTTP API endpoint
3. VPS updates its database

Usage:
    python manage.py sync_arinvoices_api
    python manage.py sync_arinvoices_api --days-back 7
    python manage.py sync_arinvoices_api --date 2026-01-21
    python manage.py sync_arinvoices_api --local-only  # Only save to local DB (for testing)
"""

import sys
import os
import requests
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from django.core.management.base import BaseCommand
from django.db import transaction
from so.api_client import SAPAPIClient
from so.models import SAPARInvoice, SAPARInvoiceItem
from django.conf import settings
from decimal import Decimal
import logging
from logging.handlers import RotatingFileHandler

# Configuration - EDIT THESE
VPS_BASE_URL = os.getenv('VPS_BASE_URL', 'https://salesorder.junaidworld.com')  # Production VPS URL
VPS_API_KEY = os.getenv('VPS_API_KEY', 'rLEkUZQiljwQWPS5ZJ8m6zawpsr9QUvRqYka-hj7fBw')  # Must match VPS

# Log file configuration
BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_arinvoices.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5  # Keep 5 backup files

# Ensure log directory exists
LOG_DIR.mkdir(exist_ok=True)

# Configure logging
logger = logging.getLogger('sync_arinvoices')
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
    help = 'Sync AR Invoices from SAP API to VPS via HTTP API (runs on PC, sends to VPS)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days-back',
            type=int,
            default=getattr(settings, 'SAP_SYNC_DAYS_BACK', 3),
            help='Number of days to fetch (default: 3)'
        )
        parser.add_argument(
            '--date',
            type=str,
            default=None,
            help='Single date to fetch (YYYY-MM-DD format)'
        )
        parser.add_argument(
            '--from-date',
            type=str,
            default=None,
            help='Start date for date range (YYYY-MM-DD format). Use with --to-date.'
        )
        parser.add_argument(
            '--to-date',
            type=str,
            default=None,
            help='End date for date range (YYYY-MM-DD format). Use with --from-date.'
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
        specific_date = options.get('date')
        from_date = options.get('from_date')
        to_date = options.get('to_date')
        docnum = options.get('docnum')
        local_only = options.get('local_only', False)
        
        sync_start_time = datetime.now()
        
        logger.info('=' * 70)
        logger.info('SAP AR Invoice Sync (PC -> VPS via HTTP)')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start_time.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'Local API: http://192.168.1.103/IntegrationApi/api/ARInvoice')
        logger.info(f'VPS URL: {VPS_BASE_URL}')
        logger.info(f'Mode: {"LOCAL ONLY (Testing)" if local_only else "VPS SYNC"}')
        if from_date and to_date:
            logger.info(f'Date range filter: {from_date} to {to_date}')
        elif specific_date:
            logger.info(f'Date filter: {specific_date}')
        elif docnum:
            logger.info(f'DocNum filter: {docnum}')
        else:
            logger.info(f'Days back: {days_back}')
        logger.info('-' * 70)
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('SAP AR Invoice Sync (PC -> VPS via HTTP)'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(f'Local API: http://192.168.1.103/IntegrationApi/api/ARInvoice')
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
        all_invoices = []
        api_calls = 0
        
        try:
            if docnum:
                # Single DocNum query - fetch last 30 days and filter
                logger.info(f'  Fetching AR invoice by DocNum: {docnum}...')
                self.stdout.write(f'  Fetching AR invoice by DocNum: {docnum}...')
                end_date = datetime.now()
                start_date = end_date - timedelta(days=30)
                invoices = client.fetch_arinvoices_by_date_range(
                    start_date.strftime('%Y-%m-%d'),
                    end_date.strftime('%Y-%m-%d')
                )
                all_invoices = [inv for inv in invoices if str(inv.get('DocNum', '')) == str(docnum)]
                api_calls = 1
                logger.info(f'  Found {len(all_invoices)} invoices for DocNum {docnum}')
            elif from_date and to_date:
                # Date range query
                logger.info(f'  Fetching AR invoices for date range: {from_date} to {to_date}...')
                self.stdout.write(f'  Fetching AR invoices for date range: {from_date} to {to_date}...')
                invoices = client.fetch_arinvoices_by_date_range(from_date, to_date)
                all_invoices.extend(invoices)
                api_calls = 1
                logger.info(f'  Found {len(invoices)} invoices for date range {from_date} to {to_date}')
            elif specific_date:
                # Single date query
                logger.info(f'  Fetching AR invoices for date: {specific_date}...')
                self.stdout.write(f'  Fetching AR invoices for date: {specific_date}...')
                invoices = client.fetch_arinvoices_by_date_range(specific_date, specific_date)
                all_invoices.extend(invoices)
                api_calls = 1
                logger.info(f'  Found {len(invoices)} invoices for date {specific_date}')
            else:
                # Default: fetch last N days
                logger.info(f'  Fetching AR invoices for last {days_back} days...')
                self.stdout.write(f'  Fetching AR invoices for last {days_back} days...')
                all_invoices = client.fetch_arinvoices_last_n_days(days=days_back)
                api_calls = 1
                logger.info(f'  Found {len(all_invoices)} invoices')
            
            if not all_invoices:
                logger.warning('  No AR invoices found.')
                self.stdout.write(self.style.WARNING('  No AR invoices found.'))
                return
            
            # Step 2: Map API responses to model format
            logger.info('[STEP 2] Mapping API responses...')
            self.stdout.write('\n[STEP 2] Mapping API responses...')
            mapped_invoices = []
            mapping_errors = []
            for api_invoice in all_invoices:
                try:
                    mapped = client._map_arinvoice_api_response(api_invoice)
                    mapped_invoices.append(mapped)
                except Exception as e:
                    error_msg = f"Error mapping invoice {api_invoice.get('DocNum')}: {e}"
                    logger.error(error_msg)
                    logger.exception(f"Error mapping invoice {api_invoice.get('DocNum')}")
                    self.stdout.write(self.style.ERROR(f"  {error_msg}"))
                    mapping_errors.append(error_msg)
            
            if not mapped_invoices:
                logger.error('  No invoices could be mapped successfully.')
                self.stdout.write(self.style.ERROR('  No invoices could be mapped successfully.'))
                return
            
            if mapping_errors:
                logger.warning(f'  {len(mapping_errors)} invoices failed to map (out of {len(all_invoices)})')
            
            logger.info(f'  Successfully mapped {len(mapped_invoices)} invoices')
            self.stdout.write(self.style.SUCCESS(f'  ✓ Mapped {len(mapped_invoices)} invoices'))
            
            # Get list of invoice numbers from API response
            api_invoice_numbers = [m['invoice_number'] for m in mapped_invoices if m.get('invoice_number')]
            
            # Step 3: Serialize dates to strings for JSON
            def serialize_invoice(invoice):
                """Convert date objects to strings for JSON serialization"""
                serialized = invoice.copy()
                if 'posting_date' in serialized and serialized['posting_date']:
                    if hasattr(serialized['posting_date'], 'isoformat'):
                        serialized['posting_date'] = serialized['posting_date'].isoformat()
                    elif isinstance(serialized['posting_date'], str):
                        pass  # Already a string
                    else:
                        serialized['posting_date'] = None
                if 'doc_due_date' in serialized and serialized['doc_due_date']:
                    if hasattr(serialized['doc_due_date'], 'isoformat'):
                        serialized['doc_due_date'] = serialized['doc_due_date'].isoformat()
                    elif isinstance(serialized['doc_due_date'], str):
                        pass
                    else:
                        serialized['doc_due_date'] = None
                return serialized
            
            serialized_invoices = [serialize_invoice(invoice) for invoice in mapped_invoices]
            
            # Initialize local stats
            local_stats = {
                'created': 0,
                'updated': 0,
                'total_items': 0,
            }
            
            # Step 4: Save to database (local or VPS)
            if local_only:
                logger.info('[STEP 3] Saving to LOCAL database only (testing mode)...')
                self.stdout.write('\n[STEP 3] Saving to LOCAL database only (testing mode)...')
                
                # Prepare data for bulk operations
                invoice_numbers = [m['invoice_number'] for m in mapped_invoices if m.get('invoice_number')]
                
                try:
                    with transaction.atomic():
                        # Fetch existing invoices
                        try:
                            existing_map = SAPARInvoice.objects.in_bulk(invoice_numbers, field_name="invoice_number")
                        except TypeError:
                            existing_map = {o.invoice_number: o for o in SAPARInvoice.objects.filter(invoice_number__in=invoice_numbers)}
                        
                        to_create = []
                        to_update = []
                        
                        def _dec2(x) -> Decimal:
                            try:
                                if x is None:
                                    return Decimal("0.00")
                                return Decimal(str(x)).quantize(Decimal("0.01"))
                            except Exception:
                                return Decimal("0.00")
                        
                        # Process each mapped invoice
                        for mapped in mapped_invoices:
                            invoice_no = mapped.get('invoice_number')
                            if not invoice_no:
                                continue
                            
                            # Parse dates if strings
                            posting_date = mapped.get('posting_date')
                            if isinstance(posting_date, str):
                                try:
                                    posting_date = datetime.strptime(posting_date, '%Y-%m-%d').date()
                                except (ValueError, TypeError):
                                    posting_date = None
                            elif posting_date and hasattr(posting_date, 'date'):
                                posting_date = posting_date.date() if hasattr(posting_date, 'date') else posting_date
                            
                            doc_due_date = mapped.get('doc_due_date')
                            if isinstance(doc_due_date, str):
                                try:
                                    doc_due_date = datetime.strptime(doc_due_date, '%Y-%m-%d').date()
                                except (ValueError, TypeError):
                                    doc_due_date = None
                            elif doc_due_date and hasattr(doc_due_date, 'date'):
                                doc_due_date = doc_due_date.date() if hasattr(doc_due_date, 'date') else doc_due_date
                            
                            defaults = {
                                "internal_number": mapped.get('internal_number'),
                                "posting_date": posting_date,
                                "doc_due_date": doc_due_date,
                                "customer_code": mapped.get('customer_code', ''),
                                "customer_name": mapped.get('customer_name', ''),
                                "customer_address": mapped.get('customer_address', ''),
                                "salesman_name": mapped.get('salesman_name', ''),
                                "salesman_code": mapped.get('salesman_code'),
                                "bp_reference_no": mapped.get('bp_reference_no', ''),
                                "doc_total": _dec2(mapped.get('doc_total', 0)),
                                "doc_total_without_vat": _dec2(mapped.get('doc_total_without_vat', 0)),
                                "vat_sum": _dec2(mapped.get('vat_sum', 0)),
                                "rounding_diff_amount": _dec2(mapped.get('rounding_diff_amount', 0)),
                                "discount_percent": _dec2(mapped.get('discount_percent', 0)),
                                "cancel_status": mapped.get('cancel_status', ''),
                                "document_status": mapped.get('document_status', ''),
                                "vat_number": mapped.get('vat_number', ''),
                                "comments": mapped.get('comments', ''),
                            }
                            
                            obj = existing_map.get(invoice_no)
                            if obj is None:
                                to_create.append(SAPARInvoice(invoice_number=invoice_no, **defaults))
                                local_stats['created'] += 1
                            else:
                                for k, v in defaults.items():
                                    setattr(obj, k, v)
                                to_update.append(obj)
                                local_stats['updated'] += 1
                        
                        # Bulk create/update
                        if to_create:
                            SAPARInvoice.objects.bulk_create(to_create, batch_size=5000)
                            logger.info(f'  Created {len(to_create)} invoices')
                            self.stdout.write(self.style.SUCCESS(f'  ✓ Created {len(to_create)} invoices'))
                        
                        if to_update:
                            update_fields = [
                                "internal_number", "posting_date", "doc_due_date", "customer_code", "customer_name",
                                "customer_address", "salesman_name", "salesman_code", "bp_reference_no",
                                "doc_total", "doc_total_without_vat", "vat_sum", "rounding_diff_amount", "discount_percent",
                                "cancel_status", "document_status", "vat_number", "comments"
                            ]
                            SAPARInvoice.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)
                            logger.info(f'  Updated {len(to_update)} invoices')
                            self.stdout.write(self.style.SUCCESS(f'  ✓ Updated {len(to_update)} invoices'))
                        
                        # Re-fetch ids for FK mapping
                        invoice_id_map = dict(
                            SAPARInvoice.objects.filter(invoice_number__in=invoice_numbers).values_list("invoice_number", "id")
                        )
                        
                        # Delete existing items for these invoices
                        SAPARInvoiceItem.objects.filter(invoice__invoice_number__in=invoice_numbers).delete()
                        
                        # Build items list + bulk insert
                        items_to_create = []
                        
                        def _dec_any(x) -> Decimal:
                            try:
                                if x is None:
                                    return Decimal("0")
                                return Decimal(str(x))
                            except Exception:
                                return Decimal("0")
                        
                        for mapped in mapped_invoices:
                            invoice_no = mapped.get('invoice_number')
                            invoice_id = invoice_id_map.get(invoice_no)
                            if not invoice_id:
                                continue
                            
                            for item_data in mapped.get('items', []):
                                item_id = item_data.get('item_id')  # From _ensure_item_exists
                                items_to_create.append(
                                    SAPARInvoiceItem(
                                        invoice_id=invoice_id,
                                        item_id=item_id,  # ForeignKey to Items
                                        line_no=item_data.get('line_no', 1),
                                        item_code=item_data.get('item_code', ''),
                                        item_description=item_data.get('item_description', ''),
                                        quantity=_dec_any(item_data.get('quantity', 0)),
                                        price=_dec_any(item_data.get('price', 0)),
                                        price_after_vat=_dec_any(item_data.get('price_after_vat', 0)),
                                        discount_percent=_dec_any(item_data.get('discount_percent', 0)),
                                        line_total=_dec_any(item_data.get('line_total', 0)),
                                        tax_percentage=_dec_any(item_data.get('tax_percentage', 0)),
                                        tax_total=_dec_any(item_data.get('tax_total', 0)),
                                        upc_code=item_data.get('upc_code', ''),
                                    )
                                )
                                
                                if len(items_to_create) >= 20000:
                                    SAPARInvoiceItem.objects.bulk_create(items_to_create, batch_size=20000)
                                    items_to_create = []
                        
                        if items_to_create:
                            SAPARInvoiceItem.objects.bulk_create(items_to_create, batch_size=20000)
                        
                        local_stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_invoices)
                        
                        logger.info(f'  ✓ Saved {local_stats["total_items"]} items')
                        self.stdout.write(self.style.SUCCESS(f'  ✓ Saved {local_stats["total_items"]} items'))
                        logger.info(f'  ✓ Local save completed: {local_stats["created"]} created, {local_stats["updated"]} updated')
                        self.stdout.write(self.style.SUCCESS(f'  ✓ Local save completed: {local_stats["created"]} created, {local_stats["updated"]} updated'))
                        
                except Exception as e:
                    logger.error(f'  ✗ Error saving to local database: {e}')
                    logger.exception('Error saving to local database')
                    self.stdout.write(self.style.ERROR(f'  ✗ Error saving to local database: {e}'))
                    raise
            else:
                logger.info('[STEP 3] Sending data to VPS via HTTP API...')
                self.stdout.write('\n[STEP 3] Sending data to VPS via HTTP API...')
                try:
                    vps_url = f"{VPS_BASE_URL}/saparinvoices/sync-api-receive/"
                    payload = {
                        "invoices": serialized_invoices,
                        "api_invoice_numbers": api_invoice_numbers,
                        "api_key": VPS_API_KEY,
                        "sync_metadata": {
                            "api_calls": api_calls,
                            "days_back": days_back,
                            "sync_time": datetime.now().isoformat(),
                        }
                    }
                    
                    logger.info(f'  Sending {len(serialized_invoices)} invoices to VPS...')
                    logger.info(f'  VPS URL: {vps_url}')
                    self.stdout.write(f'  Sending {len(serialized_invoices)} invoices to VPS...')
                    
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
                        total_items = stats.get("total_items", 0)
                        
                        logger.info(f'  ✓ Successfully synced to VPS (took {send_duration:.2f}s)')
                        logger.info(f'    Created: {created}')
                        logger.info(f'    Updated: {updated}')
                        logger.info(f'    Total Items: {total_items}')
                        
                        self.stdout.write(self.style.SUCCESS(f'  ✓ Successfully synced to VPS'))
                        self.stdout.write(f'    Created: {created}')
                        self.stdout.write(f'    Updated: {updated}')
                        self.stdout.write(f'    Total Items: {total_items}')
                    else:
                        logger.error(f'  ✗ VPS sync failed: {error}')
                        self.stdout.write(self.style.ERROR(f'  ✗ Failed: {error}'))
                        return
                        
                except requests.HTTPError as e:
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
            logger.info(f'Total Invoices Processed: {len(mapped_invoices)}')
            if local_only:
                logger.info(f'Created: {local_stats.get("created", 0)}')
                logger.info(f'Updated: {local_stats.get("updated", 0)}')
                logger.info(f'Total Items: {local_stats.get("total_items", 0)}')
            logger.info('=' * 70)
            logger.info('')  # Empty line for readability
            
            self.stdout.write(self.style.SUCCESS('\n' + '=' * 70))
            self.stdout.write(self.style.SUCCESS('SYNC SUMMARY'))
            self.stdout.write(self.style.SUCCESS('=' * 70))
            self.stdout.write(f'API Calls: {api_calls}')
            self.stdout.write(f'Total Invoices Processed: {len(mapped_invoices)}')
            if local_only:
                self.stdout.write(f'Created: {local_stats.get("created", 0)}')
                self.stdout.write(f'Updated: {local_stats.get("updated", 0)}')
                self.stdout.write(f'Total Items: {local_stats.get("total_items", 0)}')
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
