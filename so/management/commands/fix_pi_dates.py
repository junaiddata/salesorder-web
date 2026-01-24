"""
Management command to fix PI dates.
Sets pi_date = salesorder.posting_date for all PIs.
"""
from django.core.management.base import BaseCommand
from so.models import SAPProformaInvoice


class Command(BaseCommand):
    help = "Sets pi_date to the SO posting_date for all Proforma Invoices"

    def handle(self, *args, **options):
        updated_count = 0
        
        for pi in SAPProformaInvoice.objects.select_related('salesorder').all():
            so_date = pi.salesorder.posting_date if pi.salesorder else None
            
            if so_date and pi.pi_date != so_date:
                pi.pi_date = so_date
                pi.save(update_fields=['pi_date'])
                updated_count += 1
                self.stdout.write(f"  Updated {pi.pi_number}: pi_date = {so_date}")
        
        self.stdout.write(self.style.SUCCESS(f"\nSuccessfully updated {updated_count} PIs with SO dates."))
