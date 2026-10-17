"""
Fix brands for items used in Detailed Purchase Analysis POs.

Why: the brand shown on that page comes from Items.item_firm. Items that appear on POs
but are in the IgnoreList (so import_items2 skips them) or have a blank firm have no brand.
This command looks ONLY at item codes that appear on Detailed Purchase Analysis PO lines
and have no brand, fetches the stock API, and for those codes:
  - creates/updates the Items row (same field mapping as import_items2)
  - removes the code from the IgnoreList (same thing the sales sync does when an
    ignored item turns up on a document)

Usage:
    python manage.py fix_po_item_brands --dry-run     # report only
    python manage.py fix_po_item_brands
"""
import os
import time
from decimal import Decimal

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from so.models import DetailedPurchaseOrderItem, IgnoreList, Items

DEFAULT_STOCK_URL = "https://stock.junaidworld.com/api/stock"


def _f(value):
    try:
        return 0 if value in ("", None) else float(value)
    except (TypeError, ValueError):
        return 0


class Command(BaseCommand):
    help = "Add/refresh Items (with brand) for PO items that have no brand, un-ignoring them"

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help="Report only; change nothing")

    def _fetch(self, url):
        last = None
        for attempt in range(5):
            try:
                r = requests.get(url, headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
                                 timeout=(15, 600))
                if r.status_code == 503:
                    time.sleep(2 * (2 ** attempt))
                    continue
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list):
                    raise CommandError("Stock API JSON must be an array")
                return data
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 * (2 ** attempt))
        raise CommandError(f"Stock API unreachable: {last}")

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        url = os.getenv("STOCK_API_URL", DEFAULT_STOCK_URL).strip()

        po_codes = set(
            DetailedPurchaseOrderItem.objects.exclude(item_no__isnull=True).exclude(item_no='')
            .values_list('item_no', flat=True).distinct()
        )
        branded = set(
            Items.objects.filter(item_code__in=po_codes).exclude(item_firm__isnull=True).exclude(item_firm='')
            .values_list('item_code', flat=True)
        )
        todo = po_codes - branded
        self.stdout.write(f"PO item codes: {len(po_codes)} | already branded: {len(branded)} | need brand: {len(todo)}")
        if not todo:
            return

        by_code = {str(i.get('item_code', '')).strip(): i for i in self._fetch(url)}
        fixable = {c for c in todo if (by_code.get(c, {}).get('manufacturer') or '').strip()}
        missing_api = todo - fixable
        self.stdout.write(f"Found a manufacturer in stock API for {len(fixable)}; no brand available for {len(missing_api)}")
        if missing_api:
            self.stdout.write("  No brand in API: " + ", ".join(sorted(missing_api)[:30]) + (" ..." if len(missing_api) > 30 else ""))
        if dry:
            ignored = IgnoreList.objects.filter(item_code__in=fixable).count()
            self.stdout.write(self.style.WARNING(f"Dry run: would fix {len(fixable)} items ({ignored} currently in IgnoreList)."))
            return

        desc_len = Items._meta.get_field('item_description').max_length or 255
        created = updated = 0
        for code in sorted(fixable):
            it = by_code[code]
            _, was_created = Items.objects.update_or_create(
                item_code=code,
                defaults={
                    'item_description': (it.get('description') or '')[:desc_len],
                    'item_upvc': it.get('upc_code', '') or '',
                    'item_cost': _f(it.get('cost_price')),
                    'item_firm': (it.get('manufacturer') or '').strip(),
                    'item_price': _f(it.get('minimum_selling_price')),
                    'item_stock': int(_f(it.get('dip_stock'))),
                    'total_available_stock': Decimal(str(_f(it.get('total_stock')))),
                    'dip_warehouse_stock': Decimal(str(_f(it.get('dip_stock')))),
                },
            )
            created += was_created
            updated += (not was_created)
        removed = IgnoreList.objects.filter(item_code__in=fixable).delete()[0]
        self.stdout.write(self.style.SUCCESS(
            f"Done. Items created: {created}, updated: {updated}, removed from IgnoreList: {removed}"))
