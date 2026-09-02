"""
One-off backfill: LPORequest/LPORequestItem rows created before the
extra_description/discount_percent/vat_amount/amount item columns and the
total_discount/total_excl_vat/total_vat/amount_in_words request columns
existed (see emailagent migrations 0024/0025) only have the original
description/quantity/unit/price and total_amount -- this re-reads each
LPORequest's source LPO PDF and fills in all the new columns.

Deliberately does NOT re-run the full classify_email triage (that could
also change is_lpo / matching / status on a row that may already be
reviewed or converted) -- instead calls the narrower
classifier.extract_lpo_details, which only reads the items table and
totals block.

Only touches the new columns; description/quantity/unit/price,
total_amount, and the LPORequest's own status/match/sales_order are left
exactly as they are. Skips the item-level update for a request if the
re-extracted row count doesn't match the existing one, rather than
guessing at alignment (the totals-block update still applies in that
case) -- flagged in the output for manual review.

Usage:
    python manage.py backfill_lpo_items --dry-run
    python manage.py backfill_lpo_items
    python manage.py backfill_lpo_items --pk 5
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill discount/VAT/amount/totals columns on existing LPORequest(Item) rows from their source PDF."

    def add_arguments(self, parser):
        parser.add_argument('--pk', type=int, help='Only backfill this LPORequest id.')
        parser.add_argument('--dry-run', action='store_true',
                             help='Show what would be updated without saving anything.')

    def handle(self, *args, **options):
        from emailagent import classifier
        from emailagent.lpo_agent import _parse_amount
        from emailagent.models import LPORequest, LPORequestItem

        dry_run = options['dry_run']
        prefix = '[DRY RUN] ' if dry_run else ''

        qs = (LPORequest.objects
              .filter(source_attachment__isnull=False)
              .prefetch_related('items')
              .order_by('id'))
        if options['pk']:
            qs = qs.filter(pk=options['pk'])

        updated = skipped = failed = 0

        for lpo_request in qs:
            attachment = lpo_request.source_attachment
            label = f"LPORequest {lpo_request.pk} ({lpo_request.lpo_number or 'no number'})"

            if not attachment or not attachment.file:
                self.stdout.write(self.style.WARNING(f"{label}: no source PDF on file -- skipping."))
                skipped += 1
                continue
            if attachment.content_type != 'application/pdf':
                self.stdout.write(self.style.WARNING(f"{label}: source attachment isn't a PDF -- skipping."))
                skipped += 1
                continue

            try:
                attachment.file.open('rb')
                data = attachment.file.read()
            except Exception:
                logger.exception(f"{label}: could not read source attachment file")
                self.stdout.write(self.style.ERROR(f"{label}: could not read source attachment file -- skipping."))
                failed += 1
                continue
            finally:
                attachment.file.close()

            text = classifier.extract_pdf_text(data)
            page_images = [] if text.strip() else classifier.render_pdf_pages_as_images(data)

            details = classifier.extract_lpo_details(text, page_images)
            extracted_items = details['items']
            has_totals = any(details[k] for k in ('total_discount', 'total_excl_vat', 'total_vat',
                                                    'total_incl_vat', 'amount_in_words'))
            if not extracted_items and not has_totals:
                self.stdout.write(self.style.WARNING(f"{label}: re-extraction returned nothing -- skipping."))
                skipped += 1
                continue

            touched = False

            existing = list(lpo_request.items.all())
            if extracted_items and len(extracted_items) != len(existing):
                self.stdout.write(self.style.WARNING(
                    f"{label}: re-extracted {len(extracted_items)} item(s) but {len(existing)} exist on "
                    f"record -- skipping item update to avoid mismatched rows (review manually)."
                ))
            elif extracted_items:
                self.stdout.write(f"{prefix}{label}: updating {len(existing)} item(s).")
                if not dry_run:
                    for row, item in zip(existing, extracted_items):
                        row.extra_description = item.get('extra_description', '') or ''
                        row.discount_percent = item.get('discount_percent', '') or ''
                        row.vat_amount = _parse_amount(item.get('vat_amount', ''))
                        row.amount = _parse_amount(item.get('amount', ''))
                    LPORequestItem.objects.bulk_update(
                        existing, ['extra_description', 'discount_percent', 'vat_amount', 'amount'])
                touched = True

            if has_totals:
                self.stdout.write(
                    f"{prefix}{label}: totals -- discount={details['total_discount'] or '—'} "
                    f"excl_vat={details['total_excl_vat'] or '—'} vat={details['total_vat'] or '—'} "
                    f"words={details['amount_in_words'] or '—'}"
                )
                if not dry_run:
                    lpo_request.total_discount = _parse_amount(details['total_discount'])
                    lpo_request.total_excl_vat = _parse_amount(details['total_excl_vat'])
                    lpo_request.total_vat = _parse_amount(details['total_vat'])
                    lpo_request.amount_in_words = details['amount_in_words']
                    lpo_request.save(update_fields=[
                        'total_discount', 'total_excl_vat', 'total_vat', 'amount_in_words',
                    ])
                touched = True

            if touched:
                updated += 1
            else:
                skipped += 1

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(f"LPO requests updated: {updated} | skipped: {skipped} | failed: {failed}")
        if dry_run:
            self.stdout.write("Dry run -- nothing was saved. Re-run without --dry-run to apply.")
