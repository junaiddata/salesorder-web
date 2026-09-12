"""
Development helper: builds a self-contained demo enquiry + quotation so the
"closest available alternatives" feature can be clicked through by hand,
without waiting for a real RFQ to happen to produce a near-miss.

It builds TWO quotations, because the feature looks quite different either
side of a full quotation:

  A. "Mixed" -- the everyday shape. Most lines matched the catalog exactly and
     are already quoted; only two could not be matched and offer substitutes.
     This is what a real reviewer normally opens, so it is the one to look at
     first: the quoted table above, the alternatives below, and the contrast
     between "already handled" and "needs a decision".

  B. "Nothing matched" -- the edge case, where every line needs a decision and
     the quotation starts empty. Useful for seeing several requirement blocks
     at once.

Between them they cover the cases worth seeing:

  1. A CHOICE between sizes -- "Clamp 125 mm (rubber lined)", which we don't
     carry, offered as the next size under and the next size over. Two buttons;
     pressing either settles the line and the other disappears.
  2. A CHOICE where the two options give DIFFERENT QUANTITIES -- 58 metres of
     2" pipe, offered as a 6m length and a 4m length. Watch the Qty column
     differ between them (58/6 -> 10, 58/4 -> 15): that is the piece count
     being recomputed per candidate, not copied. One of the two is also at
     zero stock, so the red stock warning shows.
  3. A SINGLE suggestion -- the ordinary one-option case, which reads "Add"
     rather than offering a choice.

Everything it creates is tagged with DEMO_MARKER and removed by --clear, the
quotations included. They are real rows (each consumes a quotation number), so
they are labelled clearly in their remarks and customer display name.

Real catalog items are picked at runtime -- the reasons shown are written to
match whatever it finds, so the demo never claims a size the item isn't.

Usage:
    python manage.py seed_substitute_demo           # create it, print the URL
    python manage.py seed_substitute_demo --clear   # remove everything it made
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from emailagent.models import (
    EnquiryItem, EnquiryItemSuggestion, QuotationDraft, TrackedEmail,
)
from so.models import Customer, Items, Quotation, QuotationItem

# Stamped on every row this command creates, so --clear can find them again
# and a human reading the database can tell this was not a real enquiry.
DEMO_MARKER = '[SUBSTITUTE-DEMO]'


class Command(BaseCommand):
    help = (
        "Create (or remove with --clear) a demo enquiry whose quotation shows the "
        "agent's near-miss substitute suggestions, so the Add buttons can be tried by hand."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear', action='store_true',
            help='Delete the demo enquiry, its quotation and its suggestions, and exit.',
        )

    # -- helpers ----------------------------------------------------------

    def _pick(self, *, contains, in_stock=None, exclude_ids=(), fallback_contains=''):
        """One real catalog item matching `contains`, preferring a priced row so
        the demo shows a sensible line total. in_stock=True/False picks a row
        with/without stock when one exists, so the 0-stock warning can be
        demonstrated. Returns None if the catalog has nothing suitable."""
        for term in (contains, fallback_contains):
            if not term:
                continue
            qs = Items.objects.filter(
                item_description__icontains=term, item_price__gt=0,
            ).exclude(id__in=exclude_ids)
            rows = list(qs[:60])
            if in_stock is not None:
                def has_stock(row):
                    stock = row.total_available_stock
                    if stock is None:
                        stock = row.item_stock
                    return bool(stock and stock > 0)
                preferred = [r for r in rows if has_stock(r) is in_stock]
                if preferred:
                    return preferred[0]
            if rows:
                return rows[0]
        return None

    def _stock_of(self, item):
        stock = item.total_available_stock
        if stock is None:
            stock = item.item_stock
        return stock or 0

    def _pick_two_different_lengths(self, contains):
        """Two catalog rows of the same product family whose PER-UNIT LENGTH
        actually differs, read the same way the quotation code reads it. The
        point of that demo line is that the piece count is recomputed per
        candidate, so two rows of the same length would demonstrate nothing --
        and the length is not the first number in the description ("UPVC PIPE
        4X6MTR" is a 4-inch pipe in 6-metre lengths, not a 4-metre one).

        Returns (longer, shorter) with their lengths, or (None, None)."""
        from emailagent.quotation_agent import _catalog_length_per_unit

        by_length = {}
        for row in Items.objects.filter(
            item_description__icontains=contains, item_price__gt=0,
        )[:400]:
            length = _catalog_length_per_unit(row)
            if length:
                by_length.setdefault(length, []).append(row)
        if len(by_length) < 2:
            return None, None

        # Longest and shortest available, for the most visible difference.
        longest = max(by_length)
        shortest = min(by_length)

        def prefer_zero_stock(rows):
            zero = [r for r in rows if not self._stock_of(r)]
            return zero[0] if zero else rows[0]

        # One of the pair deliberately out of stock where possible, so the red
        # 0-stock warning shows on a real row too.
        return (
            (prefer_zero_stock(by_length[longest]), longest),
            (by_length[shortest][0], shortest),
        )

    def _new_enquiry(self, customer, subject, display_name):
        """A TrackedEmail + Quotation + confirmed QuotationDraft wired together
        the way draft_quotation() leaves them, so the review screen treats the
        result exactly like a real agent-drafted quotation."""
        email = TrackedEmail.objects.create(
            gmail_message_id=f'substitute-demo-{timezone.now().timestamp()}-{subject[:12]}',
            source='gmail',
            sender='demo.customer@example.com',
            sender_name='Demo Customer (substitute test)',
            subject=f'{DEMO_MARKER} {subject}',
            body_text=(
                'This enquiry is generated by manage.py seed_substitute_demo so the '
                'reviewer-facing "closest available alternatives" buttons can be tried '
                'by hand. It is not a real customer enquiry.'
            ),
            received_at=timezone.now(),
            status='pending',
        )
        quotation = Quotation.objects.create(
            customer=customer,
            division='JUNAID',
            license_name='JUNAID_SME',
            customer_display_name=display_name,
            remarks=(
                f'{DEMO_MARKER} Generated by manage.py seed_substitute_demo for manual '
                'testing of the substitute buttons. Remove it with '
                '"manage.py seed_substitute_demo --clear".'
            ),
        )
        QuotationDraft.objects.create(
            tracked_email=email,
            status=QuotationDraft.STATUS_CONFIRMED,
            quotation=quotation,
            matched_customer=customer,
            reasoning='Demo draft -- no agent actually ran for this.',
            generated_at=timezone.now(),
        )
        return email, quotation

    def _add_matched_line(self, email, quotation, order, description, category, quantity, item):
        """One requirement line that DID match the catalog exactly -- the
        EnquiryItem is marked matched and a real QuotationItem is created for
        it, which is what puts it in the quoted table rather than in the
        needs-attention list."""
        price = item.item_price or 0.0
        EnquiryItem.objects.create(
            tracked_email=email,
            description=description,
            category=category,
            brand=item.item_firm or '',
            quantity=str(quantity),
            unit='pcs',
            order=order,
            matched_item=item,
            matched_price=price,
            matched_quantity=quantity,
            matched_unit='pcs',
            match_notes='Exact catalog match.',
        )
        QuotationItem.objects.create(
            quotation=quotation,
            item=item,
            quantity=quantity,
            unit='pcs',
            price=price,
            line_total=quantity * price,
        )
        return quantity * price

    def _add_suggestion_line(self, email, order, description, category, quantity,
                             unit, note, candidates):
        """One requirement line that could NOT be matched, with the near-miss
        substitutes the reviewer chooses between."""
        enquiry_item = EnquiryItem.objects.create(
            tracked_email=email,
            description=description,
            category=category,
            brand='',
            quantity=str(quantity),
            unit=unit,
            order=order,
            match_notes=note[:255],
        )
        for rank, (item, reason) in enumerate(candidates):
            EnquiryItemSuggestion.objects.create(
                enquiry_item=enquiry_item,
                item=item,
                reason=reason[:255],
                order=rank,
            )

    # -- clear ------------------------------------------------------------

    def _clear(self):
        emails = TrackedEmail.objects.filter(subject__startswith=DEMO_MARKER)
        if not emails:
            self.stdout.write('Nothing to clear -- no demo enquiry exists.')
            return

        quotation_ids = set(
            QuotationDraft.objects.filter(tracked_email__in=emails, quotation__isnull=False)
            .values_list('quotation_id', flat=True)
        )

        # Suggestions and enquiry items cascade from TrackedEmail; the
        # quotation does not (the draft's FK is SET_NULL), so delete it by id.
        emails.delete()
        # .delete() returns the TOTAL cascaded object count, not the number of
        # quotations, so count them before deleting rather than reporting a
        # figure that includes every cascaded line item.
        removed = 0
        if quotation_ids:
            doomed = Quotation.objects.filter(id__in=quotation_ids)
            removed = doomed.count()
            doomed.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Cleared the demo enquiries and {removed} demo quotation(s).'
        ))

    # -- create -----------------------------------------------------------

    def handle(self, *args, **options):
        if options['clear']:
            self._clear()
            return

        if TrackedEmail.objects.filter(subject__startswith=DEMO_MARKER).exists():
            self.stdout.write(self.style.WARNING(
                'Demo enquiries already exist -- run with --clear first to rebuild them.'
            ))
            self._print_links()
            return

        customer = (
            Customer.objects.filter(customer_name='DEBIT CUSTOMER ( CASH )').first()
            or Customer.objects.first()
        )
        if not customer:
            self.stderr.write('No customers in the database -- cannot build a demo quotation.')
            return

        # Two clamp sizes to choose between, two pipe lengths to choose between
        # (one deliberately out of stock), and one lone suggestion.
        clamp_small = self._pick(contains='LINED CLAMP', fallback_contains='CLAMP')
        clamp_large = self._pick(
            contains='LINED CLAMP', exclude_ids=[clamp_small.id] if clamp_small else [],
            fallback_contains='CLAMP',
        )
        (pipe_long, long_m), (pipe_short, short_m) = self._pick_two_different_lengths('UPVC PIPE')
        lone = self._pick(contains='RUBBER INSULATION', fallback_contains='VALVE')

        # Ordinary in-stock, priced rows for the lines that matched exactly --
        # they should raise no warnings at all, so the contrast with the
        # lines that need a decision is the only thing on screen.
        used = [i.id for i in (clamp_small, clamp_large, pipe_long, pipe_short, lone) if i]
        exact = []
        for term in ('ELBOW', 'TEE', 'SOCKET', 'VALVE', 'CLAMP'):
            found = self._pick(contains=term, in_stock=True, exclude_ids=used)
            if found and self._stock_of(found) >= 20:
                exact.append(found)
                used.append(found.id)
            if len(exact) == 3:
                break

        if not all([clamp_small, clamp_large, pipe_long, pipe_short, lone]) or len(exact) < 3:
            self.stderr.write('Catalog does not have enough priced/stocked items to build the demo.')
            return

        # What the two pipe options each work out to, so the printed summary
        # states the real figures rather than an assumed 10 vs 15.
        import math
        requested_m = 58
        long_pcs = max(1, math.ceil(requested_m / long_m))
        short_pcs = max(1, math.ceil(requested_m / short_m))

        clamp_candidates = [
            (clamp_small, 'One size UNDER the 125mm requested -- will not close around a 125mm pipe'),
            (clamp_large, 'One size OVER the 125mm requested -- fits, but leaves play unless packed'),
        ]
        pipe_candidates = [
            # Only what makes this option different -- the piece count is already
            # in the Qty column and in the conversion note, and saying it a third
            # time here is what makes a row read as noise.
            (pipe_long, f'Grey, and supplied in {long_m:g}m lengths rather than 5.8m'),
            (pipe_short, f'Grey, and supplied in {short_m:g}m lengths rather than 5.8m'),
        ]
        insulation_candidates = [
            (lone, 'Nearest wall thickness we stock -- same bore, thinner insulation'),
        ]

        with transaction.atomic():
            # -- A. the everyday shape: mostly matched, two needing a decision --
            mixed_email, mixed_quotation = self._new_enquiry(
                customer,
                'Request for quotation -- site materials (partial match)',
                'DEMO -- mixed: quoted lines + substitutes',
            )
            total = 0.0
            for order, (item, qty) in enumerate(zip(exact, (25, 12, 40))):
                total += self._add_matched_line(
                    mixed_email, mixed_quotation, order,
                    description=item.item_description.title(),
                    category='Pipes & Fittings',
                    quantity=qty,
                    item=item,
                )
            self._add_suggestion_line(
                mixed_email, len(exact),
                'Clamp 125 mm (Rubber lined clamp)', 'Electrical & Mechanical Accessories',
                40, 'pcs',
                f'No 125mm rubber-lined clamp in the catalog -- closest stocked sizes are '
                f'{clamp_small.item_code} and {clamp_large.item_code}; please confirm which to substitute.',
                clamp_candidates,
            )
            self._add_suggestion_line(
                mixed_email, len(exact) + 1,
                'UPVC RED 2" Pipe - push fit', 'PVC Pipes & Fittings',
                requested_m, 'MTR',
                'No red 2" push-fit pipe in stock -- two alternative lengths available; '
                'note the piece count differs between them.',
                pipe_candidates,
            )
            mixed_quotation.total_amount = total
            mixed_quotation.grand_total = total
            mixed_quotation.save(update_fields=['total_amount', 'grand_total'])

            # -- B. the edge case: nothing matched at all ----------------------
            empty_email, _ = self._new_enquiry(
                customer,
                'Request for quotation -- clamps and pipes (nothing matched)',
                'DEMO -- nothing matched: substitutes only',
            )
            self._add_suggestion_line(
                empty_email, 0,
                'Clamp 125 mm (Rubber lined clamp)', 'Electrical & Mechanical Accessories',
                40, 'pcs',
                f'No 125mm rubber-lined clamp in the catalog -- closest stocked sizes are '
                f'{clamp_small.item_code} and {clamp_large.item_code}; please confirm which to substitute.',
                clamp_candidates,
            )
            self._add_suggestion_line(
                empty_email, 1,
                'UPVC RED 2" Pipe - push fit', 'PVC Pipes & Fittings',
                requested_m, 'MTR',
                'No red 2" push-fit pipe in stock -- two alternative lengths available; '
                'note the piece count differs between them.',
                pipe_candidates,
            )
            self._add_suggestion_line(
                empty_email, 2,
                'Pipe insulation 25mm wall, 2 inch bore', 'Insulation',
                30, 'pcs',
                'Exact wall thickness not carried -- one close alternative found.',
                insulation_candidates,
            )

        self.stdout.write(self.style.SUCCESS('Demo enquiries created.\n'))
        self.stdout.write('Catalog items used:')
        rows = [('quoted exactly', i) for i in exact] + [
            ('clamp, size under', clamp_small), ('clamp, size over', clamp_large),
            ('pipe, 0 stock', pipe_long), ('pipe, in stock', pipe_short),
            ('single suggestion', lone),
        ]
        for label, item in rows:
            self.stdout.write(
                f'  {label:<18} {item.item_code}  {item.item_description[:44]:<44} '
                f'price {item.item_price}  stock {self._stock_of(item)}'
            )
        self.stdout.write('')
        self._print_links()

    def _print_links(self):
        drafts = list(
            QuotationDraft.objects
            .filter(tracked_email__subject__startswith=DEMO_MARKER, quotation__isnull=False)
            .select_related('quotation', 'tracked_email')
            .order_by('id')
        )
        if not drafts:
            return

        labels = {
            False: 'MIXED -- lines already quoted, plus lines needing a decision',
            True: 'NOTHING MATCHED -- every line needs a decision (edge case)',
        }
        for draft in drafts:
            empty = not draft.quotation.items.all()
            self.stdout.write(self.style.SUCCESS(f'{labels[empty]}'))
            self.stdout.write(
                f'  /quotations/{draft.quotation_id}/details/   '
                f'({draft.quotation.quotation_number}, '
                f'{draft.quotation.items.count()} line(s) already quoted)'
            )
        self.stdout.write('')
        self.stdout.write('What to try on the MIXED one:')
        self.stdout.write('  - the quoted items table shows the lines that matched exactly')
        self.stdout.write('  - below it, only the two lines that could NOT be matched ask for a decision')
        self.stdout.write('  - the clamp line offers TWO options; press one and the other disappears')
        self.stdout.write('  - the pipe line shows a DIFFERENT Qty per option -- the piece count is')
        self.stdout.write('    recomputed against each length, not copied from the requirement')
        self.stdout.write('  - accepting one adds it to the table above and re-totals the quotation')
        self.stdout.write('  - then open Send Quotation: the substitution is disclosed to the client')
        self.stdout.write('')
        self.stdout.write('Remove it all again with: python manage.py seed_substitute_demo --clear')
