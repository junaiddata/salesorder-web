import os
import time
from pathlib import Path

import requests
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from so.models import Items, IgnoreList
from django.core.cache import cache

# Stock API: gzip body, ETag/304, 503 retry — see integration notes (STOCK_API_URL).
DEFAULT_STOCK_URL = "https://stock.junaidworld.com/api/stock"
ETAG_FILENAME = "stock_import_etag.txt"
MAX_RETRIES_503 = 5
RETRY_BASE_SEC = 2.0
REQUEST_TIMEOUT = (15, 600)  # connect, read (large gzip payload)


class Command(BaseCommand):
    help = "Import items from stock JSON API (gzip, ETag/304, 503 retry), excluding IgnoreList"

    def _etag_path(self) -> Path:
        return Path(settings.BASE_DIR) / ETAG_FILENAME

    def _read_etag(self):
        p = self._etag_path()
        if not p.is_file():
            return None
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _write_etag(self, etag: str) -> None:
        if not etag or not str(etag).strip():
            return
        p = self._etag_path()
        try:
            p.write_text(str(etag).strip(), encoding="utf-8")
        except OSError as exc:
            self.stdout.write(self.style.WARNING(f"Could not persist ETag ({exc}); next run may re-download."))

    def _fetch_stock_json(self, url: str):
        """
        GET /api/stock with gzip + optional If-None-Match.

        Returns:
            (list|None, status): items_data on 200; items_data None and 'not_modified' on 304.
        Raises CommandError on repeated 503 or other failures.
        """
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        etag = self._read_etag()
        if etag:
            headers["If-None-Match"] = etag

        last_error = None
        for attempt in range(MAX_RETRIES_503):
            try:
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                self.stdout.write(
                    self.style.WARNING(
                        f"Request failed (attempt {attempt + 1}/{MAX_RETRIES_503}): {exc}"
                    )
                )
                if attempt < MAX_RETRIES_503 - 1:
                    time.sleep(RETRY_BASE_SEC * (2**attempt))
                continue

            if response.status_code == 304:
                return None, "not_modified"

            if response.status_code == 503:
                self.stdout.write(
                    self.style.WARNING(
                        f"Stock API 503 (attempt {attempt + 1}/{MAX_RETRIES_503}); retrying with backoff…"
                    )
                )
                if attempt < MAX_RETRIES_503 - 1:
                    time.sleep(RETRY_BASE_SEC * (2**attempt))
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise CommandError(f"Stock API error: {exc}") from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise CommandError("Stock API returned invalid JSON") from exc

            if not isinstance(data, list):
                raise CommandError("Stock API JSON must be an array of objects")

            new_etag = response.headers.get("ETag") or response.headers.get("etag")
            if new_etag:
                self._write_etag(new_etag)

            return data, "ok"

        if last_error:
            raise CommandError(f"Stock API unreachable after {MAX_RETRIES_503} attempts: {last_error}")
        raise CommandError(
            f"Stock API returned 503 repeatedly; try again later ({MAX_RETRIES_503} attempts)."
        )

    def handle(self, *args, **kwargs):
        url = os.getenv("STOCK_API_URL", DEFAULT_STOCK_URL).strip()

        items_data, fetch_status = self._fetch_stock_json(url)

        if fetch_status == "not_modified":
            self.stdout.write(
                self.style.SUCCESS(
                    "304 Not Modified — stock unchanged since last import; skipped DB update (no lag)."
                )
            )
            return

        # 1. Load ignore list from DB
        ignore_codes = set(
            IgnoreList.objects.values_list("item_code", flat=True)
        )

        # 3. Get existing item codes from DB
        existing_codes = set(
            Items.objects.values_list("item_code", flat=True)
        )

        new_items = []
        items_to_update = []

        created, updated, skipped = 0, 0, 0

        def safe_float(value):
            """Convert to float safely; return 0 if empty, invalid, or None."""
            try:
                if value in ("", None):
                    return 0
                return float(value)
            except (TypeError, ValueError):
                return 0

        for item in items_data:
            item_code = str(item.get("item_code", "")).strip()

            if item_code in ignore_codes or not item_code:
                skipped += 1
                continue

            # Safely parse numeric fields
            cost_price = safe_float(item.get("cost_price"))
            price = safe_float(item.get("minimum_selling_price"))
            stock = int(safe_float(item.get("dip_stock")))  # IntegerField
            total_stock = Decimal(str(safe_float(item.get("total_stock"))))  # DecimalField
            dip_stock = Decimal(str(safe_float(item.get("dip_stock"))))  # DecimalField

            if item_code in existing_codes:
                obj = Items.objects.get(item_code=item_code)
                obj.item_description = item.get("description", "")
                obj.item_upvc = item.get("upc_code", "")
                obj.item_cost = cost_price
                obj.item_firm = item.get("manufacturer", "")
                obj.item_price = price
                obj.item_stock = stock
                obj.total_available_stock = total_stock
                obj.dip_warehouse_stock = dip_stock
                items_to_update.append(obj)
                updated += 1
            else:
                obj = Items(
                    item_code=item_code,
                    item_description=item.get("description", ""),
                    item_upvc=item.get("upc_code", ""),
                    item_cost=cost_price,
                    item_firm=item.get("manufacturer", ""),
                    item_price=price,
                    item_stock=stock,
                    total_available_stock=total_stock,
                    dip_warehouse_stock=dip_stock,
                )
                new_items.append(obj)
                created += 1

        # 4. Bulk update & bulk create
        if items_to_update:
            Items.objects.bulk_update(
                items_to_update,
                ["item_description", "item_upvc", "item_cost", "item_firm", "item_price", "item_stock", "total_available_stock", "dip_warehouse_stock"]
            )

        if new_items:
            Items.objects.bulk_create(new_items, ignore_conflicts=True)

        # Invalidate cached stock map used by get_stock_costs() only (avoid wiping all caches).
        cache.delete("junaid_stock_data")

        self.stdout.write(self.style.SUCCESS(
            f"Imported {created} new items, updated {updated}, skipped {skipped} (ignore list in DB)"
        ))
