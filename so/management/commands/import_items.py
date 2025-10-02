import requests
import pandas as pd
from django.core.management.base import BaseCommand
from so.models import Items  # adjust to your app name


class Command(BaseCommand):
    help = "Import items from Website A, excluding those in ignore list"

    def handle(self, *args, **kwargs):
        # 1. Load ignore list from Excel
        ignore_file = "ignore_list.xlsx"
        df_ignore = pd.read_excel(ignore_file)
        
        # Excel contains column "item_code"
        ignore_codes = df_ignore["item_code"].astype(str).str.strip().tolist()

        # 2. Fetch items from Website A JSON API
        url = "https://junaiddataanalyst.pythonanywhere.com/api/stock"  # change to your actual endpoint
        response = requests.get(url)
        items_data = response.json()

        created, skipped, updated = 0, 0, 0

        for item in items_data:
            item_code = str(item.get("item_code", "")).strip()

            # Skip if in ignore list
            if item_code in ignore_codes:
                skipped += 1
                continue  

            # 3. Create or update item in Django
            obj, created_flag = Items.objects.update_or_create(
                item_code=item_code,
                defaults={
                    "item_description": item.get("description", ""),
                    "item_upvc": item.get("upc_code",""),  # your JSON doesn’t have this field
                    "item_cost": item.get("cost_price",""),  # not provided, default 0
                    "item_firm": item.get("manufacturer", ""),
                    "item_price": item.get("minimum_selling_price"),  
                    "item_stock": item.get("stock_quantity", 0),

                }
            )

            if created_flag:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Imported {created} new items, updated {updated}, skipped {skipped} (ignored list)"
        ))
