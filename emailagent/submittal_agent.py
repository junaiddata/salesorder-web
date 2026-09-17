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


def _material_description(material):
    """The material's description WITHOUT its model number -- what a customer
    would actually call the product. Separate from _material_haystack because
    the whole-description-contained rule in match_submittal_materials only
    makes sense against the prose: a model number is precisely the part a
    requirement line tends not to repeat."""
    data = material.data or {}
    return data.get('item_description', '') or data.get('description', '')


# Shortest token that may have a plural "s" stripped. Four keeps "taps"->"tap"
# and "valves"->"valve" while leaving genuine three-letter words that merely end
# in s ("gas", "abs", "pvc"... ) alone.
_PLURAL_MIN_LEN = 4


def _stemmed(tokens):
    """Tokens with a trailing plural "s" removed, so a requirement asking for
    "hose bib taps" still matches a catalog row described as "Bib Tap". Naive
    on purpose -- these are short product nouns, not prose, and a real stemmer
    would be a dependency (and a behaviour change) for no extra recall here."""
    return {t[:-1] if len(t) >= _PLURAL_MIN_LEN and t.endswith('s') else t for t in tokens}


# Mirrors quotation_agent.DEFAULT_BRAND_NOTE_PREFIX: a fixed, code-controlled
# marker in a draft's reasoning saying this submittal is for OUR OWN standard
# brand because the customer never named one -- so the review banner and the
# Submittal Drafts queue can tell a brand the client asked for apart from one
# we chose on their behalf. Every agent-drafted submittal already lands at
# STATUS_NEEDS_REVIEW, and this is the single most important thing for that
# human to check before sending: a submittal under the wrong brand asks the
# consultant to approve a product the client never asked about.
DEFAULT_BRAND_NOTE_PREFIX = (
    "No brand was specified by the customer -- drafted for our standard brand for this category"
)


def default_brand_for_requirement(category, description=''):
    """OUR standard/default brand for a requirement the customer left the
    brand blank on, keyword-matched against its category and description.
    Returns the brand name (e.g. 'PEGLER'), or '' when no bucket fits.

    The buckets come from quotation_agent.DEFAULT_BRANDS -- the SAME table the
    quotation agent defaults with -- so an enquiry quoted under Pegler and the
    submittal drafted for it agree on the brand instead of each picking their
    own. The quotation side applies that table with Claude's judgement; here it
    has to be deterministic (this module is deliberately not a tool-calling
    agent -- see the header), so within one piece of text the most specific
    bucket wins (lowest `priority`) and keywords match on WHOLE WORDS only: a
    plain substring test would read "tap" out of "tape" and "drain" out of
    "drainage board".

    The DESCRIPTION decides whenever it matches any bucket at all, and the
    category is only a fallback for descriptions that match none. That order is
    what reproduces the judgement call the prompt spells out -- "a floor drain
    or manhole cover is Drainage even if its category was tagged generically as
    Sanitary Ware". Categories come from the classifier and are routinely
    broader than the line they label, so letting one win over the product's own
    wording would file every floor drain on a Sanitary Ware enquiry under our
    bathroom-fittings brand."""
    from .quotation_agent import DEFAULT_BRANDS

    buckets = sorted(DEFAULT_BRANDS, key=lambda e: e['priority'])
    for text in (description, category):
        text_lower = (text or '').strip().lower()
        if not text_lower:
            continue
        for entry in buckets:
            for keyword in entry['keywords']:
                if re.search(r'\b' + re.escape(keyword) + r'\b', text_lower):
                    return entry['brand']
    return ''


def _effective_brand(item, tracked_email, sibling_brands=()):
    """The brand one selected SubmittalRequestItem should be drafted under,
    plus where that brand came from:
      'item'    -- the customer named it on this line
      'email'   -- they named ONE brand for the whole request (classifier.py's
                   submittal_brand, set only when the email states a single
                   overall brand)
      'sibling' -- they named none on this line, but ANOTHER item in the same
                   selection names a brand whose catalog actually carries this
                   item (see below)
      'default' -- they named none anywhere, so our own standard brand for the
                   item's category applies (default_brand_for_requirement)
      ''        -- none of those, which fails exactly as a brandless item
                   always did

    The order matters: a brand the CUSTOMER stated anywhere has to beat one we
    picked for them. Without the email-level step, a mail naming "Hepworth"
    once at the top and then listing bare product descriptions would be drafted
    under our standard brand for each line's category instead of Hepworth --
    substituting a brand the client had in fact already specified.

    The sibling step covers the shape that motivated it: a request whose first
    line reads "Plumbing Valves (gate valve, Y strainer, ..., hose bib taps)"
    under Pegler, followed by a bare "Bib Tap" line. The category table sends
    an unqualified "Bib Tap" to our bathroom-fittings brand, which is right in
    general and wrong here -- Pegler is named one line up and stocks six Bib
    Tap models. A sibling brand is only taken when that brand's catalog
    actually MATCHES this item, so it can never pull an item under a brand
    that has nothing like it; that is also what keeps it ahead of the generic
    table, which matches on category wording alone with no catalog evidence at
    all."""
    brand = (item.brand or '').strip()
    if brand:
        return brand, 'item'

    email_brand = (getattr(tracked_email, 'submittal_brand', '') or '').strip()
    if email_brand:
        return email_brand, 'email'

    for sibling in sibling_brands:
        sibling_brand, sibling_matched, _unmatched = match_submittal_materials(
            sibling, [item.description])
        if sibling_brand and sibling_matched:
            return sibling, 'sibling'

    default_brand = default_brand_for_requirement(item.category, item.description)
    if default_brand:
        return default_brand, 'default'

    return '', ''


# OUR standard model(s) to propose for a product family when the requirement
# names the product but specifies NOTHING about it -- no size, no material, no
# pressure rating. A bare "Gate Valve" otherwise drags every gate valve in the
# catalog onto the submittal (eight of them for Pegler, from 1/2" brass to 12"
# ductile iron), which is not a proposal so much as a catalogue dump for the
# consultant to wade through. The standard pair below is chosen to span the
# whole size range with no gap: 10751 is bronze 1/2"-2", V850 is ductile iron
# 2 1/2"-12".
#
# Keyed by the brand's FIRST word, lowercased -- the same handle
# match_submittal_materials already resolves brands by (a SubmittalBrand named
# "Pegler Valves UK" is reached as "PEGLER"), so a brand renamed from
# "Pegler Valves UK" to "Pegler Yorkshire" keeps working.
#
# `family` keywords select BOTH which hints the rule applies to and which of
# the matched materials it replaces, so a hint naming several products only
# has its gate valves narrowed -- the strainers and check valves alongside
# them are left exactly as matched.
# Both singular and plural spellings are listed per family because these are
# matched as literal whole words against the raw requirement text (see
# _narrow_to_default_models) rather than through _stemmed -- the material
# descriptions they are also matched against are singular, so stemming one
# side only would silently stop the family from resolving.
DEFAULT_MODELS = {
    'pegler': (
        # Two models wherever one alone cannot span the size range: a bronze/
        # brass small-bore body up to 2" plus a cast/ductile iron large-bore
        # body from 2 1/2" to 12". Single-model families are either one size
        # (bib taps are all 1/2") or have one body covering the whole range.
        {'family': ('gate valve', 'gate valves'), 'models': ('10751', 'V850')},
        {'family': ('check valve', 'check valves'), 'models': ('10638', 'V914')},
        {'family': ('strainer', 'strainers'), 'models': ('V913', 'V912')},
        {'family': ('angle valve', 'angle valves'), 'models': ('79',)},
        {'family': ('float valve', 'float valves'), 'models': ('V901',)},
        {'family': ('butterfly valve', 'butterfly valves'), 'models': ('V905',)},
        {'family': ('globe valve', 'globe valves'), 'models': ('1031',)},
        {'family': ('ball valve', 'ball valves'), 'models': ('PB100',)},
        {'family': ('bib tap', 'bib taps'), 'models': ('PB50',)},
    ),
}

# A requirement counts as SPECIFIED -- and so is matched normally, with no
# narrowing -- if it carries any digit (a size, a DN/PN rating, a model number)
# or names a material. Deliberately broad: narrowing is the special case, and
# treating a borderline line as specified merely leaves the previous
# behaviour in place, whereas narrowing a line that did state a size would
# actively drop the model the customer asked for.
_SPEC_MATERIAL_WORDS = (
    'brass', 'bronze', 'gunmetal', 'ductile', 'iron', 'cast', 'stainless',
    'steel', 'copper', 'upvc', 'cpvc', 'pvc', 'ppr', 'pex', 'hdpe',
)


def _has_specification(text):
    """True if the requirement line says anything about WHICH variant it
    wants -- see _SPEC_MATERIAL_WORDS for why this errs towards True."""
    lowered = (text or '').lower()
    if any(ch.isdigit() for ch in lowered):
        return True
    return any(re.search(r'\b' + word + r'\b', lowered) for word in _SPEC_MATERIAL_WORDS)


def _default_model_groups(brand):
    """DEFAULT_MODELS entry for this SubmittalBrand, matched on its first word
    (see DEFAULT_MODELS). Empty tuple when the brand has no standard models."""
    first_word = (brand.name or '').split()
    return DEFAULT_MODELS.get(first_word[0].lower(), ()) if first_word else ()


def _narrow_to_default_models(brand, hint_lower, hits, candidates):
    """Replaces the matched materials of any product family the hint names
    with that family's standard model(s) (see DEFAULT_MODELS). Only ever
    touches materials of a family the hint actually names, so the rest of a
    multi-product line survives untouched; returns `hits` unchanged when the
    brand has no standard models, the hint names no such family, or none of
    the standard models are in the catalog."""
    for group in _default_model_groups(brand):
        patterns = [r'\b' + re.escape(k) + r'\b' for k in group['family']]
        if not any(re.search(p, hint_lower) for p in patterns):
            continue
        family = [m for m in hits
                  if any(re.search(p, (_material_description(m) or '').lower()) for p in patterns)]
        if not family:
            continue
        by_model = {(m.model_no or '').casefold(): m for m in candidates}
        defaults = [by_model[mn.casefold()] for mn in group['models'] if mn.casefold() in by_model]
        if not defaults:
            # The standard models are not in this brand's catalog (not yet
            # imported, or renamed) -- proposing nothing would be worse than
            # the catalogue dump, so leave the full match alone.
            continue
        family_ids = {m.id for m in family}
        hits = [m for m in hits if m.id not in family_ids] + defaults
    return hits


def match_submittal_materials(brand_name, hints):
    """Best-effort match of free-text requirement hints (item descriptions
    and/or model numbers) against the SubmittalMaterial catalog for one
    brand. Returns (brand: SubmittalBrand|None, matched: list[SubmittalMaterial],
    unmatched_hints: list[str]).

    A hint matches a material on any of: a model-number hit (the model
    appears as a whole word in the hint, or the hint is itself part of the
    model), at least two overlapping tokens against the material's
    model_no/description, or the material's ENTIRE description appearing in
    the hint -- that last rule exists because a one-word description like
    "Strainer" can never reach an overlap of two and would otherwise be
    unmatchable. Tokens are compared with plural "s" stripped (see _stemmed)
    so "hose bib taps" reaches "Bib Tap".

    A hint that names a product family but SPECIFIES nothing about it (no
    size, material or pressure -- see _has_specification) is then narrowed to
    that family's standard model(s), when the brand defines any: a bare "Gate
    Valve" proposes Pegler's 10751 and V850 rather than all eight gate valves
    in the catalog. See DEFAULT_MODELS and _narrow_to_default_models.

    EVERY OTHER material a hint matches is attached, not just the best-scoring one.
    A single requirement line routinely names several products at once ("gate
    valve, Y strainer, swing check valve, air vent, foot valve, hose bib
    taps") and keeping only the top scorer silently dropped all but one of
    them -- while still reporting the line as matched, which is the worst
    possible combination. Over-attaching is the right side to err on here:
    every submittal is reviewed before it can be sent (see
    Submittal.needs_verification), so a reviewer deleting a surplus row is
    cheap, whereas a product missing from an approval package is not noticed
    until the consultant rejects it.

    Never guesses across brands, and never raises -- worst case is an empty
    match list for a human to fill in by hand."""
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
    seen_ids = set()
    unmatched_hints = []
    for hint in hints:
        hint_norm = (hint or '').strip()
        if not hint_norm:
            continue
        hint_lower = hint_norm.lower()
        hint_tokens = _stemmed(_tokens(hint_norm))

        hits = []
        for material in candidates:
            model_lower = (material.model_no or '').lower()
            # Whole-word on the model-in-hint side: a bare substring test lets
            # a short model number ("76", "89") match any hint that merely
            # contains those digits inside a size or another code.
            if model_lower and (re.search(r'\b' + re.escape(model_lower) + r'\b', hint_lower)
                                or hint_lower in model_lower):
                hits.append(material)
                continue
            if len(hint_tokens & _stemmed(_tokens(_material_haystack(material)))) >= 2:
                hits.append(material)
                continue
            desc_tokens = _stemmed(_tokens(_material_description(material)))
            if desc_tokens and desc_tokens <= hint_tokens:
                hits.append(material)

        if hits and not _has_specification(hint_norm):
            hits = _narrow_to_default_models(brand, hint_lower, hits, candidates)

        if hits:
            for material in hits:
                if material.id not in seen_ids:
                    seen_ids.add(material.id)
                    matched.append(material)
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
    """Returns (hints: list[str], top_brand_name: str, brand_source: str) for a
    quotation's line items, item_ids-restricted when given -- item_code+
    description hints for catalog matching (see match_submittal_materials),
    the most common item_firm among them as the brand guess, and where that
    guess came from ('item' from item_firm, 'default' from our own standard
    brand table, '' when neither produced anything). Shared by
    draft_submittal_from_quotation and the 'Generate Submittal' view's
    check for a sibling quotation already covering the same brand (see
    sibling_quotation_ids) -- both need the exact same brand guess for
    that check to mean anything, which is also why the default-brand
    fallback lives HERE rather than in draft_submittal_from_quotation: a
    fallback applied on only one of those two paths would make the sibling
    check silently stop matching for brandless quotations."""
    items_qs = quotation.items.select_related('item')
    if item_ids is not None:
        items_qs = items_qs.filter(id__in=item_ids)
    items = list(items_qs)
    hints = [f"{qi.item.item_code} {qi.item.item_description}" for qi in items if qi.item_id]
    brand_counts = Counter(qi.item.item_firm for qi in items if qi.item_id and qi.item.item_firm)
    if brand_counts:
        return hints, brand_counts.most_common(1)[0][0], 'item'

    # Not one selected line records an item_firm -- fall back to OUR standard
    # brand for what the lines actually are (the same table the quotation agent
    # defaults with), so a brandless quotation still yields a usable draft
    # instead of the "no brand on the selected items" dead end. Counted the
    # same way item_firm is, since a mixed selection should follow its
    # majority rather than whichever line happens to come first.
    default_counts = Counter(
        brand for brand in (
            default_brand_for_requirement('', qi.item.item_description)
            for qi in items if qi.item_id
        ) if brand
    )
    if default_counts:
        return hints, default_counts.most_common(1)[0][0], 'default'
    return hints, '', ''



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

    hints, top_brand_name, brand_source = quotation_item_hints(quotation, item_ids)
    if not hints:
        return None, "none of the selected items could be found on this quotation"

    brand, matched, unmatched = match_submittal_materials(top_brand_name, hints)

    if not brand:
        if brand_source == 'default':
            return None, (
                f"the selected items record no brand of their own, and our standard brand for them "
                f"('{top_brand_name}') is not yet set up in the submittal materials library"
            )
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
        f"matched {len(hints) - len(unmatched)}/{len(hints)} selected item(s) to "
        f"{len(matched)} catalog material(s)."
    )
    if brand_source == 'default':
        reasoning += (
            f" {DEFAULT_BRAND_NOTE_PREFIX} -- '{top_brand_name}' is OUR standard brand for that "
            "category, so confirm it is what the client wants approved before sending."
        )
    if unmatched:
        reasoning += " Could not match (add manually): " + "; ".join(u[:80] for u in unmatched[:10])
    return submittal, reasoning


def _tag_items(items, submittal):
    """Marks each SubmittalRequestItem as belonging to `submittal`."""
    for item in items:
        item.submittal = submittal
        item.save(update_fields=['submittal'])


def _group_by_brand(items, tracked_email=None):
    """Groups the given (already human-selected) SubmittalRequestItem rows
    by BRAND ONLY, preserving first-seen order -- a single email commonly
    asks for submittal approval across several DIFFERENT BRANDS at once
    (e.g. Pegler valves, Ariston water heaters, Cosmoplast drainage pipes,
    all for the same project), and each brand needs its OWN submittal
    document. Items of the same brand but different category stay on ONE
    shared submittal -- category is informational only (see
    _category_summary), not a grouping key, since a real submittal package
    is organized by brand, not by the classifier's per-line category label.

    Items the customer stated no brand on are grouped under their EFFECTIVE
    brand (see _effective_brand -- the email's one overall brand, else our own
    standard brand for that item's category) rather than all falling into a
    single brandless group that could never resolve. Two unbranded lines whose
    categories default to DIFFERENT standard brands therefore become two
    submittals, exactly as two explicitly-branded lines would.

    Returns a list of (brand_label, items, sources) tuples, one per distinct
    effective brand, where `sources` counts how many of the group's items
    reached it each way (see _effective_brand). A COUNT rather than one label
    because groups are routinely mixed -- one line naming Pegler outright
    alongside one that only got there by inference -- and the caller's caveat
    has to say which, and how many, rather than tarring a customer-stated
    group with "no brand was specified" or hiding an inferred line inside a
    stated one."""
    from collections import Counter

    # Brands the customer named OUTRIGHT somewhere in this selection, in
    # first-seen order -- the candidates _effective_brand's sibling step tries
    # before falling back to the generic category table.
    stated_brands = []
    seen_stated = set()
    for item in items:
        brand = (item.brand or '').strip()
        if brand and brand.casefold() not in seen_stated:
            seen_stated.add(brand.casefold())
            stated_brands.append(brand)

    groups = {}
    order = []
    for item in items:
        brand, source = _effective_brand(item, tracked_email, stated_brands)
        key = brand.casefold()
        if key not in groups:
            groups[key] = {'brand': brand, 'sources': Counter(), 'items': []}
            order.append(key)
        groups[key]['sources'][source] += 1
        groups[key]['items'].append(item)

    return [(groups[k]['brand'], groups[k]['items'], groups[k]['sources']) for k in order]


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


def _resolve_group_submittal(tracked_email, brand_label, items, group_note='', brand_sources=None):
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

    # Said on every successful outcome below, not just new submittals -- items
    # folded into an existing submittal are just as capable of being folded
    # into the wrong brand.
    sources = brand_sources or {}
    n_default = sources.get('default', 0)
    n_sibling = sources.get('sibling', 0)
    if n_default:
        scope = '' if n_default == len(items) else f" ({n_default} of {len(items)} items)"
        default_note = (
            f" {DEFAULT_BRAND_NOTE_PREFIX}{scope} -- '{brand_label}' is OUR standard brand for that "
            "category, so confirm it is what the client wants approved before sending."
        )
    elif n_sibling:
        scope = 'These items' if n_sibling == len(items) else f"{n_sibling} of these items"
        default_note = (
            f" {scope} stated no brand -- they were grouped under '{brand_label}' because another "
            "item in the same request names it and its catalog carries them."
        )
    else:
        default_note = ''

    if not brand:
        if n_default:
            return None, (
                f"No brand was stated for these items, so our standard brand for their category "
                f"('{brand_label}') would apply -- but that brand is not yet set up in the submittal "
                "materials library. Add it under Submittal > Materials Library, then build this "
                "submittal manually from the email."
            ), False
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
            f"{len(new_materials)} new catalog material(s) from {len(hints) - len(unmatched)}/{len(hints)} "
            "selected item(s) into it instead of creating a duplicate."
        ) + default_note
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
        f"matched {len(hints) - len(unmatched)}/{len(hints)} selected item(s) to "
        f"{len(matched)} catalog material(s)."
    )
    reasoning += default_note
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

    groups = _group_by_brand(items, tracked_email)
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
        brand_label, group_items, brand_sources = remaining.pop(0)
        try:
            submittal, reasoning, _merged = _resolve_group_submittal(
                tracked_email, brand_label, group_items, group_note, brand_sources)
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

    for brand_label, group_items, brand_sources in remaining:
        category_label = _category_summary(group_items)
        try:
            submittal, reasoning, merged = _resolve_group_submittal(
                tracked_email, brand_label, group_items, group_note, brand_sources)
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

