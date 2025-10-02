import requests
from django.core.management.base import BaseCommand
from so.models import Items  # replace yourapp with your app name

class Command(BaseCommand):
    help = "Fetch stock data from Flask API and update Items stock quantities"

    def handle(self, *args, **kwargs):
        url = "https://junaiddataanalyst.pythonanywhere.com/api/stock"  # Change this to your Flask API URL

        try:
            response = requests.get(url)
            response.raise_for_status()
            stock_data = response.json()
        except Exception as e:
            self.stderr.write(f"Error fetching stock data: {e}")
            return

        updated = 0
        for stock_item in stock_data:
            item_code = stock_item.get("item_code")
            stock_quantity = stock_item.get("stock_quantity", 0)

            if not item_code:
                continue

            try:
                item = Items.objects.get(item_code=item_code)
                item.item_stock = int(stock_quantity)
                item.save(update_fields=['item_stock'])
                updated += 1
            except Items.DoesNotExist:
                self.stdout.write(f"Item with code {item_code} not found in DB.")

        self.stdout.write(f"Updated stock quantity for {updated} items.")
