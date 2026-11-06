"""Import Alabama's item master from the Alabama stock API into alabama.AlabamaItem.

Separate from Junaid's `import_items2` and never touches so.Items. Values are stored exactly as the API sends them
(cost included). Nothing is ever deleted: an item missing from a run just stops being active.

  python manage.py import_items_alabama                 # live API
  python manage.py import_items_alabama --file x.json   # a saved API response (offline / tests)
  python manage.py import_items_alabama --dry-run       # parse + report, write nothing
"""
import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from alabama import item_sync
from alabama.models import AlabamaItem

MAX_RETRIES = 5
RETRY_BASE_SEC = 2.0
REQUEST_TIMEOUT = (15, 600)
MAX_PAGES = 100
BATCH = 500


class Command(BaseCommand):
    help = "Import Alabama items from the Alabama stock API (own table, separate from Junaid's items)."

    def add_arguments(self, parser):
        parser.add_argument('--file', help='Read a saved API response (JSON) instead of calling the API.')
        parser.add_argument('--dry-run', action='store_true', help='Report what would change; write nothing.')

    # ---- fetching -------------------------------------------------------------------------------------------
    def _get(self, url, headers, params):
        last = None
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
            else:
                if r.status_code == 401:
                    raise CommandError('Alabama stock API rejected the key (401). Check ALABAMA_STOCK_API_KEY / ALABAMA_API_KEY.')
                if r.status_code != 503:
                    try:
                        r.raise_for_status()
                        return r.json()
                    except (requests.HTTPError, ValueError) as exc:
                        raise CommandError(f'Alabama stock API error: {exc}') from exc
                last = '503'
            self.stdout.write(self.style.WARNING(f'Alabama API attempt {attempt + 1}/{MAX_RETRIES} failed ({last}); retrying'))
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_SEC * (2 ** attempt))
        raise CommandError(f'Alabama stock API unreachable after {MAX_RETRIES} attempts: {last}')

    def _fetch_all(self):
        url = (settings.ALABAMA_STOCK_API_URL or '').strip()
        key = (settings.ALABAMA_STOCK_API_KEY or '').strip()
        if not url or not key:
            raise CommandError('ALABAMA_STOCK_API_URL / ALABAMA_STOCK_API_KEY are not configured.')
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip, deflate', 'X-API-Key': key}
        rows, offset = [], 0
        for _ in range(MAX_PAGES):
            data = self._get(url, headers, {'include_cost': 1, 'offset': offset})
            items = self._items_of(data)
            rows.extend(items)
            total = data.get('total', len(rows)) if isinstance(data, dict) else len(rows)
            if not items or len(rows) >= total:
                break
            offset += len(items)
        else:
            raise CommandError('Alabama stock API paging did not finish (too many pages).')
        return rows

    @staticmethod
    def _items_of(data):
        if isinstance(data, dict) and isinstance(data.get('items'), list):
            return data['items']
        if isinstance(data, list):
            return data
        raise CommandError('Alabama stock API: unexpected response shape (no "items" list).')

    # ---- import ---------------------------------------------------------------------------------------------
    def handle(self, *args, **opts):
        if opts.get('file'):
            with open(opts['file'], encoding='utf-8') as fh:
                raw_rows = self._items_of(json.load(fh))
        else:
            raw_rows = self._fetch_all()

        parsed, skipped = {}, 0
        for raw in raw_rows:
            item = item_sync.parse_feed_item(raw)
            if item is None:
                skipped += 1
                continue
            parsed[item['item_code']] = item      # a repeated code: last one wins
        if not parsed:
            raise CommandError('The feed listed no usable items; nothing imported.')

        rules = item_sync.load_brand_rules()
        existing = {o.item_code: o for o in AlabamaItem.objects.all()}
        run_at = timezone.now()

        # Sanity check (same idea as the Junaid importer): a run listing far fewer items than the previous run is
        # not stamped, so items are not wrongly shown as inactive. Values are still updated.
        prev_run = item_sync.latest_run()
        stamp = True
        if prev_run is not None:
            previous = AlabamaItem.objects.filter(source=AlabamaItem.SOURCE_FEED, last_seen_at=prev_run).count()
            if previous and len(parsed) < previous * item_sync.MIN_RUN_FRACTION:
                stamp = False
                self.stdout.write(self.style.WARNING(
                    f'Feed listed only {len(parsed)} items (previous run: {previous}); not recording this run.'))

        fields = ['item_description', 'item_upvc', 'item_firm', 'item_cost', 'item_price', 'item_stock',
                  'total_available_stock', 'quotable', 'source', 'last_seen_at']
        to_create, to_update = [], []
        quotable_total = 0
        for code, vals in parsed.items():
            quotable = item_sync.brand_is_quotable(vals['item_firm'], rules)
            quotable_total += 1 if quotable else 0
            obj = existing.get(code)
            if obj is None:
                to_create.append(AlabamaItem(**vals, quotable=quotable, source=AlabamaItem.SOURCE_FEED,
                                             last_seen_at=run_at if stamp else None))
            else:
                for k, v in vals.items():
                    setattr(obj, k, v)
                obj.quotable = quotable
                obj.source = AlabamaItem.SOURCE_FEED
                if stamp:
                    obj.last_seen_at = run_at
                to_update.append(obj)

        summary = (f'{len(parsed)} items in feed ({skipped} junk rows skipped): {len(to_create)} new, '
                   f'{len(to_update)} updated, {quotable_total} quotable (brand list).')
        if opts.get('dry_run'):
            self.stdout.write(self.style.SUCCESS('DRY RUN - nothing written. ' + summary))
            return

        with transaction.atomic():
            for i in range(0, len(to_create), BATCH):
                AlabamaItem.objects.bulk_create(to_create[i:i + BATCH], ignore_conflicts=True)
            for i in range(0, len(to_update), BATCH):
                AlabamaItem.objects.bulk_update(to_update[i:i + BATCH], fields)
        self.stdout.write(self.style.SUCCESS('Imported: ' + summary))
