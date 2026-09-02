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
    from so.models import Customer, Quotation

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

    customer_name = (lpo_request.customer_name_stated or '').strip()
    if customer_name:
        resolved_customers = list(Customer.objects.filter(customer_name__icontains=customer_name)[:2])
        if len(resolved_customers) == 1:
            customer = resolved_customers[0]
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
    Otherwise leaves the LPORequest at STATUS_NEEDS_REVIEW with whatever
    candidates were found for a human to pick from.

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
            lpo_request.status = LPORequest.STATUS_NEEDS_REVIEW
            lpo_request.candidate_quotations.set([quotation])
            lpo_request.match_reasoning = (
                f"Matched quotation {quotation.quotation_number} {match_basis}, but it is not yet ready "
                f"to convert: {reason} Resolve that, then use 'Create Sales Order' on this page."
            )
    elif quotation is not None:
        lpo_request.status = LPORequest.STATUS_NEEDS_REVIEW
        lpo_request.candidate_quotations.set(candidates)
        if not confident:
            lpo_request.match_reasoning = (
                f"A possible match ({quotation.quotation_number}, {match_basis}) was found, but the "
                f"match is too weak to auto-create a sales order -- please confirm the right one below."
            )
        else:
            lpo_request.match_reasoning = (
                f"Multiple possible quotations were found {match_basis} -- please confirm the right "
                f"one below."
            )
    else:
        lpo_request.status = LPORequest.STATUS_NEEDS_REVIEW
        lpo_request.match_reasoning = (
            "Could not find any candidate quotation -- no quotation reference was stated, and "
            "the customer name/items didn't resolve to one either. Review the extracted details "
            "below and match this manually."
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
