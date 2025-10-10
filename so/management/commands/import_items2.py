import requests
from django.core.management.base import BaseCommand
from so.models import Items, IgnoreList  # adjust app name
from django.core.cache import cache


class Command(BaseCommand):
    help = "Import items from Website A, excluding those in IgnoreList (DB based)"

    def handle(self, *args, **kwargs):
        # 1. Load ignore list from DB
        ignore_codes = set(
            IgnoreList.objects.values_list("item_code", flat=True)
        )

        # 2. Fetch items from Website A JSON API
        url = "https://stock.junaidworld.com/api/stock"  # change to your actual endpoint
        response = requests.get(url)
        items_data = response.json()

        # 3. Get existing item codes from DB
        existing_codes = set(
            Items.objects.values_list("item_code", flat=True)
        )

        new_items = []
        items_to_update = []

        created, updated, skipped = 0, 0, 0


        for item in items_data:
            item_code = str(item.get("item_code", "")).strip()

            if item_code in ignore_codes or not item_code:
                skipped += 1
                continue

            if item_code in existing_codes:
                # fetch existing object so it has a primary key
                obj = Items.objects.get(item_code=item_code)
                obj.item_description = item.get("description", "")
                obj.item_upvc = item.get("upc_code", "")
                obj.item_cost = item.get("cost_price", "")
                obj.item_firm = item.get("manufacturer", "")
                obj.item_price = item.get("minimum_selling_price", "")
                obj.item_stock = item.get("stock_quantity", 0)
                items_to_update.append(obj)
                updated += 1
            else:
                # create new object
                obj = Items(
                    item_code=item_code,
                    item_description=item.get("description", ""),
                    item_upvc=item.get("upc_code", ""),
                    item_cost=item.get("cost_price", ""),
                    item_firm=item.get("manufacturer", ""),
                    item_price=item.get("minimum_selling_price", ""),
                    item_stock=item.get("stock_quantity", 0),
                )
                new_items.append(obj)
                created += 1

        # 4. Bulk update & bulk create
        if items_to_update:
            Items.objects.bulk_update(
                items_to_update,
                ["item_description", "item_upvc", "item_cost", "item_firm", "item_price", "item_stock"]
            )

        if new_items:
            Items.objects.bulk_create(new_items,ignore_conflicts=True)

        cache.clear()
        self.stdout.write(self.style.SUCCESS(
            f"Imported {created} new items, updated {updated}, skipped {skipped} (ignore list in DB)"
        ))
