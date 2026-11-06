"""Link Alabama's existing Excel-upload history (sales / purchase / delivery-order lines) to Alabama's own item master.

Only fills the NEW `alabama_item` column, matching on item code. The legacy `item` link (so.Items), quantities, amounts
and every other column are never touched, so this is safe to re-run and fully reversible (--rollback).

  python manage.py backfill_alabama_items --dry-run   # report only (default is to write)
  python manage.py backfill_alabama_items             # link the lines
  python manage.py backfill_alabama_items --rollback  # clear alabama_item again
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from alabama.models import AlabamaDeliveryOrderItem, AlabamaItem, AlabamaPurchaseLine, AlabamaSalesLine

MODELS = [
    ('sales lines', AlabamaSalesLine),
    ('purchase lines', AlabamaPurchaseLine),
    ('delivery-order lines', AlabamaDeliveryOrderItem),
]


class Command(BaseCommand):
    help = 'Link existing Alabama history lines to AlabamaItem by item code (adds alabama_item only).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--rollback', action='store_true', help='Clear alabama_item on every line.')

    def handle(self, *args, **opts):
        if opts['rollback']:
            with transaction.atomic():
                for label, model in MODELS:
                    n = model.objects.exclude(alabama_item__isnull=True).update(alabama_item=None)
                    self.stdout.write(f'{label}: cleared {n}')
            return

        by_code = {code: pk for pk, code in AlabamaItem.objects.values_list('pk', 'item_code')}
        total_unmatched = set()
        with transaction.atomic():
            for label, model in MODELS:
                pairs = list(model.objects.filter(alabama_item__isnull=True)
                             .values_list('item_id', 'item__item_code').distinct())
                linked = unmatched_lines = 0
                for item_id, code in pairs:
                    target = by_code.get((code or '').strip())
                    if target is None:
                        total_unmatched.add(code)
                        unmatched_lines += model.objects.filter(item_id=item_id, alabama_item__isnull=True).count()
                        continue
                    if opts['dry_run']:
                        linked += model.objects.filter(item_id=item_id, alabama_item__isnull=True).count()
                    else:
                        linked += model.objects.filter(item_id=item_id, alabama_item__isnull=True).update(alabama_item_id=target)
                self.stdout.write(f'{label}: {"would link" if opts["dry_run"] else "linked"} {linked}, '
                                  f'no Alabama item for {unmatched_lines} line(s)')
            if opts['dry_run']:
                transaction.set_rollback(True)
        if total_unmatched:
            self.stdout.write(self.style.WARNING(
                f'{len(total_unmatched)} code(s) not in the Alabama item master (those lines keep the legacy item): '
                f'{sorted(c for c in total_unmatched if c)[:20]}'))
        self.stdout.write(self.style.SUCCESS('Dry run complete.' if opts['dry_run'] else 'Backfill complete.'))
