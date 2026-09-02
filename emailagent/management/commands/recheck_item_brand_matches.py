"""
One-off maintenance command: re-check EVERY line item (not just unmatched
ones) on already-auto-created quotations against the current brand-handling
rules (brand aliases like Cosmo=COSMOPLAST, honoring a stated brand
preference, falling back to the cheapest in-stock option under an "or
cheapest"-style instruction), and REPLACE a line if a better match is found.

This is a broader, more invasive sibling of rematch_unmatched_items -- that
command only ever ADDS a line for something that had no match at all; this
one can also swap out an item that already has a match, e.g. an "or
cheapest" substitute that was picked before the Cosmo=COSMOPLAST alias
existed, now that the real Cosmoplast item can be found.

Same untouched-quotation safety gates as rematch_unmatched_items (status
still Pending, no QuotationLog entries, remarks still start with the
agent's auto-draft marker). A line is only ever replaced when it can be
unambiguously located on the quotation (exactly one QuotationItem currently
pointing at the EnquiryItem's current matched catalog item) -- if that
can't be confirmed, the item is left alone and reported as skipped rather
than risking a wrong or duplicate edit.

Usage:
    python manage.py recheck_item_brand_matches --dry-run
    python manage.py recheck_item_brand_matches
    python manage.py recheck_item_brand_matches --draft-id 42
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from emailagent import supervisor
from emailagent.models import AgentRun, QuotationDraft
from emailagent.quotation_agent import (
    AUTO_DRAFT_MARKER, DEFAULT_BRAND_NOTE_PREFIX, build_match_notes, _resolve_price, _resolve_quantity, recheck_item_brand_matches,
)
from so.models import Items, QuotationItem


class Command(BaseCommand):
    help = (
        "Re-check every item (matched or not) on already-auto-created quotations against "
        "the current brand-handling rules, replacing a line if a better brand/price match "
        "is found."
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
        items_changed = 0
        items_ambiguous = 0

        for draft in qs.iterator():
            quotation = draft.quotation

            # Safety: skip anything showing any sign of human interaction.
            if quotation.logs.exists():
                skipped_touched += 1
                continue
            if not (quotation.remarks or '').startswith(AUTO_DRAFT_MARKER):
                skipped_touched += 1
                continue

            all_items = list(draft.tracked_email.items.select_related('matched_item').all())
            if not all_items:
                continue

            checked += 1
            self.stdout.write(
                f"\nQuotation #{quotation.quotation_number} (draft {draft.id}, "
                f"email \"{draft.tracked_email.subject}\") -- {len(all_items)} item(s)"
            )

            # One AgentRun per document (email + quotation) actually processed,
            # same as rematch_unmatched_items -- so this stage shows up on the
            # Agent Activity dashboard with its own timestamp/status. Never
            # recorded during --dry-run, since nothing else is saved then.
            doc_recorder = None if dry_run else supervisor.AgentRunRecorder(
                AgentRun.AGENT_RECHECK, tracked_email=draft.tracked_email, quotation=quotation,
            )
            doc_ambiguous = 0
            doc_changed = 0

            results = recheck_item_brand_matches(draft.tracked_email, all_items)
            if not results:
                self.stdout.write("  no response from agent -- skipping")
                unchanged += 1
                if doc_recorder:
                    doc_recorder.finish(summary=f"{len(all_items)} item(s) checked -- no response from the agent.")
                continue

            quotation_total_delta = 0.0
            new_quotation_items = []
            any_change = False

            for enquiry_item, match in results:
                suggested_code = (match.get('item_code') or '').strip()
                current_code = enquiry_item.matched_item.item_code if enquiry_item.matched_item_id else ''

                if suggested_code == current_code:
                    continue  # agent kept the existing (or still-empty) match -- nothing to do
                if not suggested_code:
                    # Never let a rule-based recheck downgrade an existing match to
                    # unmatched -- that's a strict regression, not an improvement.
                    continue

                candidate = Items.objects.filter(item_code=suggested_code).first()
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
                        f"  SKIP {enquiry_item.description[:60]!r}: suggested {suggested_code} "
                        "but it's out of stock"
                    )
                    continue

                unit = match.get('unit') if match.get('unit') in ('pcs', 'ctn', 'roll') else 'pcs'
                qty, qty_note = _resolve_quantity(enquiry_item.quantity, enquiry_item.unit, candidate)
                price = _resolve_price(draft.matched_customer, candidate)
                new_line_total = qty * price

                if not enquiry_item.matched_item_id:
                    # Was unmatched -- this is a pure addition, same as rematch_unmatched_items.
                    self.stdout.write(
                        f"  ADD {enquiry_item.description[:60]!r} -> {suggested_code} "
                        f"({candidate.item_firm}) x{qty} @ {price}"
                    )
                    if not dry_run:
                        new_quotation_items.append(QuotationItem(
                            quotation=quotation, item=candidate, quantity=qty,
                            unit=unit, price=price, line_total=new_line_total,
                        ))
                    quotation_total_delta += new_line_total
                    any_change = True
                else:
                    # Was matched to something else -- only replace if we can find
                    # exactly one existing line for it, to avoid touching the wrong row.
                    existing_qis = list(QuotationItem.objects.filter(
                        quotation=quotation, item_id=enquiry_item.matched_item_id,
                    ))
                    if len(existing_qis) != 1:
                        self.stdout.write(
                            f"  SKIP {enquiry_item.description[:60]!r}: agent suggests "
                            f"{suggested_code} over {current_code}, but couldn't unambiguously "
                            f"locate the current line on the quotation ({len(existing_qis)} matches) "
                            "-- leaving as-is"
                        )
                        items_ambiguous += 1
                        doc_ambiguous += 1
                        continue

                    old_qi = existing_qis[0]
                    self.stdout.write(
                        f"  REPLACE {enquiry_item.description[:60]!r}: "
                        f"{current_code} ({old_qi.item.item_firm}, {old_qi.price}) -> "
                        f"{suggested_code} ({candidate.item_firm}, {price})"
                    )
                    if not dry_run:
                        old_qi.item = candidate
                        old_qi.unit = unit
                        old_qi.quantity = qty
                        old_qi.price = price
                        old_qi.line_total = new_line_total
                        old_qi.save(update_fields=['item', 'unit', 'quantity', 'price', 'line_total'])
                    quotation_total_delta += (new_line_total - old_qi.line_total)
                    any_change = True

                if not dry_run:
                    zero_stock_note = (
                        f"Requested brand ({enquiry_item.brand}) item {suggested_code} is currently 0 stock -- "
                        "quoted anyway since that brand was specifically requested; verify availability before approving."
                    ) if zero_stock else ''
                    default_brand_note = (
                        f"{DEFAULT_BRAND_NOTE_PREFIX} ({candidate.item_firm})."
                        if match.get('default_brand_applied') else ''
                    )
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
                items_changed += 1
                doc_changed += 1

            if not any_change:
                self.stdout.write("  no changes")
                unchanged += 1
                if doc_recorder:
                    doc_recorder.finish(
                        summary=f"{len(all_items)} item(s) checked -- no changes.",
                        issues=([f"{doc_ambiguous} suggested replacement(s) were ambiguous -- left as-is."]
                                if doc_ambiguous else []),
                    )
                continue

            if dry_run:
                continue

            with transaction.atomic():
                QuotationItem.objects.bulk_create(new_quotation_items)

                quotation.total_amount = (quotation.total_amount or 0.0) + quotation_total_delta
                quotation.grand_total = (quotation.grand_total or 0.0) + quotation_total_delta

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

            doc_issues = []
            if doc_ambiguous:
                doc_issues.append(f"{doc_ambiguous} suggested replacement(s) were ambiguous -- left as-is.")
            if still_unmatched:
                doc_issues.append(f"{len(still_unmatched)} item(s) still unmatched after this pass.")
            doc_recorder.finish(
                summary=f"changed {doc_changed} item(s), net AED {quotation_total_delta:,.2f} change to the quotation.",
                issues=doc_issues,
            )

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(
            f"Quotations checked: {checked} | Items changed: {items_changed} | "
            f"Ambiguous (left as-is): {items_ambiguous} | Unchanged: {unchanged} | "
            f"Skipped (human-touched): {skipped_touched}"
        )
        if dry_run:
            self.stdout.write("Dry run -- nothing was saved. Re-run without --dry-run to apply.")
