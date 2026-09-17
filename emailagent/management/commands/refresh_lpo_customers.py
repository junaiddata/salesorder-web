"""
Re-reads the buyer's name/TRN on existing LPORequests from their stored LPO
document via lpo_agent.refine_lpo_customer (page 1 sent as an image, so a
name that exists only in the letterhead logo is picked up) -- for LPOs
processed before that step existed.

Only touches NEEDS_REVIEW/FAILED requests with no Sales Order and a stored
source attachment. Updates customer_name_stated/customer_trn_stated/
customer_name_source only; status/matching are left alone unless --recheck is
passed, which then runs match_and_maybe_convert exactly like the review
page's "Re-check" button (and can therefore auto-create a Sales Order).

Usage:
    python manage.py refresh_lpo_customers --dry-run
    python manage.py refresh_lpo_customers --pk 29
    python manage.py refresh_lpo_customers --pk 29 --recheck
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-extract the customer name/TRN on needs-review LPOs from their stored LPO document."

    def add_arguments(self, parser):
        parser.add_argument('--pk', type=int, help='Only refresh this LPORequest id.')
        parser.add_argument('--dry-run', action='store_true',
                             help='Show what would change without saving anything.')
        parser.add_argument('--recheck', action='store_true',
                             help='After updating, re-run matching (may auto-create a Sales Order).')

    def handle(self, *args, **options):
        from emailagent.lpo_agent import match_and_maybe_convert, refine_lpo_customer
        from emailagent.models import LPORequest

        dry_run = options['dry_run']
        prefix = '[DRY RUN] ' if dry_run else ''

        qs = (LPORequest.objects
              .filter(status__in=[LPORequest.STATUS_NEEDS_REVIEW, LPORequest.STATUS_FAILED],
                      sales_order__isnull=True, source_attachment__isnull=False)
              .select_related('tracked_email', 'source_attachment')
              .order_by('id'))
        if options['pk']:
            qs = qs.filter(pk=options['pk'])

        updated = unchanged = rechecked = 0
        for lpo_request in qs:
            label = f"LPORequest {lpo_request.pk} ({lpo_request.lpo_number or 'no number'})"
            before = (lpo_request.customer_name_stated, lpo_request.customer_trn_stated)

            if not refine_lpo_customer(lpo_request):
                self.stdout.write(f"{label}: nothing extracted -- kept '{before[0] or '(blank)'}'.")
                unchanged += 1
                continue

            after = (lpo_request.customer_name_stated, lpo_request.customer_trn_stated)
            self.stdout.write(
                f"{prefix}{label}: name '{before[0] or '(blank)'}' -> '{after[0] or '(blank)'}' "
                f"[{lpo_request.customer_name_source or '-'}], TRN '{before[1] or '(blank)'}' -> '{after[1] or '(blank)'}'"
            )
            if dry_run:
                continue

            lpo_request.save(update_fields=['customer_name_stated', 'customer_trn_stated', 'customer_name_source'])
            updated += 1

            if options['recheck']:
                try:
                    lpo_request.error = ''
                    match_and_maybe_convert(lpo_request)
                    rechecked += 1
                    self.stdout.write(f"  re-checked: status={lpo_request.status}")
                except Exception as exc:
                    logger.exception(f"{label}: recheck failed")
                    self.stdout.write(self.style.ERROR(f"  re-check failed: {exc}"))

        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Done. updated={updated} unchanged={unchanged} rechecked={rechecked}"
        ))
