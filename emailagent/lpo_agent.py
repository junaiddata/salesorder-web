import difflib
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r'[a-z0-9]+')
_REF_NORMALIZE_RE = re.compile(r'[^A-Z0-9]')


def _tokens(text):
    return set(_WORD_RE.findall((text or '').lower()))


def _normalize_reference(text):
    """Uppercases and strips everything but letters/digits, so 'QTN-1234',
    'qtn 1234', and 'QTN1234' all normalize identically for comparison."""
    return _REF_NORMALIZE_RE.sub('', (text or '').upper())


def _resolve_single_customer(customer_name):
    """Resolves a stated customer name to exactly one so.Customer, or None
    if it's blank, unresolved, or ambiguous (2+ matches) -- shared by
    find_matching_quotation's customer+item fallback and
    build_sales_order_directly_from_lpo, so an ambiguous customer name is
    never silently guessed at by either path."""
    from so.models import Customer

    customer_name = (customer_name or '').strip()
    if not customer_name:
        return None
    matches = list(Customer.objects.filter(customer_name__icontains=customer_name)[:2])
    return matches[0] if len(matches) == 1 else None


def _parse_amount(text):
    """Best-effort parse of a classifier-extracted amount string (e.g.
    'AED 12,500.00', 'Total: 12500') into a float. Never raises -- returns
    None when nothing numeric can be recovered."""
    if not text:
        return None
    cleaned = re.sub(r'[^0-9.]', '', text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_matching_quotation(lpo_request):
    """Tries, in order, to match `lpo_request` to an existing so.Quotation.
    Never raises; returns (quotation_or_None, method, score_or_None,
    candidates: list[Quotation]).

    ADVISORY ONLY. Nothing here creates or converts anything any more: the
    agent raises orders from the LPO's own contents alone (see
    match_and_maybe_convert), and this is called solely to hand a human a
    shortlist on the review page once that has already failed. The scores
    and thresholds below therefore only order that shortlist -- they no
    longer gate a real financial document.

    1. Exact reference match -- normalizes referenced_quotation_number and
       every Quotation.quotation_number the same way; a unique normalized
       match wins outright, regardless of that quotation's current status
       (eligibility to actually convert it is checked at conversion time,
       in views.lpo_request_convert).
    2. Fuzzy reference match -- only when a reference was stated but step 1
       found nothing. Surfaces close matches (stdlib difflib, ratio >=
       EMAILAGENT_LPO_FUZZY_MATCH_THRESHOLD) for a human to confirm.
    3. Customer + item/amount overlap fallback -- only when no reference
       was stated at all, or steps 1-2 found nothing. Requires the LPO's
       stated customer name to resolve to EXACTLY ONE so.Customer (an
       ambiguous customer must never feed a scoring heuristic) before
       scoring that customer's recent quotations.
    """
    from so.models import Quotation

    from .models import LPORequest

    referenced = (lpo_request.referenced_quotation_number or '').strip()

    if referenced:
        normalized_ref = _normalize_reference(referenced)
        if normalized_ref:
            exact_matches = [
                q for q in Quotation.objects.all().only('id', 'quotation_number')
                if _normalize_reference(q.quotation_number) == normalized_ref
            ]
            if len(exact_matches) == 1:
                quotation = Quotation.objects.get(id=exact_matches[0].id)
                return quotation, LPORequest.MATCH_EXACT_NUMBER, 1.0, [quotation]

            fuzzy_scored = []
            threshold = settings.EMAILAGENT_LPO_FUZZY_MATCH_THRESHOLD
            for q in Quotation.objects.all().only('id', 'quotation_number'):
                ratio = difflib.SequenceMatcher(None, normalized_ref, _normalize_reference(q.quotation_number)).ratio()
                if ratio >= threshold:
                    fuzzy_scored.append((ratio, q.id))
            if fuzzy_scored:
                fuzzy_scored.sort(key=lambda pair: -pair[0])
                top = fuzzy_scored[:5]
                candidates = list(Quotation.objects.filter(id__in=[qid for _, qid in top]))
                candidates.sort(key=lambda q: next(-ratio for ratio, qid in top if qid == q.id))
                best_score = top[0][0]
                return candidates[0], LPORequest.MATCH_FUZZY_NUMBER, best_score, candidates

    customer = _resolve_single_customer(lpo_request.customer_name_stated)
    if customer:
        cutoff = (timezone.now() - timedelta(days=settings.EMAILAGENT_LPO_MATCH_LOOKBACK_DAYS)).date()
        lpo_items = list(lpo_request.items.all())
        lpo_item_tokens = [_tokens(item.description) for item in lpo_items]

        scored = []
        candidate_quotations = (Quotation.objects
                                 .filter(customer=customer, quotation_date__gte=cutoff)
                                 .prefetch_related('items__item'))
        for q in candidate_quotations:
            q_item_tokens = [_tokens(qi.item.item_description) for qi in q.items.all() if qi.item_id]
            matched_lines = 0
            for hint_tokens in lpo_item_tokens:
                if not hint_tokens:
                    continue
                if any(len(hint_tokens & qt) >= 2 for qt in q_item_tokens):
                    matched_lines += 1
            item_score = (matched_lines / len(lpo_item_tokens)) if lpo_item_tokens else 0.0

            # Compared like-for-like: Quotation.grand_total is stored EXCL.
            # VAT (see so/views_quotation.py, which adds the 5% only when
            # displaying/printing), so the LPO's own excl-VAT total is the
            # right counterpart. This used to compare it against
            # LPORequest.total_amount, which is the total INCL. VAT -- a
            # genuinely matching quotation then differed by the whole 5%,
            # far outside this 2% tolerance, so the bonus never applied when
            # it should have (and could apply to an unrelated quotation
            # whose excl-VAT total happened to equal this LPO's incl-VAT one).
            # Falls back to deriving it from the incl-VAT figure when the LPO
            # didn't break the excl-VAT line out separately.
            lpo_excl_vat = lpo_request.total_excl_vat
            if not lpo_excl_vat and lpo_request.total_amount:
                lpo_excl_vat = lpo_request.total_amount / 1.05
            amount_bonus = 0.0
            if lpo_excl_vat and q.grand_total:
                if abs(lpo_excl_vat - q.grand_total) <= 0.02 * q.grand_total:
                    amount_bonus = 0.1

            score = min(1.0, item_score + amount_bonus)
            if score > 0:
                scored.append((score, q))

        if scored:
            scored.sort(key=lambda pair: -pair[0])
            top = scored[:5]
            candidates = [q for _, q in top]
            return candidates[0], LPORequest.MATCH_CUSTOMER_ITEM, top[0][0], candidates

    return None, LPORequest.MATCH_NONE, None, []


def _resolve_source_attachment(tracked_email, result):
    """The EmailAttachment the classifier tagged source='lpo_document', if
    any -- matched by filename against result.attachment_sources (see
    services.py, which persists every non-supplier_reference attachment
    before this runs)."""
    from .models import EmailAttachment

    lpo_filename = next(
        (filename for filename, source in (result.attachment_sources or {}).items() if source == 'lpo_document'),
        None,
    )
    if not lpo_filename:
        return None
    return EmailAttachment.objects.filter(tracked_email=tracked_email, filename=lpo_filename).first()


_ITEM_MATCH_MIN_TOKENS = 2
# An LPO line with fewer than this many meaningful (3+ char) words has too
# little signal to match against the 10k+ item catalog safely -- treated as
# unmatched rather than guessing.

_ITEM_MATCH_SCAN_LIMIT = 2000
# Upper bound on rows _match_catalog_item will examine after narrowing, set
# well above any legitimate narrowing (the widest measured on this catalog is
# ~350 rows) so it acts purely as a safety stop. Hitting it means the line was
# too vague to narrow usefully, which is treated as "no confident match"
# rather than matching against an arbitrary subset.


def _match_catalog_item(description):
    """Best-effort match of one LPO line's free-text description to a real
    so.Items catalog row -- deliberately conservative, since this feeds a
    real financial document (a Sales Order) with no human review (see
    build_sales_order_directly_from_lpo): every one of the description's
    own significant (3+ char) words must ALSO appear in the candidate's
    item_description -- not just a partial overlap -- and exactly one
    catalog item may satisfy that. Returns the matched Items row, or None
    if the description has too little signal, or the match is missing or
    ambiguous."""
    from so.models import Items

    hint_tokens = {t for t in _tokens(description) if len(t) >= 3}
    if len(hint_tokens) < _ITEM_MATCH_MIN_TOKENS:
        return None

    # 10k+ catalog rows -- too many to token-score in Python without a DB
    # pre-filter first. Narrows using the two longest (most distinctive)
    # words, then scores the narrowed set exactly.
    narrowing_tokens = sorted(hint_tokens, key=len, reverse=True)[:2]
    candidates_qs = Items.objects.all()
    for token in narrowing_tokens:
        candidates_qs = candidates_qs.filter(item_description__icontains=token)

    # Every row the narrowing left is examined -- no arbitrary window. A
    # two-word narrowing routinely exceeds 200 rows on this catalog
    # (measured: "pipe"+"upvc" 212, "valve"+"brass" 254, "elbow"+"pvc" 346),
    # and the old unordered [:200] slice both hid the correct item and, worse,
    # could leave exactly one OTHER superset row inside that arbitrary window
    # -- which then passed the "unique match" test and went onto a real Sales
    # Order that nobody reviews. Ordering is fixed so the same LPO line always
    # resolves the same way. The subset test below is what actually decides a
    # match, so this only bounds a pathological narrowing, and reaching the
    # bound means "too vague to be sure" -- which must not auto-create.
    candidates = list(candidates_qs.order_by('item_code')[:_ITEM_MATCH_SCAN_LIMIT])
    if len(candidates) >= _ITEM_MATCH_SCAN_LIMIT:
        logger.warning(
            f"_match_catalog_item: {description!r} narrowed to {_ITEM_MATCH_SCAN_LIMIT}+ candidates "
            "-- too broad to match safely, leaving unmatched for human review."
        )
        return None

    matches = [
        item for item in candidates
        if hint_tokens <= _tokens(item.item_description)
    ]
    return matches[0] if len(matches) == 1 else None


def build_sales_order_directly_from_lpo(lpo_request):
    """Builds a real so.SalesOrder straight from `lpo_request`'s own
    extracted customer, line items and prices -- the ONLY way the agent
    auto-creates an order from an LPO. A customer's Purchase Order is
    itself the confirmation of an already-agreed order, so the agent
    raises the order from that document alone and never consults, matches
    or converts a Quotation to do it. (Quotation matching still exists,
    but only runs for a human's benefit once this has already failed --
    see match_and_maybe_convert.)

    Deliberately all-or-nothing and conservative -- every one of the
    following must resolve confidently, or this creates NOTHING and
    returns (None, reason) so the caller falls back to
    LPORequest.STATUS_NEEDS_REVIEW exactly like an unresolved LPO always
    has:
      - the stated customer name must resolve to exactly one so.Customer
        (see _resolve_single_customer)
      - EVERY line item must match exactly one catalog item (see
        _match_catalog_item) -- an order silently missing a line the
        customer actually asked for is worse than no order at all.
      - EVERY line item must carry a price taken off the LPO (see the
        unpriced-line gate below).

    Line pricing comes from the LPO itself (LPORequestItem.price): the PO
    states the pricing the customer has already agreed to and is ordering
    against, so that is the figure the order must be raised at. The
    catalog rate (quotation_agent._resolve_price) is deliberately NOT used
    as a fallback for a line the extraction couldn't price -- that would
    quietly bill a different number than the PO the customer sent, which
    is the exact mismatch this path exists to avoid.

    Tagged created_via=SalesOrder.CREATED_VIA_AGENT_LPO_DIRECT -- still
    distinct from CREATED_VIA_AGENT_LPO, which is now only ever reached by
    orders the agent created before this became the sole path (a human
    converting a quotation from the review page stamps CREATED_VIA_MANUAL),
    so agent orders stay identifiable as raised from the PO alone."""
    from django.db import transaction

    from emailagent.quotation_agent import _is_length_unit, _parse_quantity
    from so.models import OrderItem, SalesOrder

    customer = _resolve_single_customer(lpo_request.customer_name_stated)
    if not customer:
        return None, (
            f"Customer '{lpo_request.customer_name_stated or '(not stated)'}' could not be resolved "
            "to exactly one existing customer."
        )

    lpo_items = list(lpo_request.items.all())
    if not lpo_items:
        return None, "No line items were extracted from this LPO."

    # A line ordered in METERS cannot be turned into a piece count here
    # without the same catalog-length conversion quotations use -- and unlike
    # a quotation, nothing on this path is reviewed before it becomes a real
    # order. Left unconverted (as it was), "24 MTR" of 6m pipe silently
    # became 24 pieces = 144 m, six times what the customer ordered. Rather
    # than convert unreviewed, these go to a human, consistent with this
    # function's all-or-nothing contract. Ordinary count units are unaffected.
    length_unit_lines = [
        f"{item.description[:60]} ({item.quantity} {item.unit})"
        for item in lpo_items if _is_length_unit(item.unit)
    ]
    if length_unit_lines:
        return None, (
            "Line(s) are ordered by length, not piece count, so the quantity needs converting "
            "against each item's per-piece length before an order can be raised: "
            + "; ".join(length_unit_lines[:10])
        )

    # _parse_quantity falls back to 1 for anything it can't read ("", "TBD",
    # "as required"). That is a reasonable default on the quotation path,
    # where a person checks the figure -- here it would put a silent
    # quantity of 1 on a real order, so an unreadable quantity is escalated
    # instead of defaulted.
    unreadable_quantities = []
    for item in lpo_items:
        try:
            if float(str(item.quantity).strip()) > 0:
                continue
        except (TypeError, ValueError, AttributeError):
            pass
        unreadable_quantities.append(f"{item.description[:60]} (quantity: '{item.quantity}')")
    if unreadable_quantities:
        return None, (
            "Line(s) have no usable quantity, so the order cannot be raised automatically: "
            + "; ".join(unreadable_quantities[:10])
        )

    # The order is raised at the LPO's OWN prices, so a line the extraction
    # couldn't put a price on has nothing to bill against. Falling back to the
    # catalog rate here would quietly raise the order at a different figure
    # than the PO the customer sent -- escalated like an unreadable quantity
    # instead. A stated 0.00 is left alone: free-of-charge lines are genuinely
    # written on customer POs, and that is a price, not a missing one.
    unpriced_lines = [
        item.description[:60] for item in lpo_items
        if item.price is None or item.price < 0
    ]
    if unpriced_lines:
        return None, (
            "Line(s) have no price stated on the LPO, so the order cannot be raised "
            "automatically at the customer's agreed pricing: "
            + "; ".join(unpriced_lines[:10])
        )

    resolved = []
    unmatched_descriptions = []
    for lpo_item in lpo_items:
        catalog_item = _match_catalog_item(lpo_item.description)
        if catalog_item:
            resolved.append((lpo_item, catalog_item))
        else:
            unmatched_descriptions.append(lpo_item.description[:80])

    if unmatched_descriptions:
        return None, (
            "Could not confidently match every line item to the catalog -- unmatched: "
            + "; ".join(unmatched_descriptions[:10])
        )

    with transaction.atomic():
        sales_order = SalesOrder.objects.create(
            customer=customer,
            division='JUNAID',
            salesman=customer.salesman,
            created_via=SalesOrder.CREATED_VIA_AGENT_LPO_DIRECT,
        )

        order_items = []
        total_amount = 0.0
        for lpo_item, catalog_item in resolved:
            quantity = _parse_quantity(lpo_item.quantity)
            # Straight off the customer's PO -- guaranteed non-None by the
            # unpriced-line gate above. Flagged is_custom_price whenever it
            # differs from the catalog rate, exactly as the quotation
            # conversion path does, so the order screens show it as a
            # deliberately-set price rather than a stale catalog figure.
            # Unlike that path this does NOT write the rate back to
            # CustomerPrice: a price agreed on one PO is for that order, and
            # persisting it would silently re-apply to later drafts for the
            # same customer (the reason quotation_agent._resolve_price stopped
            # reading that table at all).
            price = lpo_item.price
            order_items.append(OrderItem(
                order=sales_order, item=catalog_item, quantity=quantity, price=price, unit='pcs',
                is_custom_price=abs(float(price) - float(catalog_item.item_price)) > 0.01,
            ))
            total_amount += quantity * price

        OrderItem.objects.bulk_create(order_items)
        sales_order.total_amount = total_amount
        sales_order.tax = round(0.05 * total_amount, 2)
        sales_order.save()

    return sales_order, (
        f"Auto-created Sales Order {sales_order.order_number} directly from this LPO's own "
        f"{len(resolved)} line item(s), matched to the catalog and priced at the LPO's own "
        f"stated rates, for customer {customer.customer_name}. No quotation was involved."
    )


def match_and_maybe_convert(lpo_request):
    """Auto-creates a real so.SalesOrder for `lpo_request` straight from its
    OWN extracted customer, line items and prices, via
    build_sales_order_directly_from_lpo (tagged
    created_via='agent_lpo_direct').

    A customer's Purchase Order is the confirmation of an already-agreed
    order, so the agent raises the order from that document alone: it no
    longer matches, eligibility-checks or converts a Quotation on the way,
    and a quotation that is missing, unapproved or already converted can no
    longer hold up an order the LPO itself fully describes.

    That build is deliberately all-or-nothing (see its docstring): if the
    customer doesn't resolve to exactly one record, or ANY line is ordered
    by length, lacks a usable quantity, lacks a price, or doesn't match
    exactly one catalog item, it creates NOTHING and the request lands at
    STATUS_NEEDS_REVIEW with that specific blocker recorded.

    ONLY in that failed case is find_matching_quotation then run -- purely
    to hand the human picking this up a shortlist of quotations they could
    convert instead from the review page (views.lpo_request_convert, which
    stamps created_via='manual'). It never runs on the success path, so a
    confirmed LPO costs no quotation scan at all, and its result never
    feeds an automatic conversion either way.

    Pulled out of process_lpo so it can ALSO be re-run later against an
    LPORequest whose extracted data has since been corrected, or whose
    customer/items now resolve against a catalog that has moved -- see
    emailagent.views.lpo_request_recheck_match. Mutates and saves
    `lpo_request`; never raises (failures are recorded on the request
    itself, same contract as process_lpo)."""
    from .models import LPORequest

    sales_order, direct_reason = build_sales_order_directly_from_lpo(lpo_request)

    if sales_order:
        lpo_request.sales_order = sales_order
        lpo_request.status = LPORequest.STATUS_CONFIRMED
        lpo_request.match_reasoning = direct_reason
        # Cleared rather than left as they were: a re-check may be running
        # over a request that failed earlier and picked up candidate
        # quotations then, none of which had anything to do with the order
        # just raised from the LPO itself.
        lpo_request.matched_quotation = None
        lpo_request.match_method = LPORequest.MATCH_NONE
        lpo_request.match_score = None
        lpo_request.save()
        lpo_request.candidate_quotations.clear()
    else:
        # Nothing could be raised automatically, so a human has to finish
        # this one -- look for quotations they might convert instead.
        # Advisory only: nothing below this point creates or converts
        # anything (see find_matching_quotation's docstring).
        quotation, method, score, candidates = find_matching_quotation(lpo_request)
        lpo_request.matched_quotation = quotation
        lpo_request.match_method = method
        lpo_request.match_score = score

        fallback_hint = ''
        if quotation is not None:
            fallback_hint = (
                f" A possible related quotation was also found ({quotation.quotation_number}) and is "
                "listed below -- convert that instead if it is the right one."
            )

        lpo_request.status = LPORequest.STATUS_NEEDS_REVIEW
        lpo_request.match_reasoning = (
            f"Could not auto-create a Sales Order from this LPO: {direct_reason} "
            f"Review the extracted details below and raise the order manually.{fallback_hint}"
        )
        lpo_request.save()
        lpo_request.candidate_quotations.set(candidates)

    if lpo_request.status == LPORequest.STATUS_CONFIRMED and lpo_request.sales_order_id:
        # Best-effort/non-blocking -- see stock_check's own docstring.
        from . import stock_check

        stock_check.run_stock_check_for_sales_order(lpo_request.sales_order)

    return lpo_request


def process_lpo(tracked_email, result):
    """Best-effort entry point used by services.py -- creates the
    LPORequest + LPORequestItem rows from the classifier's extracted
    fields, then runs match_and_maybe_convert, which auto-creates a real
    so.SalesOrder straight from those rows (the LPO's own customer, items
    and prices) whenever every one of them resolves confidently, and never
    consults a quotation to do it. Never raises: a failure is recorded on
    the request itself so it never blocks email tracking."""
    from .models import LPORequest, LPORequestItem

    lpo_request, _ = LPORequest.objects.get_or_create(tracked_email=tracked_email)
    try:
        lpo_request.lpo_number = result.lpo_number
        lpo_request.lpo_date = result.lpo_date
        lpo_request.customer_name_stated = result.lpo_customer_name
        lpo_request.referenced_quotation_number = result.lpo_referenced_quotation_number
        lpo_request.delivery_terms = result.lpo_delivery_terms
        lpo_request.payment_terms = result.lpo_payment_terms
        lpo_request.total_amount = _parse_amount(result.lpo_total_amount)
        lpo_request.total_discount = _parse_amount(result.lpo_total_discount)
        lpo_request.total_excl_vat = _parse_amount(result.lpo_total_excl_vat)
        lpo_request.total_vat = _parse_amount(result.lpo_total_vat)
        lpo_request.amount_in_words = result.lpo_amount_in_words
        lpo_request.source_attachment = _resolve_source_attachment(tracked_email, result)
        lpo_request.save()

        lpo_request.items.all().delete()
        LPORequestItem.objects.bulk_create([
            LPORequestItem(
                lpo_request=lpo_request,
                description=item.get('description', ''),
                extra_description=item.get('extra_description', ''),
                quantity=item.get('quantity', ''),
                unit=item.get('unit', ''),
                price=_parse_amount(item.get('price', '')),
                discount_percent=item.get('discount_percent', ''),
                vat_amount=_parse_amount(item.get('vat_amount', '')),
                amount=_parse_amount(item.get('amount', '')),
                order=i,
            )
            for i, item in enumerate(result.lpo_items)
            if item.get('description')
        ])

        lpo_request.error = ''
        lpo_request.save()

        match_and_maybe_convert(lpo_request)
    except Exception as exc:
        logger.exception(f"process_lpo failed for TrackedEmail {tracked_email.id}")
        lpo_request.status = LPORequest.STATUS_FAILED
        lpo_request.error = str(exc)[:2000]
        lpo_request.save()

    return lpo_request
