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

    1. Exact reference match -- the ONLY method eligible for auto-create
       (see lpo_agent.process_lpo / the confidence policy in this module's
       docstring). Normalizes referenced_quotation_number and every
       Quotation.quotation_number the same way; a unique normalized match
       wins outright, regardless of that quotation's current status --
       eligibility for actually converting it is checked separately.
    2. Fuzzy reference match -- only when a reference was stated but step 1
       found nothing. Never auto-creates; surfaces close matches (stdlib
       difflib, ratio >= EMAILAGENT_LPO_FUZZY_MATCH_THRESHOLD) for a human
       to confirm.
    3. Customer + item/amount overlap fallback -- only when no reference
       was stated at all, or steps 1-2 found nothing. Never auto-creates.
       Requires the LPO's stated customer name to resolve to EXACTLY ONE
       so.Customer (an ambiguous customer must never feed a scoring
       heuristic) before scoring that customer's recent quotations.
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

            amount_bonus = 0.0
            if lpo_request.total_amount and q.grand_total:
                if abs(lpo_request.total_amount - q.grand_total) <= 0.02 * q.grand_total:
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

    matches = [
        item for item in candidates_qs[:200]
        if hint_tokens <= _tokens(item.item_description)
    ]
    return matches[0] if len(matches) == 1 else None


def build_sales_order_directly_from_lpo(lpo_request):
    """Builds a real so.SalesOrder straight from `lpo_request`'s own
    extracted items -- the fallback match_and_maybe_convert reaches for
    (via _finalize_needs_review_or_direct_build) whenever its quotation
    matching didn't itself produce a usable, confirmable quotation: no
    candidate at all, a candidate that isn't eligible/ready yet (not
    Approved, discount pending, etc.), or an ambiguous/weak match. In
    every one of those cases there's nothing usable to convert, so this
    is the only way such an LPO can still auto-create an order rather
    than sitting stuck waiting on a human (or a quotation that may never
    get approved).

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

    Item pricing uses the SAME company/customer pricing quotations
    already use (quotation_agent._resolve_price) -- never the price
    printed on the customer's own PO, which is unverified PDF-extracted
    text, not something to trust for a real financial document.

    Tagged created_via=SalesOrder.CREATED_VIA_AGENT_LPO_DIRECT --
    deliberately distinct from CREATED_VIA_AGENT_LPO (only for the
    quotation-conversion path) so these are always identifiable later as
    having skipped quotation review entirely."""
    from django.db import transaction

    from emailagent.quotation_agent import _parse_quantity, _resolve_price
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
            price = _resolve_price(customer, catalog_item)
            order_items.append(OrderItem(
                order=sales_order, item=catalog_item, quantity=quantity, price=price, unit='pcs',
            ))
            total_amount += quantity * price

        OrderItem.objects.bulk_create(order_items)
        sales_order.total_amount = total_amount
        sales_order.tax = round(0.05 * total_amount, 2)
        sales_order.save()

    return sales_order, (
        f"Auto-created Sales Order {sales_order.order_number} directly from this LPO's own "
        f"{len(resolved)} line item(s), matched to the catalog, for customer {customer.customer_name}."
    )


def _finalize_needs_review_or_direct_build(lpo_request, review_reason):
    """Shared by every branch of match_and_maybe_convert that would
    otherwise leave the LPORequest at STATUS_NEEDS_REVIEW because its
    quotation-matching result wasn't itself usable (no candidate at all,
    an ineligible candidate, or an ambiguous/weak one) -- tries
    build_sales_order_directly_from_lpo as a last resort before actually
    giving up on auto-creating anything, so a messy QUOTATION situation
    doesn't block an order when the LPO's own customer/items are clean
    enough to stand on their own. `review_reason` explains why the
    quotation path alone didn't confirm this; combined with the direct
    attempt's own outcome either way so the full picture is always on
    the LPORequest, whether it ends up Confirmed or stays Needs Review."""
    from .models import LPORequest

    sales_order, direct_reason = build_sales_order_directly_from_lpo(lpo_request)
    if sales_order:
        lpo_request.sales_order = sales_order
        lpo_request.status = LPORequest.STATUS_CONFIRMED
        lpo_request.match_reasoning = f"{review_reason} {direct_reason}"
    else:
        # Lead with WHY nothing could be auto-created (the actual blocker
        # a human needs to act on) -- the quotation situation is just
        # context at this point, since it was never going to be used
        # either way once a quotation candidate wasn't itself confirmable.
        lpo_request.status = LPORequest.STATUS_NEEDS_REVIEW
        lpo_request.match_reasoning = (
            f"Could not auto-create a Sales Order directly from the LPO's own items: {direct_reason} "
            f"(Quotation situation: {review_reason}) Review the extracted details below and "
            "match/create this manually."
        )


AUTO_CREATE_MIN_SCORE = 0.6
# Floor applied to a customer+item-overlap or fuzzy-number match before it's
# trusted to auto-create (see match_and_maybe_convert) -- an exact reference
# match always scores 1.0 so this never gates that path. Chosen so a single
# candidate that only weakly overlaps on tokens (e.g. shares just one
# generic word) still falls back to human review instead of silently
# creating a real financial document.


def match_and_maybe_convert(lpo_request):
    """Runs find_matching_quotation against `lpo_request`'s CURRENT
    extracted fields/items and, when it resolves to exactly one
    unambiguous, eligible candidate quotation, auto-creates a real
    so.SalesOrder via quotation_conversion_service (tagged
    created_via='agent_lpo' so it's identifiable later -- see
    so.models.SalesOrder.created_via and supervisor.evaluate_lpo).

    In EVERY other case -- no candidate quotation at all, a candidate
    that isn't eligible/ready yet, or an ambiguous/weak match -- falls
    back to build_sales_order_directly_from_lpo (tagged
    created_via='agent_lpo_direct') via _finalize_needs_review_or_direct_build,
    so a Sales Order still gets auto-created whenever the LPO's own
    customer + every line item resolve confidently against the catalog,
    regardless of whether a usable quotation exists. An LPO should never
    sit waiting on a quotation that may never get built (or approved) if
    it can be resolved directly. Only when THAT also fails to resolve
    does the LPORequest actually land at STATUS_NEEDS_REVIEW, with
    whatever quotation candidates were found (if any) plus the reason
    the direct build couldn't confirm it either, for a human to pick up.

    "Unambiguous" = find_matching_quotation returned zero or one candidate
    (an exact reference match always does; a fuzzy-number or customer+item
    match only counts here when just ONE quotation cleared its threshold)
    AND, for anything other than an exact reference match, the match score
    is at least AUTO_CREATE_MIN_SCORE -- a single weak-overlap candidate
    still goes to a human rather than auto-converting.

    Pulled out of process_lpo so it can ALSO be re-run later against an
    LPORequest whose own data hasn't changed but whose matching quotation
    now exists/is newly eligible (e.g. the quotation was only created or
    approved after this LPO first arrived) -- see
    emailagent.views.lpo_request_recheck_match. Mutates and saves
    `lpo_request`; never raises (failures are recorded on the request
    itself, same contract as process_lpo)."""
    from django.db import transaction

    from so import quotation_conversion_service
    from .models import LPORequest

    quotation, method, score, candidates = find_matching_quotation(lpo_request)
    lpo_request.match_method = method
    lpo_request.match_score = score
    lpo_request.matched_quotation = quotation

    unambiguous = quotation is not None and len(candidates) <= 1
    confident = method == LPORequest.MATCH_EXACT_NUMBER or (score or 0) >= AUTO_CREATE_MIN_SCORE

    if method == LPORequest.MATCH_EXACT_NUMBER:
        match_basis = f"by exact reference to '{lpo_request.referenced_quotation_number}'"
    elif method == LPORequest.MATCH_FUZZY_NUMBER:
        match_basis = f"by a close (fuzzy) match to the cited reference '{lpo_request.referenced_quotation_number}'"
    elif method == LPORequest.MATCH_CUSTOMER_ITEM:
        match_basis = "by customer name + item overlap (no usable quotation reference was stated)"
    else:
        match_basis = ''

    if unambiguous and confident:
        eligible, reason = quotation_conversion_service.check_conversion_eligibility(quotation)
        if eligible:
            with transaction.atomic():
                sales_order = quotation_conversion_service.convert_quotation_to_sales_order(
                    quotation, created_via='agent_lpo',
                )
            lpo_request.sales_order = sales_order
            lpo_request.status = LPORequest.STATUS_CONFIRMED
            lpo_request.match_reasoning = (
                f"Matched quotation {quotation.quotation_number} {match_basis}; it was Approved and "
                f"eligible -- auto-created Sales Order {sales_order.order_number}."
            )
        else:
            # A quotation candidate exists but isn't ready to convert --
            # try building the order directly from the LPO's own items
            # instead of leaving it stuck on this quotation (see
            # _finalize_needs_review_or_direct_build).
            lpo_request.candidate_quotations.set([quotation])
            _finalize_needs_review_or_direct_build(
                lpo_request,
                f"Matched quotation {quotation.quotation_number} {match_basis}, but it is not yet "
                f"ready to convert: {reason}",
            )
    elif quotation is not None:
        lpo_request.candidate_quotations.set(candidates)
        if not confident:
            review_reason = (
                f"A possible match ({quotation.quotation_number}, {match_basis}) was found, but the "
                f"match is too weak to trust for a sales order."
            )
        else:
            review_reason = f"Multiple possible quotations were found {match_basis}."
        _finalize_needs_review_or_direct_build(lpo_request, review_reason)
    else:
        # No candidate quotation exists to convert at all.
        _finalize_needs_review_or_direct_build(
            lpo_request,
            "Could not find any candidate quotation -- no quotation reference was stated, and the "
            "customer name/items didn't resolve to one either.",
        )

    lpo_request.save()

    if lpo_request.status == LPORequest.STATUS_CONFIRMED and lpo_request.sales_order_id:
        # Best-effort/non-blocking -- see stock_check's own docstring.
        from . import stock_check

        stock_check.run_stock_check_for_sales_order(lpo_request.sales_order)

    return lpo_request


def process_lpo(tracked_email, result):
    """Best-effort entry point used by services.py -- creates the
    LPORequest + LPORequestItem rows from the classifier's extracted
    fields, then runs match_and_maybe_convert to try to match a quotation
    and (only when that match is unambiguous, confident, and the
    quotation's already eligible) auto-create a real so.SalesOrder. Never
    raises: a failure is recorded on the request itself so it never blocks
    email tracking."""
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
