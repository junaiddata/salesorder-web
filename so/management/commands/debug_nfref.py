#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Debug command to check NFRef extraction from API for a specific sales order.

Usage:
    python manage.py debug_nfref --docnum 12345
    python manage.py debug_nfref --so-number SO-12345
"""

import json
from django.core.management.base import BaseCommand
from so.api_client import SAPAPIClient
from so.models import SAPSalesorder


class Command(BaseCommand):
    help = 'Debug NFRef extraction from API'

    def add_arguments(self, parser):
        parser.add_argument(
            '--docnum',
            type=int,
            default=None,
            help='Document number from SAP API'
        )
        parser.add_argument(
            '--so-number',
            type=str,
            default=None,
            help='Sales order number (from database)'
        )

    def handle(self, *args, **options):
        docnum = options['docnum']
        so_number = options['so_number']
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write('NFRef Debug Tool')
        self.stdout.write('=' * 70)
        
        client = SAPAPIClient()
        
        # Get sales order from API
        if docnum:
            self.stdout.write(f'\nFetching sales order by DocNum: {docnum}...')
            orders = client.fetch_salesorders_by_docnum(docnum)
        elif so_number:
            # First get from database to find DocNum
            try:
                so = SAPSalesorder.objects.get(so_number=so_number)
                self.stdout.write(f'\nFound SO in database: {so_number}')
                self.stdout.write(f'  Current nf_ref: {so.nf_ref or "(empty)"}')
                self.stdout.write(f'  Related quotation: {so.related_quotation_number or "(none)"}')
                
                # Try to find DocNum (might be in internal_number or need to search)
                # For now, let's fetch from API using the SO number
                # Note: API might use DocNum, not SO number
                self.stdout.write(f'\nNote: API uses DocNum, not SO number. Please use --docnum instead.')
                return
            except SAPSalesorder.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Sales order not found: {so_number}'))
                return
        else:
            self.stdout.write(self.style.ERROR('Please provide either --docnum or --so-number'))
            return
        
        if not orders:
            self.stdout.write(self.style.ERROR('No sales orders found from API'))
            return
        
        order = orders[0]
        docnum_val = order.get('DocNum')
        
        self.stdout.write(f'\n{"=" * 70}')
        self.stdout.write('API Response Analysis')
        self.stdout.write('=' * 70)
        self.stdout.write(f'DocNum: {docnum_val}')
        self.stdout.write(f'Customer: {order.get("CardName", "N/A")}')
        
        # Check TaxExtension
        tax_extension = order.get('TaxExtension')
        self.stdout.write(f'\nTaxExtension type: {type(tax_extension)}')
        
        if tax_extension is None:
            self.stdout.write(self.style.WARNING('  ⚠ TaxExtension is None'))
        elif not isinstance(tax_extension, dict):
            self.stdout.write(self.style.WARNING(f'  ⚠ TaxExtension is not a dict: {type(tax_extension)}'))
            self.stdout.write(f'  Value: {tax_extension}')
        else:
            self.stdout.write(f'  TaxExtension keys: {list(tax_extension.keys()) if tax_extension else "empty"}')
            nf_ref_raw = tax_extension.get('NFRef')
            self.stdout.write(f'  NFRef raw value: {repr(nf_ref_raw)}')
            self.stdout.write(f'  NFRef type: {type(nf_ref_raw)}')
            
            if nf_ref_raw:
                nf_ref_str = str(nf_ref_raw).strip()
                self.stdout.write(self.style.SUCCESS(f'  ✓ NFRef found: "{nf_ref_str}"'))
                
                # Test extraction
                import re
                match = re.search(r'(?:Quotations?|Q)\s+(\d+)', nf_ref_str, re.IGNORECASE)
                if match:
                    quotation_number = match.group(1)
                    self.stdout.write(self.style.SUCCESS(f'  ✓ Extracted quotation number: {quotation_number}'))
                else:
                    self.stdout.write(self.style.WARNING(f'  ⚠ Could not extract quotation number from: "{nf_ref_str}"'))
            else:
                self.stdout.write(self.style.WARNING('  ⚠ NFRef is empty/None in TaxExtension'))
        
        # Test mapping
        self.stdout.write(f'\n{"=" * 70}')
        self.stdout.write('Mapping Test')
        self.stdout.write('=' * 70)
        try:
            mapped = client._map_api_response_to_model(order)
            mapped_nf_ref = mapped.get('nf_ref', '')
            self.stdout.write(f'Mapped nf_ref: {repr(mapped_nf_ref)}')
            
            if mapped_nf_ref:
                self.stdout.write(self.style.SUCCESS('  ✓ NFRef successfully mapped'))
            else:
                self.stdout.write(self.style.WARNING('  ⚠ NFRef is empty after mapping'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  ✗ Error during mapping: {e}'))
        
        # Show full TaxExtension for debugging
        if tax_extension:
            self.stdout.write(f'\n{"=" * 70}')
            self.stdout.write('Full TaxExtension (JSON)')
            self.stdout.write('=' * 70)
            try:
                self.stdout.write(json.dumps(tax_extension, indent=2, default=str))
            except Exception as e:
                self.stdout.write(f'Could not serialize: {e}')
                self.stdout.write(str(tax_extension))
        
        self.stdout.write('\n' + '=' * 70)
