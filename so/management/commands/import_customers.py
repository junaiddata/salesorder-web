import openpyxl
from django.core.management.base import BaseCommand
from so.models import Customer, Salesman  # adjust path if needed

class Command(BaseCommand):
    help = "Import customers from Excel and skip existing customer codes"

    def handle(self, *args, **kwargs):
        filepath = 'data/customers.xlsx'  # adjust path

        wb = openpyxl.load_workbook(filepath)
        sheet = wb.active

        created_count = 0
        skipped_count = 0

        for row in sheet.iter_rows(min_row=2, values_only=True):  # Skip header
            customer_code, customer_name, salesman_name = row

            if Customer.objects.filter(customer_code=customer_code).exists():
                skipped_count += 1
                continue

            salesman = Salesman.objects.filter(salesman_name=salesman_name).first()
            if not salesman:
                self.stdout.write(self.style.WARNING(f"Salesman '{salesman_name}' not found. Skipping customer '{customer_name}'."))
                continue

            Customer.objects.create(
                customer_code=customer_code,
                customer_name=customer_name,
                salesman=salesman
            )
            created_count += 1

        self.stdout.write(self.style.SUCCESS(f"✅ Imported {created_count} customers. Skipped {skipped_count} already existing."))
