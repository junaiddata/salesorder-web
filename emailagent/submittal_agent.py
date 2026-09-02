"""Submittal drafting: turns a customer's material-submittal request --
either a CHOSEN subset of an existing so.Quotation's line items (the
'Generate Submittal' action on the quotation page, see
submittal.views.submittal_generate_from_quotation) or, for a PURE
submittal-request email with no pricing ask at all (so no Quotation ever
gets drafted for it -- see quotation_agent.draft_quotation, which only
runs for status='rfq' emails), a CHOSEN subset of that email's own
Requested Submittal Items (checkboxes on the email detail page, see
emailagent.views.submittal_draft_selected) -- into a DRAFT
submittal.Submittal for a human to review and correct through the normal
submittal wizard before it's sent to a client.

Deliberately NOT a tool-calling Claude agent like quotation_agent.py --
SubmittalMaterial is a much smaller, simpler catalog (brand + model_no +
free-text attributes, no stock/pricing to reason about) than the item
master, so a plain keyword/model-number match against the brand's own
material list is precise enough to be a useful starting point. Every
submittal this produces is created with status=STATUS_NEEDS_REVIEW
specifically because this matching is best-effort -- a human always
confirms/corrects it before Send Submittal is allowed
(see submittal.models.Submittal.needs_verification)."""
import logging
import re
from collections import Counter

from django.utils import timezone

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r'[a-z0-9]+')


def _tokens(text):
    return set(_WORD_RE.findall((text or '').lower()))


def _material_haystack(material):
    data = material.data or {}
    parts = [material.model_no, data.get('item_description', ''), data.get('description', '')]
    return ' '.join(p for p in parts if p)


def match_submittal_materials(brand_name, hints):
    """Best-effort match of free-text requirement hints (item descriptions
    and/or model numbers) against the SubmittalMaterial catalog for one
    brand. Returns (brand: SubmittalBrand|None, matched: list[SubmittalMaterial],
    unmatched_hints: list[str]).

    Matching rule, in order: an exact/substring model-number hit, else at
    least two overlapping meaningful (>2 char) tokens between the hint and
    the material's own model_no/description -- picking the material with
    the most overlap. Never guesses across brands, and never raises --
    worst case is an empty match list for a human to fill in by hand."""
    from submittal.models import SubmittalBrand

    brand = None
    name = (brand_name or '').strip()
    if name:
        brand = (SubmittalBrand.objects.filter(name__iexact=name).first()
                 or SubmittalBrand.objects.filter(name__icontains=name).first())
        if not brand:
            # `name` may carry extra words rather than a bare brand -- e.g.
            # the Items catalog's item_firm field sometimes stores something
            # like "PEGLER BRASS VALVE" instead of just "PEGLER", which a
            # plain icontains(name) miss (the brand's own name isn't a
            # substring of that). Fall back to checking whether the
            # SubmittalBrand's own first/most distinctive word appears
            # anywhere in `name` -- brand names are conventionally that
            # first word (e.g. "Pegler" in "Pegler Valves UK").
            name_lower = name.lower()
            for candidate in SubmittalBrand.objects.all():
                first_word = candidate.name.split()[0].lower() if candidate.name else ''
                if first_word and first_word in name_lower:
                    brand = candidate
                    break
    if not brand:
        return brand, [], list(hints)

    candidates = list(brand.materials.all())
    matched = []
    unmatched_hints = []
    for hint in hints:
        hint_norm = (hint or '').strip()
        if not hint_norm:
            continue
        hint_lower = hint_norm.lower()
        hint_tokens = _tokens(hint_norm)

        best, best_score, exact_hit = None, 0, False
        for material in candidates:
            model_lower = (material.model_no or '').lower()
            if model_lower and (model_lower in hint_lower or hint_lower in model_lower):
                best, exact_hit = material, True
                break
            overlap = len(hint_tokens & _tokens(_material_haystack(material)))
            if overlap > best_score:
                best, best_score = material, overlap

        if best and (exact_hit or best_score >= 2):
            if best not in matched:
                matched.append(best)
        else:
            unmatched_hints.append(hint_norm)

    return brand, matched, unmatched_hints


def _create_draft_submittal(*, company, project, client, consultant, main_contractor,
                             mep_contractor, brand, matched_materials, created_via, source_quotation=None):
    from submittal.models import Submittal

    submittal = Submittal.objects.create(
        company=company,
        project=project[:998] if project else '',
        client=(client or '')[:255],
        consultant=(consultant or '')[:255],
        main_contractor=(main_contractor or '')[:255],
        mep_contractor=(mep_contractor or '')[:255],
        title_brand=brand,
        product=brand.name if brand else '',
        compliance_brand=brand,
        created_via=created_via,
        status=Submittal.STATUS_NEEDS_REVIEW,
        source_quotation=source_quotation,
    )
    if matched_materials:
        submittal.materials.set(matched_materials)
    return submittal


def quotation_item_hints(quotation, item_ids=None):
    """Returns (hints: list[str], top_brand_name: str) for a quotation's
    line items, item_ids-restricted when given -- item_code+description
    hints for catalog matching (see match_submittal_materials), and the
    most common item_firm among them as the brand guess. Shared by
    draft_submittal_from_quotation and the 'Generate Submittal' view's
    check for a sibling quotation already covering the same brand (see
    sibling_quotation_ids) -- both need the exact same brand guess for
    that check to mean anything."""
    items_qs = quotation.items.select_related('item')
    if item_ids is not None:
        items_qs = items_qs.filter(id__in=item_ids)
    items = list(items_qs)
    hints = [f"{qi.item.item_code} {qi.item.item_description}" for qi in items if qi.item_id]
    brand_counts = Counter(qi.item.item_firm for qi in items if qi.item_id and qi.item.item_firm)
    top_brand_name = brand_counts.most_common(1)[0][0] if brand_counts else ''
    return hints, top_brand_name


def sibling_quotation_ids(tracked_email):
    """Every quotation id that traces back to this same enquiry thread --
    the primary quotation (or the one an in-thread follow-up was merged
    into) plus every additional-scope quotation drafted for a distinct
    attachment/building in the SAME multi-attachment email (see
    AdditionalQuotationDraft/quotation_agent._group_items_by_scope). A
    multi-attachment enquiry deliberately gets a SEPARATE quotation per
    attachment (different pricing/quantities per building/scope), but the
    same physical brand+item appearing in more than one of those
    attachments should still end up on ONE shared submittal rather than a
    duplicate per quotation -- see the sibling-brand check in
    submittal.views.submittal_generate_from_quotation."""
    ids = set()
    draft = getattr(tracked_email, 'quotation_draft', None)
    if draft:
        if draft.quotation_id:
            ids.add(draft.quotation_id)
        if draft.merged_into_id:
            ids.add(draft.merged_into_id)
    ids.update(
        tracked_email.additional_quotation_drafts
        .exclude(quotation__isnull=True)
        .values_list('quotation_id', flat=True)
    )
    return ids


def draft_submittal_from_quotation(quotation, item_ids=None):
    """Builds a draft Submittal from a CHOSEN subset of an existing
    Quotation's line items -- not every quoted item needs a submittal, so
    the 'Generate Submittal' modal on the quotation page lets a human tick
    which QuotationItems to include; item_ids is that selection (a list of
    QuotationItem ids). Falls back to every line item on the quotation when
    item_ids is None, e.g. for programmatic callers that don't offer a
    selection step. The brand is the most common item_firm among the
    selected QuotationItems, and each line's item_code/item_description is
    matched against that brand's SubmittalMaterial catalog. Never raises;
    returns (submittal_or_None, reasoning: str) -- reasoning explains what
    was matched/missed so the SubmittalDraft record (and the review banner)
    can show it.

    Callers with a multi-attachment enquiry should check
    sibling_quotation_ids + the matched brand FIRST (see
    submittal.views.submittal_generate_from_quotation) and merge into an
    existing sibling submittal instead of calling this -- this function
    always creates a new Submittal."""
    from submittal.models import Submittal

    hints, top_brand_name = quotation_item_hints(quotation, item_ids)
    if not hints:
        return None, "none of the selected items could be found on this quotation"

    brand, matched, unmatched = match_submittal_materials(top_brand_name, hints)

    if not brand:
        stated_brand = top_brand_name or 'no brand on the selected items'
        return None, f"the quoted brand ('{stated_brand}') is not yet set up in the submittal materials library"

    customer_name = quotation.customer_display_name or quotation.customer.customer_name
    company = 'alabama' if quotation.division == 'ALABAMA' else 'junaid'
    submittal = _create_draft_submittal(
        company=company,
        project=f"Submittal for {quotation.quotation_number} -- {customer_name}",
        client=customer_name,
        consultant='', main_contractor='', mep_contractor='',
        brand=brand, matched_materials=matched,
        created_via=Submittal.SOURCE_AGENT_QUOTATION, source_quotation=quotation,
    )

    reasoning = (
        f"Drafted from {quotation.quotation_number} -- brand '{brand.name}', "
        f"matched {len(matched)}/{len(hints)} selected item(s) to the submittal catalog."
    )
    if unmatched:
        reasoning += " Could not match (add manually): " + "; ".join(u[:80] for u in unmatched[:10])
    return submittal, reasoning


def _tag_items(items, submittal):
    """Marks each SubmittalRequestItem as belonging to `submittal`."""
    for item in items:
        item.submittal = submittal
        item.save(update_fields=['submittal'])


def _group_by_brand(items):
    """Groups the given (already human-selected) SubmittalRequestItem rows
    by BRAND ONLY, preserving first-seen order -- a single email commonly
    asks for submittal approval across several DIFFERENT BRANDS at once
    (e.g. Pegler valves, Ariston water heaters, Cosmoplast drainage pipes,
    all for the same project), and each brand needs its OWN submittal
    document. Items of the same brand but different category stay on ONE
    shared submittal -- category is informational only (see
    _category_summary), not a grouping key, since a real submittal package
    is organized by brand, not by the classifier's per-line category label.

    Returns a list of (brand_label, items) tuples, one per distinct brand."""
    groups = {}
    order = []
    for item in items:
        brand = (item.brand or '').strip()
        key = brand.casefold()
        if key not in groups:
            groups[key] = {'brand': brand, 'items': []}
            order.append(key)
        groups[key]['items'].append(item)

    return [(groups[k]['brand'], groups[k]['items']) for k in order]


def _category_summary(items, max_len=255):
    """Human-readable, comma-joined list of the distinct categories among
    `items` (order preserved, blanks skipped) -- purely informational text
    for SubmittalDraft/AdditionalSubmittalDraft.category now that grouping
    is brand-only (see _group_by_brand), so a brand's items can
    legitimately span more than one category label."""
    seen = []
    for item in items:
        cat = (item.category or '').strip()
        if cat and cat not in seen:
            seen.append(cat)
    return ', '.join(seen)[:max_len]


def _find_existing_email_submittal(tracked_email, brand):
    """A submittal already generated for THIS email under `brand`, if any
    -- checked before drafting a new submittal for a group so the same
    brand (selected across more than one 'Generate Submittal' click on the
    email page) lands on ONE shared submittal instead of a duplicate.
    Checks both this email's primary SubmittalDraft and every
    AdditionalSubmittalDraft."""
    from .models import AdditionalSubmittalDraft

    primary = getattr(tracked_email, 'submittal_draft', None)
    if primary and primary.submittal_id and primary.submittal.title_brand_id == brand.id:
        return primary.submittal

    additional = (AdditionalSubmittalDraft.objects
                  .filter(tracked_email=tracked_email, submittal__isnull=False, submittal__title_brand_id=brand.id)
                  .first())
    return additional.submittal if additional else None


def _resolve_group_submittal(tracked_email, brand_label, items, group_note=''):
    """Matches one brand group's selected items against the submittal
    catalog, then either folds them into a submittal already generated
    for this SAME email+brand (see _find_existing_email_submittal) or
    drafts a brand-new one -- shared by the primary group and every
    additional group in draft_submittals_for_selected_items. Never
    raises; returns (submittal_or_None, reasoning: str, merged: bool) --
    merged is True when items were folded into an EXISTING submittal
    rather than a new one being created, so the caller knows not to
    create a new tracking row (AdditionalSubmittalDraft) for it."""
    from submittal.models import Submittal

    hints = [item.description for item in items]
    brand, matched, unmatched = match_submittal_materials(brand_label, hints)

    if not brand:
        stated_brand = brand_label or 'no brand stated in the email'
        return None, (
            f"No submittal was drafted for '{stated_brand}' -- that brand is not yet set up in the "
            "submittal materials library. Add it under Submittal > Materials Library, then build this "
            "submittal manually from the email."
        ), False

    existing = _find_existing_email_submittal(tracked_email, brand)
    if existing:
        existing_material_ids = set(existing.materials.values_list('id', flat=True))
        new_materials = [m for m in matched if m.id not in existing_material_ids]
        if new_materials:
            existing.materials.add(*new_materials)
        reasoning = (
            f"{brand.name} was already covered by a submittal drafted earlier for this email -- merged "
            f"{len(matched)} selected item(s) into it instead of creating a duplicate."
        )
        if unmatched:
            reasoning += " Could not match (add manually): " + "; ".join(u[:80] for u in unmatched[:10])
        return existing, reasoning, True

    submittal = _create_draft_submittal(
        company='junaid',
        project=tracked_email.submittal_project or tracked_email.subject or f"Submittal request from {tracked_email.sender}",
        client=tracked_email.submittal_client or tracked_email.sender_name or tracked_email.sender,
        consultant=tracked_email.submittal_consultant, main_contractor=tracked_email.submittal_main_contractor,
        mep_contractor=tracked_email.submittal_mep_contractor,
        brand=brand, matched_materials=matched,
        created_via=Submittal.SOURCE_AGENT_EMAIL,
    )

    reasoning = (
        f"Drafted from \"{tracked_email.subject}\" ({tracked_email.sender}) -- {brand.name}, "
        f"matched {len(matched)}/{len(hints)} selected item(s) to the submittal catalog."
    )
    if group_note:
        reasoning = f"{group_note} {reasoning}"
    if unmatched:
        reasoning += " Could not match (add manually): " + "; ".join(u[:80] for u in unmatched[:10])
    return submittal, reasoning, False


def draft_submittals_for_selected_items(tracked_email, item_ids):
    """Human-triggered submittal drafting for a CHOSEN subset of this
    email's Requested Submittal Items (checkboxes on that table on the
    email detail page) -- the only entry point for a PURE submittal-
    request email (no pricing ask at all, so no Quotation ever gets
    drafted for it -- see quotation_agent.draft_quotation, which only
    runs for status='rfq' emails) and therefore no quotation-page
    'Generate Submittal' button to use instead. Not every requirement a
    client sends needs a submittal, so a human picks which ones on the
    email detail page and this drafts only those.

    Groups the selected items by BRAND ONLY (see _group_by_brand --
    category differences within the same brand don't split the
    submittal) -- the first group becomes this email's PRIMARY
    SubmittalDraft (created here if this is the first time anything has
    been generated for this email; if the primary slot is already
    confirmed from an earlier selection, every group this call produces
    becomes an AdditionalSubmittalDraft instead), and every other group
    becomes its own AdditionalSubmittalDraft. Items that already belong
    to a drafted submittal (from an earlier call) are silently excluded
    by the caller before this runs, so this is safe to call again later
    with a further batch of items. Never raises; returns the list of
    submittal-or-None results, one per distinct brand actually drafted."""
    from .models import AdditionalSubmittalDraft, SubmittalDraft, SubmittalRequestItem

    items = list(
        SubmittalRequestItem.objects.filter(
            tracked_email=tracked_email, id__in=item_ids, submittal__isnull=True,
        )
    )
    if not items:
        return []

    groups = _group_by_brand(items)
    is_multi_group = len(groups) > 1
    group_note = (
        f"Drafted together from {len(groups)} distinct brands in this selection -- see the "
        "related submittal(s) noted on this email for the others."
        if is_multi_group else ''
    )

    draft, _ = SubmittalDraft.objects.get_or_create(tracked_email=tracked_email)
    remaining = list(groups)
    results = []

    if not (draft.status == SubmittalDraft.STATUS_CONFIRMED and draft.submittal_id):
        brand_label, group_items = remaining.pop(0)
        try:
            submittal, reasoning, _merged = _resolve_group_submittal(tracked_email, brand_label, group_items, group_note)
        except Exception as exc:
            logger.exception(f"draft_submittals_for_selected_items failed for TrackedEmail {tracked_email.id}")
            submittal, reasoning = None, str(exc)[:2000]

        draft.category = _category_summary(group_items)
        if submittal:
            draft.status = SubmittalDraft.STATUS_CONFIRMED
            draft.submittal = submittal
            draft.matched_brand = submittal.title_brand
            draft.reasoning = reasoning
            draft.generated_at = timezone.now()
            draft.error = ''
            _tag_items(group_items, submittal)
        else:
            draft.status = SubmittalDraft.STATUS_FAILED
            draft.error = reasoning
        draft.save()
        results.append(submittal)

    for brand_label, group_items in remaining:
        category_label = _category_summary(group_items)
        try:
            submittal, reasoning, merged = _resolve_group_submittal(tracked_email, brand_label, group_items, group_note)
        except Exception as exc:
            logger.exception(f"draft_submittals_for_selected_items (additional group) failed for TrackedEmail {tracked_email.id}")
            AdditionalSubmittalDraft.objects.create(
                tracked_email=tracked_email, brand=brand_label, category=category_label, error=str(exc)[:2000],
            )
            results.append(None)
            continue

        if merged:
            # Folded into a submittal that already exists for this email --
            # nothing new to track separately (see _find_existing_email_submittal),
            # just tag the items so they aren't offered for selection again.
            _tag_items(group_items, submittal)
            results.append(submittal)
            continue

        AdditionalSubmittalDraft.objects.create(
            tracked_email=tracked_email, brand=brand_label, category=category_label,
            submittal=submittal, reasoning=reasoning, error='' if submittal else reasoning,
        )
        if submittal:
            _tag_items(group_items, submittal)
        results.append(submittal)

    return results

