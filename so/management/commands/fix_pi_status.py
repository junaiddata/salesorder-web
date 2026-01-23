#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Management command to set ALL PI status to ACTIVE.

Usage:
    python manage.py fix_pi_status
"""

from django.core.management.base import BaseCommand
from so.models import SAPProformaInvoice


class Command(BaseCommand):
    help = 'Set all PI status to ACTIVE'

    def handle(self, *args, **options):
        # Update ALL PIs to ACTIVE
        updated = SAPProformaInvoice.objects.exclude(status='ACTIVE').update(status='ACTIVE')
        
        total = SAPProformaInvoice.objects.count()
        self.stdout.write(self.style.SUCCESS(f'✅ Updated {updated} PIs to status ACTIVE (Total PIs: {total})'))
