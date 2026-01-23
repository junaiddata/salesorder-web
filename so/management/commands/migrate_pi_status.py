#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Management command to migrate existing PI status to match their SO status.

This updates all PIs so that their status matches their linked Sales Order status:
- SO status "O" or "OPEN" → PI status "OPEN"
- SO status "C" or "CLOSED" → PI status "CLOSED"

Usage:
    python manage.py migrate_pi_status
    python manage.py migrate_pi_status --dry-run  # Preview changes without applying
"""

from django.core.management.base import BaseCommand
from so.models import SAPProformaInvoice


class Command(BaseCommand):
    help = 'Sync PI status to match SO status (PI status = SO status)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without applying them'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        # Get all PIs with their salesorder
        all_pis = SAPProformaInvoice.objects.select_related('salesorder').all()
        total = all_pis.count()
        
        if total == 0:
            self.stdout.write(self.style.SUCCESS('No PIs found.'))
            return
        
        self.stdout.write(f'Found {total} PIs to check')
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\n🔍 DRY RUN MODE - No changes will be applied\n'))
        
        updated_count = 0
        skipped_count = 0
        
        for pi in all_pis:
            so = pi.salesorder
            if not so:
                skipped_count += 1
                continue
            
            # Get SO status and map to PI status
            so_status = (so.status or '').strip().upper()
            if so_status in ('O', 'OPEN'):
                new_pi_status = 'OPEN'
            elif so_status in ('C', 'CLOSED'):
                new_pi_status = 'CLOSED'
            else:
                # Default to OPEN if unclear
                new_pi_status = 'OPEN'
            
            # Check if PI status needs update
            current_pi_status = (pi.status or '').strip().upper()
            if current_pi_status != new_pi_status:
                if dry_run:
                    self.stdout.write(f'  PI {pi.pi_number}: {pi.status} → {new_pi_status} (SO status: {so.status})')
                else:
                    pi.status = new_pi_status
                    pi.save(update_fields=['status'])
                updated_count += 1
            else:
                skipped_count += 1
        
        if dry_run:
            self.stdout.write(self.style.WARNING(f'\n{updated_count} PIs would be updated'))
            self.stdout.write(f'{skipped_count} PIs already have correct status')
            self.stdout.write(self.style.WARNING('\nRun without --dry-run to apply changes'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\n✅ Updated {updated_count} PIs to match SO status'))
            self.stdout.write(f'{skipped_count} PIs already had correct status')
