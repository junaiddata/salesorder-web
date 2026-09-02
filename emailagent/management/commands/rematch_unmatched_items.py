"""
One-off maintenance command: re-check RFQ items the email-tracking agent
could not confidently match to the catalog when a quotation was first
auto-drafted (e.g. before a trade-name synonym like "bend" = "elbow" was
added to the matching rules), and add them to the quotation if a match can
now be found.

Only touches quotations that look untouched by a human since the agent
created them -- ALL of the following must hold:
  - the QuotationDraft is CONFIRMED and still linked to its quotation
  - quotation.status is still 'Pending' (never approved / put on hold,
    which only happens via a review/edit flow)
  - the quotation has no QuotationLog entries at all -- the agent's own
    creation path never writes a log, so any log at all (discount
    approval/rejection, or a manual "created" entry) means a person has
    interacted with this quotation
  - quotation.remarks still starts with the agent's own auto-draft marker
    text -- editing a quotation always overwrites remarks with whatever was
    submitted, so unedited remarks still have the original wording

For each qualifying quotation, only its EnquiryItems with no matched_item
are re-checked -- already-matched items and the previously-matched customer
are never touched or re-picked. Newly-matched items are ADDED as new
QuotationItem rows (nothing existing is removed or changed), and
total_amount/grand_total are increased accordingly.

Usage:
    python manage.py rematch_unmatched_items --dry-run
    python manage.py rematch_unmatched_items
    python manage.py rematch_unmatched_items --draft-id 42
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from emailagent import supervisor
from emailagent.models import AgentRun, QuotationDraft
from emailagent.quotation_agent import (
    AUTO_DRAFT_MARKER, DEFAULT_BRAND_NOTE_PREFIX, build_match_notes, _resolve_price, _resolve_quantity, rematch_unmatched_items,
)
from so.models import Items, QuotationItem


class Command(BaseCommand):
    help = (
        "Re-check previously-unmatched RFQ items against the catalog (e.g. after new "
        "trade-name synonyms were added) and add any new matches to the still-untouched "
        "auto-created quotation."
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                             help='Show what would change without saving anything.')
        parser.add_argument('--draft-id', type=int, default=None,
                             help='Only process a single QuotationDraft, by id.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        draft_id = options.get('draft_id')

        qs = QuotationDraft.objects.filter(
            status=QuotationDraft.STATUS_CONFIRMED,
            quotation__isnull=False,
            quotation__status='Pending',
        ).select_related('quotation', 'tracked_email')
        if draft_id:
            qs = qs.filter(id=draft_id)

        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(f"{prefix}Scanning {qs.count()} confirmed agent draft(s)...")

        checked = 0
        skipped_touched = 0
        unchanged = 0
        items_fixed = 0

        for draft in qs.iterator():
            quotation = draft.quotation

            # Safety: skip anything showing any sign of human interaction.
            if quotation.logs.exists():
                skipped_touched += 1
                continue
            if not (quotation.remarks or '').startswith(AUTO_DRAFT_MARKER):
                skipped_touched += 1
                continue

            unmatched_items = list(draft.tracked_email.items.filter(matched_item__isnull=True))
            if not unmatched_items:
                continue

            checked += 1
            self.stdout.write(
                f"\nQuotation #{quotation.quotation_number} (draft {draft.id}, "
                f"email \"{draft.tracked_email.subject}\") -- {len(unmatched_items)} unmatched item(s)"
            )

            # One AgentRun per document (email + quotation) actually processed --
            # not one per whole command invocation -- so this quotation's rematch
            # stage shows up on the Agent Activity dashboard same as its
            # classification/drafting stages did, with its own timestamp and
            # status. Never recorded during --dry-run, since nothing else is saved then.
            doc_recorder = None if dry_run else supervisor.AgentRunRecorder(
                AgentRun.AGENT_REMATCH, tracked_email=draft.tracked_email, quotation=quotation,
            )

            results = rematch_unmatched_items(draft.tracked_email, unmatched_items)
            if not results:
                self.stdout.write("  no new matches")
                unchanged += 1
                if doc_recorder:
                    doc_recorder.finish(summary=f"{len(unmatched_items)} unmatched item(s) checked -- no new matches found.")
                continue

            new_quotation_items = []
            added_total = 0.0
            fixed_count = 0
            for enquiry_item, match in results:
                item_code = (match.get('item_code') or '').strip()
                if not item_code:
                    continue
                candidate = Items.objects.filter(item_code=item_code).first()
                if not candidate:
                    continue
                stock = candidate.total_available_stock
                if stock is None:
                    stock = candidate.item_stock
                zero_stock = not stock or stock <= 0
                if zero_stock and not enquiry_item.brand.strip():
                    # No stock and no specific brand requested -- skip. When
                    # a brand WAS requested, quote it anyway (fall through)
                    # so a brand-specific request never silently disappears.
                    self.stdout.write(
                        f"  SKIP {enquiry_item.description[:60]!r}: matched {item_code} "
                        "but it's out of stock"
                    )
                    continue

                unit = match.get('unit') if match.get('unit') in ('pcs', 'ctn', 'roll') else 'pcs'
                qty, qty_note = _resolve_quantity(enquiry_item.quantity, enquiry_item.unit, candidate)
                price = _resolve_price(draft.matched_customer, candidate)
                line_total = qty * price

                zero_stock_note = (
                    f"Requested brand ({enquiry_item.brand}) item {item_code} is currently 0 stock -- "
                    "quoted anyway since that brand was specifically requested; verify availability before approving."
                ) if zero_stock else ''
                default_brand_note = (
                    f"{DEFAULT_BRAND_NOTE_PREFIX} ({candidate.item_firm})."
                    if match.get('default_brand_applied') else ''
                )

                self.stdout.write(
                    f"  MATCH {enquiry_item.description[:60]!r} -> {item_code} x{qty} @ {price}"
                    + (f"  [{qty_note}]" if qty_note else "")
                    + (f"  [0 STOCK -- brand requested]" if zero_stock else "")
                )

                if not dry_run:
                    enquiry_item.matched_item = candidate
                    enquiry_item.matched_price = price
                    enquiry_item.matched_unit = unit
                    enquiry_item.matched_quantity = qty
                    enquiry_item.match_notes = build_match_notes(
                        "; ".join(filter(None, [(match.get('notes', '') or ''), qty_note])), zero_stock_note, default_brand_note,
                    )
                    enquiry_item.save(update_fields=[
                        'matched_item', 'matched_price', 'matched_unit', 'matched_quantity', 'match_notes',
                    ])

                    new_quotation_items.append(QuotationItem(
                        quotation=quotation,
                        item=candidate,
                        quantity=qty,
                        unit=unit,
                        price=price,
                        line_total=line_total,
                    ))
                added_total += line_total
                fixed_count += 1

            if fixed_count == 0:
                unchanged += 1
                if doc_recorder:
                    doc_recorder.finish(
                        summary=f"{len(results)} candidate match(es) from the agent, none usable "
                                "(out of stock or no catalog code).",
                    )
                continue

            items_fixed += fixed_count

            if dry_run:
                continue

            with transaction.atomic():
                QuotationItem.objects.bulk_create(new_quotation_items)

                quotation.total_amount = (quotation.total_amount or 0.0) + added_total
                quotation.grand_total = (quotation.grand_total or 0.0) + added_total

                # Regenerate the "could not auto-match" note from what's still
                # actually unmatched, instead of surgically editing old text.
                still_unmatched = [
                    f"{it.description}{f' -- {it.match_notes}' if it.match_notes else ''}"
                    for it in draft.tracked_email.items.filter(matched_item__isnull=True)
                ]
                base_remarks = quotation.remarks.split('\n\nCould not auto-match', 1)[0]
                if still_unmatched:
                    quotation.remarks = base_remarks + (
                        "\n\nCould not auto-match against the catalog -- add manually: "
                        + "; ".join(still_unmatched)
                    )
                else:
                    quotation.remarks = base_remarks
                quotation.save(update_fields=['total_amount', 'grand_total', 'remarks'])

            doc_recorder.finish(
                summary=f"fixed {fixed_count} item(s), added AED {added_total:,.2f} to the quotation.",
                issues=([f"{len(still_unmatched)} item(s) still unmatched after this pass."] if still_unmatched else []),
            )

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(
            f"Emails with unmatched items checked: {checked} | Items fixed: {items_fixed} | "
            f"Unchanged: {unchanged} | Skipped (human-touched): {skipped_touched}"
        )
        if dry_run:
            self.stdout.write("Dry run -- nothing was saved. Re-run without --dry-run to apply.")
