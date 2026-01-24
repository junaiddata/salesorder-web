"""
Management command to update existing sales order items with stock from Items model.
This updates total_available_stock and dip_warehouse_stock for all existing SAPSalesorderItem records.
Also removes items from IgnoreList if they're used in sales orders but not in Items model.
"""
from django.core.management.base import BaseCommand
from so.models import SAPSalesorderItem, Items, IgnoreList
from decimal import Decimal


class Command(BaseCommand):
    help = "Updates existing sales order items with stock from Items model"

    def handle(self, *args, **options):
        updated_count = 0
        not_found_codes = set()
        
        # Get all items that have item_no
        items_to_update = SAPSalesorderItem.objects.filter(item_no__isnull=False).exclude(item_no='')
        
        self.stdout.write(f"Found {items_to_update.count()} sales order items to check...")
        
        # Get all unique item codes
        item_codes = list(items_to_update.values_list('item_no', flat=True).distinct())
        
        # Batch load all Items
        items_dict = {}
        for item in Items.objects.filter(item_code__in=item_codes).only('item_code', 'total_available_stock', 'dip_warehouse_stock'):
            items_dict[item.item_code] = {
                'total_available_stock': item.total_available_stock or Decimal('0'),
                'dip_warehouse_stock': item.dip_warehouse_stock or Decimal('0'),
            }
        
        self.stdout.write(f"Loaded stock for {len(items_dict)} items from Items model...")
        
        # Update in batches
        batch_size = 1000
        batch = []
        
        for so_item in items_to_update.select_related('salesorder'):
            item_code = so_item.item_no
            
            if item_code in items_dict:
                stock_data = items_dict[item_code]
                so_item.total_available_stock = stock_data['total_available_stock']
                so_item.dip_warehouse_stock = stock_data['dip_warehouse_stock']
                batch.append(so_item)
                updated_count += 1
                
                if len(batch) >= batch_size:
                    SAPSalesorderItem.objects.bulk_update(
                        batch,
                        ['total_available_stock', 'dip_warehouse_stock'],
                        batch_size=batch_size
                    )
                    batch = []
                    self.stdout.write(f"  Updated {updated_count} items so far...")
            else:
                not_found_codes.add(item_code)
        
        # Update remaining batch
        if batch:
            SAPSalesorderItem.objects.bulk_update(
                batch,
                ['total_available_stock', 'dip_warehouse_stock'],
                batch_size=batch_size
            )
        
        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Successfully updated {updated_count} sales order items with stock data"
        ))
        
        # Remove not found items from IgnoreList so they can be imported
        if not_found_codes:
            self.stdout.write(f"\n⚠ {len(not_found_codes)} unique items not found in Items model")
            self.stdout.write("Removing these items from IgnoreList so they can be imported...")
            
            deleted_count, _ = IgnoreList.objects.filter(item_code__in=not_found_codes).delete()
            
            if deleted_count > 0:
                self.stdout.write(self.style.SUCCESS(
                    f"✓ Removed {deleted_count} items from IgnoreList"
                ))
                self.stdout.write(self.style.WARNING(
                    "→ Run 'python manage.py import_items2' to import these items"
                ))
                self.stdout.write(self.style.WARNING(
                    "→ Then run 'python manage.py update_so_items_stock' again to update stock"
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    f"⚠ {len(not_found_codes)} items not in Items model and not in IgnoreList (may not exist in stock API)"
                ))
