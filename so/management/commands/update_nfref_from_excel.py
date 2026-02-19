#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
One-time management command to update NFRef field for existing sales orders from Excel file.

Usage:
    python manage.py update_nfref_from_excel --file path/to/file.xlsx
    python manage.py update_nfref_from_excel --file path/to/file.xlsx --sheet-name Sheet1
    python manage.py update_nfref_from_excel --file path/to/file.xlsx --dry-run  # Preview changes without saving

Excel file format:
    - Column 1: SO number (e.g., "SO-12345" or "12345")
    - Column 2: NFRef (e.g., "Based On Sales Quotations 126001023.")
    
    OR with headers:
    - Row 1: Headers (SO number, NFRef)
    - Row 2+: Data rows
"""

import sys
import os
import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction
from so.models import SAPSalesorder
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Update NFRef field for sales orders from Excel file'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            required=True,
            help='Path to Excel file (must have SO number and NFRef columns)'
        )
        parser.add_argument(
            '--sheet-name',
            type=str,
            default=0,
            help='Sheet name or index (default: first sheet)'
        )
        parser.add_argument(
            '--so-column',
            type=str,
            default=None,
            help='Column name for SO number (auto-detect if not specified)'
        )
        parser.add_argument(
            '--nfref-column',
            type=str,
            default=None,
            help='Column name for NFRef (auto-detect if not specified)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without saving to database'
        )
        parser.add_argument(
            '--skip-header',
            action='store_true',
            help='Skip first row (if file has headers)'
        )

    def handle(self, *args, **options):
        file_path = options['file']
        sheet_name = options['sheet_name']
        dry_run = options['dry_run']
        so_column = options['so_column']
        nfref_column = options['nfref_column']
        skip_header = options['skip_header']
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('Update NFRef from Excel'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(f'File: {file_path}')
        self.stdout.write(f'Mode: {"DRY RUN (Preview Only)" if dry_run else "UPDATE DATABASE"}')
        self.stdout.write('-' * 70)
        
        # Check if file exists
        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f'Error: File not found: {file_path}'))
            return
        
        try:
            # Read Excel file
            self.stdout.write('\n[STEP 1] Reading Excel file...')
            try:
                df = pd.read_excel(file_path, sheet_name=sheet_name, header=0 if not skip_header else None)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error reading Excel file: {e}'))
                return
            
            self.stdout.write(f'  ✓ Loaded {len(df)} rows from Excel')
            self.stdout.write(f'  Columns: {", ".join(df.columns.tolist())}')
            
            # Auto-detect columns if not specified
            if not so_column:
                # Try common column names
                possible_so_cols = ['so_number', 'so number', 'so_number', 'so', 'document number', 'docnum', 'doc_num']
                for col in possible_so_cols:
                    if col.lower() in [c.lower() for c in df.columns]:
                        so_column = col
                        break
                
                # If still not found, use first column
                if not so_column:
                    so_column = df.columns[0]
                    self.stdout.write(f'  Using first column for SO number: {so_column}')
            
            if not nfref_column:
                # Try common column names
                possible_nfref_cols = ['nfref', 'nf_ref', 'nf ref', 'quotation reference', 'quotation_ref']
                for col in possible_nfref_cols:
                    if col.lower() in [c.lower() for c in df.columns]:
                        nfref_column = col
                        break
                
                # If still not found, use second column
                if not nfref_column:
                    nfref_column = df.columns[1] if len(df.columns) > 1 else None
                    if nfref_column:
                        self.stdout.write(f'  Using second column for NFRef: {nfref_column}')
            
            if not nfref_column:
                self.stdout.write(self.style.ERROR('Error: Could not find NFRef column. Please specify with --nfref-column'))
                return
            
            # Validate columns exist
            if so_column not in df.columns:
                self.stdout.write(self.style.ERROR(f'Error: Column "{so_column}" not found in Excel file'))
                return
            
            if nfref_column not in df.columns:
                self.stdout.write(self.style.ERROR(f'Error: Column "{nfref_column}" not found in Excel file'))
                return
            
            # Extract data
            self.stdout.write('\n[STEP 2] Processing data...')
            updates = []
            not_found = []
            skipped = []
            
            for idx, row in df.iterrows():
                so_number = str(row[so_column]).strip() if pd.notna(row[so_column]) else None
                nf_ref = str(row[nfref_column]).strip() if pd.notna(row[nfref_column]) else None
                
                if not so_number or so_number.lower() in ['nan', 'none', '']:
                    skipped.append(idx + 1)
                    continue
                
                if not nf_ref or nf_ref.lower() in ['nan', 'none', '']:
                    skipped.append(idx + 1)
                    continue
                
                # Try to find sales order
                try:
                    so = SAPSalesorder.objects.get(so_number=so_number)
                    updates.append({
                        'so': so,
                        'so_number': so_number,
                        'nf_ref': nf_ref,
                        'current_nf_ref': so.nf_ref or '(empty)'
                    })
                except SAPSalesorder.DoesNotExist:
                    not_found.append(so_number)
                except SAPSalesorder.MultipleObjectsReturned:
                    self.stdout.write(self.style.WARNING(f'  Warning: Multiple SOs found for {so_number}'))
                    not_found.append(so_number)
            
            self.stdout.write(f'  ✓ Found {len(updates)} sales orders to update')
            if not_found:
                self.stdout.write(self.style.WARNING(f'  ⚠ {len(not_found)} SO numbers not found in database'))
            if skipped:
                self.stdout.write(f'  ⚠ {len(skipped)} rows skipped (empty SO number or NFRef)')
            
            # Show preview
            if updates:
                self.stdout.write('\n[STEP 3] Preview of changes:')
                self.stdout.write('-' * 70)
                preview_count = min(10, len(updates))
                for i, update in enumerate(updates[:preview_count]):
                    self.stdout.write(f'  {i+1}. SO: {update["so_number"]}')
                    self.stdout.write(f'     Current: {update["current_nf_ref"]}')
                    self.stdout.write(f'     New:     {update["nf_ref"]}')
                    self.stdout.write('')
                
                if len(updates) > preview_count:
                    self.stdout.write(f'  ... and {len(updates) - preview_count} more')
            
            # Apply updates
            if not dry_run and updates:
                self.stdout.write('\n[STEP 4] Updating database...')
                updated_count = 0
                error_count = 0
                
                try:
                    with transaction.atomic():
                        for update in updates:
                            try:
                                update['so'].nf_ref = update['nf_ref']
                                update['so'].save(update_fields=['nf_ref'])
                                updated_count += 1
                            except Exception as e:
                                error_count += 1
                                logger.error(f'Error updating SO {update["so_number"]}: {e}')
                                self.stdout.write(self.style.ERROR(f'  ✗ Error updating {update["so_number"]}: {e}'))
                    
                    self.stdout.write(self.style.SUCCESS(f'\n✓ Successfully updated {updated_count} sales orders'))
                    if error_count > 0:
                        self.stdout.write(self.style.WARNING(f'  ⚠ {error_count} errors occurred'))
                
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'\n✗ Error during update: {e}'))
                    logger.exception('Error during bulk update')
                    return
            
            elif dry_run:
                self.stdout.write('\n[STEP 4] DRY RUN - No changes made')
                self.stdout.write(f'  Would update {len(updates)} sales orders')
            
            # Summary
            self.stdout.write('\n' + '=' * 70)
            self.stdout.write('SUMMARY')
            self.stdout.write('=' * 70)
            self.stdout.write(f'Total rows in Excel: {len(df)}')
            self.stdout.write(f'Sales orders found: {len(updates)}')
            self.stdout.write(f'SO numbers not found: {len(not_found)}')
            self.stdout.write(f'Rows skipped: {len(skipped)}')
            if not dry_run:
                self.stdout.write(f'Updated: {len(updates) - error_count if updates else 0}')
            self.stdout.write('=' * 70)
            
            # Show not found SOs if any
            if not_found and len(not_found) <= 20:
                self.stdout.write('\nSO numbers not found in database:')
                for so_num in not_found[:20]:
                    self.stdout.write(f'  - {so_num}')
                if len(not_found) > 20:
                    self.stdout.write(f'  ... and {len(not_found) - 20} more')
        
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\n✗ Error: {e}'))
            logger.exception('Error in update_nfref_from_excel command')
            raise
