# in notifications/management/commands/send_notifications.py
from django.core.management.base import BaseCommand
from tradelicense.models import Customer
from tradelicense.whatsapp_utils import send_whatsapp_template_message
from datetime import date, timedelta

class Command(BaseCommand):
    help = 'Sends trade license expiry notifications via Meta WhatsApp API'

    def handle(self, *args, **kwargs):
        today = date.today()
        thirty_days_from_now = today + timedelta(days=30)
        
        # NOTE: You need a way to map Salesman to their WhatsApp number.
        # For this example, let's assume a simple dictionary.
        # In a real app, you should store this in a User/Salesman model.
        salesman_whatsapp_numbers = {
            'A.MR.RAFIQ AD': '971542677947',
            'B.MR.JUNAID': '971542677947',
            # ... add all other salesmen and their numbers
        }

        # 1. Customers whose licenses expire in exactly 30 days
        expiring_soon_customers = Customer.objects.filter(trade_license_expiry=thirty_days_from_now)

        for customer in expiring_soon_customers:
            salesman_name = customer.sales_employee_name
            whatsapp_number = salesman_whatsapp_numbers.get(salesman_name)
            
            if whatsapp_number:
                params = [
                    salesman_name,
                    customer.bp_name,
                    customer.trade_license_expiry.strftime('%d-%b-%Y')
                ]
                success, response = send_whatsapp_template_message(
                    whatsapp_number, 'license_expiry_reminder', params
                )
                if success:
                    self.stdout.write(self.style.SUCCESS(f'Sent 30-day reminder for {customer.bp_name} to {salesman_name}'))
                else:
                    self.stdout.write(self.style.ERROR(f'Failed to send 30-day reminder for {customer.bp_name}: {response}'))
            else:
                self.stdout.write(self.style.WARNING(f'WhatsApp number not found for salesman: {salesman_name}'))

        # 2. Customers whose licenses have expired
        # To avoid spamming, you should track which notifications have been sent.
        # For now, let's just find licenses that expired yesterday to send a one-time alert.
        yesterday = today - timedelta(days=1)
        expired_customers = Customer.objects.filter(trade_license_expiry=yesterday)

        for customer in expired_customers:
            salesman_name = customer.sales_employee_name
            whatsapp_number = salesman_whatsapp_numbers.get(salesman_name)

            if whatsapp_number:
                params = [salesman_name, customer.bp_name]
                success, response = send_whatsapp_template_message(
                    whatsapp_number, 'license_expired_utility', params
                )
                if success:
                    self.stdout.write(self.style.SUCCESS(f'Sent expired alert for {customer.bp_name} to {salesman_name}'))
                else:
                    self.stdout.write(self.style.ERROR(f'Failed to send expired alert for {customer.bp_name}: {response}'))
            else:
                self.stdout.write(self.style.WARNING(f'WhatsApp number not found for salesman: {salesman_name}'))