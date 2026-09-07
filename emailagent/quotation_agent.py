"""Quotation drafting: turns a confirmed RFQ's extracted requirement items
into a draft quotation -- matching each line to the real item catalog and
guessing the customer -- for a human to review and confirm via the normal
quotation form (so/views_quotation.py::create_quotation). Runs as a
tool-calling agent, same pattern as classifier.py.
"""
import logging
import math
import re

import anthropic
from anthropic import beta_tool
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel
from so.models import Quotation, QuotationItem, Customer, Items
from emailagent.models import AdditionalQuotationDraft, QuotationDraft

from emailagent.tools import lookup_customer, lookup_item_master

logger = logging.getLogger(__name__)


class MatchedItem(BaseModel):
    enquiry_item_index: int
    item_code: str = ""
    unit: str = ""
    notes: str = ""
    default_brand_applied: bool = False


# Set (in Python, not by the agent) on an EnquiryItem's match_notes whenever
# default_brand_applied was true for it -- a fixed, code-controlled prefix
# rather than the agent's own free-text notes, so later callers (the
# "Send Quotation" email compose step in so/views_quotation.py) can reliably
# detect "this line's brand was defaulted" without parsing LLM prose.
DEFAULT_BRAND_NOTE_PREFIX = "No brand specified by customer -- quoted our standard brand for this category"

# Same idea as DEFAULT_BRAND_NOTE_PREFIX, but for the opposite case -- a
# same-thread follow-up email changed an item's brand on an already-matched
# line (see merge_followup_into_quotation). Fixed, code-authored text (not
# the agent's free-text notes) so it reliably shows on the item and can be
# rolled up into the quotation's remarks for a human to see at a glance why
# the brand changed, without having to reopen the original email thread.
BRAND_CHANGE_NOTE_PREFIX = "Customer requested brand change"

_MATCH_NOTES_MAX_LEN = 255  # EnquiryItem.match_notes is a CharField(max_length=255)


def build_match_notes(agent_notes, *fixed_notes):
    """Composes EnquiryItem.match_notes from the agent's free-text notes plus
    any fixed, code-authored markers (DEFAULT_BRAND_NOTE_PREFIX, zero-stock
    warnings, etc.), truncating only the agent's free text to fit within
    match_notes' 255-char limit. The fixed markers are never truncated away --
    callers like so/views_quotation.py's _default_brand_notice_items rely on
    DEFAULT_BRAND_NOTE_PREFIX surviving intact to detect a defaulted brand and
    disclose it to the client; a naive join-then-slice can silently chop the
    marker off the end when the agent's own notes run long."""
    fixed = [n for n in fixed_notes if n]
    fixed_text = "; ".join(fixed)
    agent_notes = (agent_notes or '').strip()
    if not agent_notes:
        return fixed_text[:_MATCH_NOTES_MAX_LEN]
    if not fixed_text:
        return agent_notes[:_MATCH_NOTES_MAX_LEN]
    room = _MATCH_NOTES_MAX_LEN - len(fixed_text) - 2  # 2 = "; " separator
    if room <= 0:
        return fixed_text[:_MATCH_NOTES_MAX_LEN]
    return f"{agent_notes[:room]}; {fixed_text}"


@beta_tool
def submit_quotation_draft(
    items: list[MatchedItem],
    matched_customer_id: int,
    customer_display_name: str,
    reasoning: str,
) -> str:
    """Record your final quotation draft for this enquiry. Call this exactly
    once, after using lookup_item_master (and lookup_customer, if useful) as
    many times as needed -- do not call any other tool afterward.

    Args:
        items: One entry per requirement item from the enquiry (same order
            and count as the items list given to you), matching it to a
            catalog item. Set item_code to the exact code from
            lookup_item_master for your best match, or leave it "" if
            nothing in the catalog matches closely enough -- never guess a
            code that wasn't returned by the tool. unit should be the
            closest of "pcs"/"ctn"/"roll" to what was requested (use "pcs"
            if unclear -- that is the quotation form's default). Use notes
            for anything the reviewer should double check (no confident
            match, brand differs from what was requested, quantity/unit was
            ambiguous, etc.) -- "" if nothing to flag. Set
            default_brand_applied=True when this item's requirement did NOT
            state a brand and you matched it to our standard default brand
            for its category (see the default-brand table below) -- False
            whenever the customer stated their own brand, or no standard
            default applies to this item's category.
        matched_customer_id: The id of an existing customer found via
            lookup_customer that this enquiry is really for, or 0 if none
            of the candidates are a confident match.
        customer_display_name: Best-guess display name for this customer --
            the company name from the email signature/letterhead if
            present, otherwise the sender's own name. Used to label the
            quotation even when matched_customer_id is 0 (quoted as a
            walk-in customer under this name).
        reasoning: One or two sentences on the item-matching and customer
            decisions, including anything uncertain.
    """
    return "Recorded"


@beta_tool
def submit_item_rematch(items: list[MatchedItem], reasoning: str) -> str:
    """Record your final catalog matches for the previously-unmatched items
    listed above. Call this exactly once, after using lookup_item_master as
    many times as needed -- do not call any other tool afterward.

    Args:
        items: One entry per item listed above (same enquiry_item_index
            values you were given), matching it to a catalog item. Set
            item_code to the exact code from lookup_item_master for your
            best match, or leave it "" if nothing in the catalog matches
            closely enough -- never guess a code that wasn't returned by the
            tool. unit should be the closest of "pcs"/"ctn"/"roll" to what
            was requested (use "pcs" if unclear). Use notes for anything the
            reviewer should double check -- "" if nothing to flag. Set
            default_brand_applied=True when this item's requirement did NOT
            state a brand and you matched it to our standard default brand
            for its category (see the default-brand table below) -- False
            otherwise.
        reasoning: One or two sentences on the item-matching decisions,
            including anything uncertain.
    """
    return "Recorded"


# Shared between the initial full draft and the later unmatched-items
# re-check -- keeping this in one place means a synonym/rule added for one
# flow automatically applies to the other.
_ITEM_MATCH_RETRY_RULES = (
    "For EACH requirement item listed below, call lookup_item_master AT LEAST "
    "TWICE with genuinely different search terms before deciding there is no "
    "match -- catalog descriptions often use trade abbreviations and imperial "
    "sizing (e.g. a customer's \"100mm x 100mm surface type floor drain\" may "
    "only exist in the catalog as \"FL/DRAIN 4X4\") rather than the customer's "
    "own wording, so a weak or empty first result does NOT mean nothing exists "
    "-- it means try again differently. Vary each retry: first the requirement "
    "as given, then just the core noun/category (e.g. \"floor drain\" instead "
    "of the full sentence), then the size converted to the other unit system "
    "(mm<->inch: 25mm=1in, 50mm=2in, 100mm=4in, 150mm=6in, 200mm=8in), then the "
    "brand alone. Only after exhausting these should you leave item_code empty "
    "-- never pick a loosely related item just to fill it in, and never stop "
    "after a single search per item. Items with 0 stock are never added to the "
    "quotation regardless of what you pick, so when lookup_item_master shows "
    "more than one reasonable candidate, prefer one with stock > 0 over an "
    "otherwise-equal match that shows stock=0.\n\n"
    "Pipe-fitting items are especially prone to trade-name mismatches between "
    "what the customer wrote and what the catalog SKU description says -- if a "
    "search for the customer's term comes back weak or empty, retry with its "
    "catalog-side equivalent (and vice versa):\n"
    "  bend = elbow\n"
    "  adaptor/adapter = socket (male adaptor = male socket, female adaptor = "
    "female socket)\n"
    "  coupler = socket\n"
    "  reducer = R/bush (reducing bush)\n"
    "  pressure reducing valve = R/VALVE or PRV (e.g. \"PRESSURE R/VALVE\")\n"
    "  cap = plug (threaded cap = threaded plug)\n"
    "  clip = clamp\n"
    "  double tee = cross tee\n"
    "  double yee = cross yee\n"
    "  free socket / slipper socket = repair socket\n"
    "  air vent = vent cowl\n"
    "  door socket / access socket = access pipe\n"
    "This list is not exhaustive -- apply the same customer-wording-vs-SKU-"
    "wording reasoning to other CPVC/UPVC/PPR fitting terms not listed here.\n\n"
    "Brand names get shortened/informal treatment the same way -- notably "
    "\"Cosmo\" always means the catalog brand \"COSMOPLAST\" (there is no "
    "separate \"Cosmo\" brand); search and compare against COSMOPLAST items "
    "when a requirement says Cosmo. Apply the same reasoning to other obvious "
    "brand shorthands you recognize.\n\n"
    "Brand-specific naming can also differ for the exact same fitting type -- "
    "e.g. a PEX elbow/tee with a wall-mounting box: RAKTHERM, JOMIX, VESBO, and "
    "PILSA all name it \"...WITH BOX...\", but COSMOPLAST's catalog calls the "
    "equivalent fitting \"SANITARY (elbow/tee)\" or \"...W/NECK\" instead -- it "
    "never uses the word \"box\". If a requirement needs a boxed/wall-mount "
    "fitting in Cosmo/Cosmoplast and a \"box\" search only turns up other "
    "brands, retry with \"sanitary\" and \"w/neck\" before concluding "
    "Cosmoplast has no equivalent. Treat this as an example of a general "
    "pattern -- a brand having no result for the customer's literal wording "
    "does not mean that brand lacks the product, only that its catalog "
    "description uses different terminology for it.\n\n"
    "If an item's brand field states a preferred brand, prefer a catalog match "
    "whose brand (shown in lookup_item_master's results) equals that preferred "
    "brand over an equally-suitable match from a different brand. If the "
    "wording includes a fallback such as \"or cheapest\" / \"or equivalent\", or "
    "no brand is stated at all and no standard default brand applies (see the "
    "table below), and either no item from the preferred brand is found or you "
    "are choosing freely, compare the price of the reasonable candidates "
    "lookup_item_master returns and pick the cheapest one that is in stock -- "
    "do not just default to the first result returned. Whenever the stated "
    "brand preference could not be honored, say so in notes and name what was "
    "quoted instead (e.g. \"Cosmo not available in this size -- quoted cheapest "
    "in-stock alternative (Brand X)\").\n\n"
    "When an item's brand field is BLANK (the customer did not ask for any "
    "particular brand), do NOT jump straight to \"pick the cheapest in-stock "
    "item\" -- first check whether the item's category/description matches one "
    "of our own standard default brands below, and treat that default brand "
    "exactly like a customer-stated brand preference (search/compare against "
    "it, retry with synonyms/unit conversions before giving up on it). Only "
    "fall back to cheapest-in-stock-across-brands if the item's category "
    "doesn't match any of these, or you genuinely cannot find that default "
    "brand's equivalent after retrying. These are OUR standard/default "
    "brands, not something the customer asked for -- use judgement for which "
    "bucket an item's category/description falls into (e.g. a floor drain or "
    "manhole cover is \"Drainage\" even if its category was tagged generically "
    "as \"Sanitary Ware\"). Whenever you use one of these, set "
    "default_brand_applied=True on that item -- this is required so the "
    "quotation can flag to the client that no brand was specified and our "
    "standard brand was used instead:\n"
    "  Pipes & Fittings (PVC/PPR/CPVC pipes, elbows, tees, sockets, unions, "
    "reducers, etc.) -> COSMOPLAST\n"
    "  Water Heaters -> ARISTON (the \"ARISTON - ITALY\" catalog brand "
    "specifically -- not the ARISTON-CHINA/BANGLADESH/OLD/SOLAR variants)\n"
    "  Valves (gate/ball/check/angle/pressure-reducing, etc.) -> PEGLER\n"
    "  Bathroom Fittings & Sanitary Ware (taps, mixers, showers, basins, WC "
    "fittings -- fixtures, not drainage) -> GROHE\n"
    "  Solvent Cement & Glue -> OATEY\n"
    "  Drainage / manhole covers / floor traps & cleanouts -> AQUAVERA\n"
    "  Hanging Clamps (pipe clamps/brackets) -> JETFIX\n"
    "  Flexible Hose -> JOMIX\n\n"
)


_QUOTATION_DRAFT_PROMPT = (
    "You are preparing a DRAFT sales quotation from a customer enquiry that has "
    "already been confirmed as a genuine RFQ. A person will review and adjust "
    "your draft before it becomes a real quotation, so it is fine to flag "
    "uncertainty rather than guess -- but do try to find a real match for "
    "every item and the customer before giving up.\n\n"
    + _ITEM_MATCH_RETRY_RULES +
    "Also call lookup_customer with likely candidate names (the company name "
    "from the email signature/letterhead, the sender's own name, words from "
    "the sender's email domain) to see if this enquiry is from a customer "
    "already in the system. If nothing is a confident match, that's fine -- it "
    "will be quoted as a walk-in customer using your best-guess display name "
    "instead.\n\n"
    "When done, call submit_quotation_draft exactly once with your final "
    "answer -- that is the only way to record a result; do not just describe "
    "your answer in text."
)


_ITEM_REMATCH_PROMPT = (
    "A quotation was already auto-created from this RFQ, but the requirement "
    "items listed below had no confident catalog match at the time (their "
    "item_code was left blank). The matching guidance has since improved "
    "(e.g. trade-name synonyms), so re-check each one from scratch using the "
    "rules below -- do not assume the earlier \"no match\" result still holds.\n\n"
    + _ITEM_MATCH_RETRY_RULES +
    "When done, call submit_item_rematch exactly once with one entry per item "
    "listed above (same enquiry_item_index values given to you) -- that is the "
    "only way to record a result; do not just describe your answer in text."
)


_ITEM_BRAND_RECHECK_PROMPT = (
    "A quotation was already auto-created from this RFQ. For each item listed "
    "below you're given the catalog item currently quoted for it (if any), "
    "including its brand and price. Re-check EVERY item against the rules "
    "below and decide whether the current choice is still correct:\n"
    "  - KEEP the current item_code if it's already the best available match.\n"
    "  - REPLACE it with a different item_code if the rules point to a better "
    "one -- most commonly because the customer's preferred brand exists in "
    "the catalog but a different brand was quoted, or a cheaper in-stock "
    "alternative should have been picked under an \"or cheapest\"-style "
    "instruction.\n"
    "  - Only use \"\" (no match) for an item that genuinely has no reasonable "
    "catalog match at all -- never turn an already-matched item into "
    "unmatched just because a marginally different option exists.\n\n"
    + _ITEM_MATCH_RETRY_RULES +
    "When done, call submit_item_rematch exactly once with one entry per item "
    "listed above (same enquiry_item_index values given to you), setting "
    "item_code to the SAME code shown as currently quoted if you're keeping "
    "it, a DIFFERENT code if you found a better match, or \"\" only for a "
    "genuinely unmatchable item -- that is the only way to record a result; "
    "do not just describe your answer in text."
)


def _build_draft_content(tracked_email, enquiry_items) -> list:
    lines = [
        f"From: {tracked_email.sender_name} <{tracked_email.sender}>",
        f"Subject: {tracked_email.subject}",
        f"Body:\n{tracked_email.body_text}",
        "",
        "Requirement items (index | description | category | brand | quantity | unit | notes):",
    ]
    for i, item in enumerate(enquiry_items):
        lines.append(
            f"{i} | {item.description} | {item.category} | {item.brand} | "
            f"{item.quantity} | {item.unit} | {item.notes}"
        )
    return [{"type": "text", "text": "\n".join(lines)}]


WALKIN_CUSTOMER_NAME = 'DEBIT CUSTOMER ( CASH )'

# Set as the initial remarks on every agent-created quotation (see
# draft_quotation below) -- used as a safety check elsewhere (this file,
# rematch_unmatched_items, recheck_item_brand_matches) to confirm a
# quotation hasn't been edited by a human since: editing a quotation always
# overwrites remarks with whatever was submitted, so unedited remarks still
# start with this exact text.
AUTO_DRAFT_MARKER = 'Auto-drafted by the email tracking agent'


def _resolve_price(pricing_customer, item):
    """pricing_customer is None for walk-in quotes -- CustomerPrice is
    specific to a real customer, so it's only consulted when we matched one."""
    if pricing_customer:
        from so.models import CustomerPrice
        custom = CustomerPrice.objects.filter(customer=pricing_customer, item=item).first()
        if custom:
            return custom.custom_price
    return item.item_price


def _parse_quantity(raw) -> int:
    try:
        qty = int(round(float(str(raw).strip())))
        return qty if qty > 0 else 1
    except (TypeError, ValueError, AttributeError):
        return 1


# Matches a requested unit that is a LENGTH measure rather than a piece count
# -- customers commonly write pipe/cable/hose requirements in running meters
# ("24 MTR", "50m") even though we sell that item as fixed-length pieces or
# rolls, e.g. catalog item "UPVC PIPE 6X6MTR" is a 6-meter pipe per piece.
# Treating "24 MTR" as 24 pcs (as the code used to, since it only ever parsed
# the raw number and ignored the unit) massively overcharges/over-quotes.
_LENGTH_UNIT_RE = re.compile(
    r'^(m|mtr|mtrs|mt|mts|meter|meters|metre|metres|rmt|rm|lm|lmt|running\s*met(?:er|re)s?)\.?$',
    re.IGNORECASE,
)
# Catalog descriptions encode the per-piece/per-roll length as a number
# immediately before "MTR", e.g. "PIPE 6X6MTR" (6m/pc), "PEX PIPE 25X50MTR
# ROLL" (50m/roll), "COSMO PIPE 4X5.8 MTR" (5.8m/pc). The LAST such number in
# the description is consistently the length figure (the first is a
# diameter/size), so take that one.
_CATALOG_LENGTH_RE = re.compile(r'(\d+(?:\.\d+)?)\s*MTR', re.IGNORECASE)


def _is_length_unit(raw_unit) -> bool:
    token = (raw_unit or '').strip().lower().rstrip('.')
    return bool(token) and bool(_LENGTH_UNIT_RE.match(token))


def _catalog_length_per_unit(matched_item):
    """Best-effort per-piece/per-roll length (in meters) parsed from the
    catalog item's own description. None if it can't be determined -- callers
    must not guess a conversion factor in that case."""
    if not matched_item or not matched_item.item_description:
        return None
    matches = _CATALOG_LENGTH_RE.findall(matched_item.item_description)
    if not matches:
        return None
    try:
        length = float(matches[-1])
        return length if length > 0 else None
    except ValueError:
        return None


def _resolve_quantity(raw_quantity, raw_unit, matched_item):
    """Converts a requirement line's raw quantity into the whole-unit count
    to actually quote. For an ordinary count unit (pcs/nos/each/...), this is
    just the parsed number, same as before. For a LENGTH unit (meters etc.),
    it instead divides the requested length by the catalog item's per-unit
    length (parsed from its description) and rounds up -- because you can't
    buy half a pipe. When that per-unit length can't be determined, the raw
    number is used as a last resort but flagged, rather than silently
    quoting the wrong quantity.

    Returns (quantity: int, note: str) -- note is '' unless a human should
    double check this line (a conversion was applied, or should have been
    but the catalog description didn't have a parseable length).
    """
    base_qty = _parse_quantity(raw_quantity)
    if not _is_length_unit(raw_unit):
        return base_qty, ''

    try:
        requested_meters = float(str(raw_quantity).strip())
    except (TypeError, ValueError, AttributeError):
        requested_meters = None

    length_per_unit = _catalog_length_per_unit(matched_item)

    if length_per_unit and requested_meters and requested_meters > 0:
        converted = max(1, math.ceil(requested_meters / length_per_unit))
        note = (
            f"Requested {requested_meters:g} m -- converted to {converted} x "
            f"{length_per_unit:g}m (catalog item length per unit). Please verify."
        )
        return converted, note

    note = (
        f"Requested in METERS ({raw_quantity}) but this item's per-unit length "
        f"isn't stated in its catalog description -- quantity NOT converted "
        f"(quoted {base_qty} as entered). Verify and correct manually."
    )
    return base_qty, note


def _group_items_by_scope(enquiry_items):
    """Groups `enquiry_items` by EnquiryItem.source_attachment, preserving
    first-seen order -- see EnquiryItem.source_attachment and
    classifier._CLASSIFICATION_PROMPT for how that field gets set (e.g. two
    separate BOQ attachments for two different buildings). Items with no
    source_attachment (from the email body, or a single-source enquiry
    where the classifier left it blank) join the FIRST scope's group rather
    than becoming their own near-empty quotation.

    Returns a list of (scope_label, items) tuples -- a single ('',
    all_items) entry when there's at most one distinct non-blank
    source_attachment (the overwhelming common case), so draft_quotation()
    only needs to branch on len() > 1 to detect a genuine multi-scope
    enquiry; the single-scope path behaves identically to before this
    grouping existed."""
    groups = {}
    for item in enquiry_items:
        key = (item.source_attachment or '').strip()
        groups.setdefault(key, []).append(item)

    distinct_scopes = [k for k in groups if k]
    if len(distinct_scopes) <= 1:
        return [('', enquiry_items)]

    blank_items = groups.pop('', [])
    scoped = list(groups.items())
    if blank_items:
        first_label, first_items = scoped[0]
        scoped[0] = (first_label, blank_items + first_items)
    return scoped


def _build_quotation_from_matched_items(tracked_email, enquiry_items, quotation_customer,
                                         customer_display_name, matched_customer, scope_note=''):
    """Creates one real so.Quotation from `enquiry_items` -- each item's
    matched_item/matched_price/matched_unit/match_notes must already be set
    by the caller (draft_quotation's LLM-matching loop runs once for the
    whole email, across every scope, before this is called per scope).
    Shared by draft_quotation's single-scope path and its multi-scope path
    (see _group_items_by_scope) so both build quotations identically.
    Returns (quotation, matched_items_count) -- callers decide what to do
    when matched_items_count is 0 (the original single-scope behavior is to
    delete the quotation and fail instead of keeping an empty one)."""
    remarks = f'{AUTO_DRAFT_MARKER} from RFQ "{tracked_email.subject}" ({tracked_email.sender}).'
    if scope_note:
        remarks += f'\n\n{scope_note}'

    quotation = Quotation.objects.create(
        customer=quotation_customer,
        salesman=quotation_customer.salesman if quotation_customer.salesman_id else None,
        division='JUNAID',
        license_name='JUNAID_SME',
        customer_display_name=customer_display_name or None,
        remarks=remarks,
    )

    quotation_items = []
    unmatched_descriptions = []
    quantity_conversion_notes = []
    total_amount = 0.0
    matched_items_count = 0
    for enquiry_item in enquiry_items:
        if not enquiry_item.matched_item:
            reason = f" -- {enquiry_item.match_notes}" if enquiry_item.match_notes else ""
            unmatched_descriptions.append(f"{enquiry_item.description}{reason}")
            continue
        matched_items_count += 1
        qty, qty_note = _resolve_quantity(enquiry_item.quantity, enquiry_item.unit, enquiry_item.matched_item)
        price = _resolve_price(matched_customer, enquiry_item.matched_item)
        line_total = qty * price
        total_amount += line_total
        quotation_items.append(QuotationItem(
            quotation=quotation,
            item=enquiry_item.matched_item,
            quantity=qty,
            unit=enquiry_item.matched_unit or 'pcs',
            price=price,
            line_total=line_total,
        ))
        update_fields = []
        if enquiry_item.matched_price != price:
            enquiry_item.matched_price = price
            update_fields.append('matched_price')
        if enquiry_item.matched_quantity != qty:
            enquiry_item.matched_quantity = qty
            update_fields.append('matched_quantity')
        if qty_note:
            quantity_conversion_notes.append(f"{enquiry_item.description}: {qty_note}")
            room = _MATCH_NOTES_MAX_LEN - len(enquiry_item.match_notes) - 2
            combined_notes = f"{enquiry_item.match_notes}; {qty_note[:room]}" if room > 0 else enquiry_item.match_notes
            if combined_notes != enquiry_item.match_notes:
                enquiry_item.match_notes = combined_notes
                update_fields.append('match_notes')
        if update_fields:
            enquiry_item.save(update_fields=update_fields)

    QuotationItem.objects.bulk_create(quotation_items)

    if unmatched_descriptions:
        quotation.remarks += (
            "\n\nCould not auto-match against the catalog -- add manually: "
            + "; ".join(unmatched_descriptions)
        )
    if quantity_conversion_notes:
        quotation.remarks += (
            "\n\n⚠ Unit/quantity conversions applied -- please verify before approving: "
            + "; ".join(quantity_conversion_notes)
        )
    quotation.total_amount = total_amount
    quotation.grand_total = total_amount
    quotation.save()

    return quotation, matched_items_count


def draft_quotation(tracked_email) -> None:
    """Best-effort: matches this RFQ's items to the catalog, guesses a
    customer, and automatically creates a real so.Quotation from the
    result -- a person can edit it normally afterward (add/fix items no
    confident match was found for, change customer/salesman, etc.). Never
    raises -- failures are recorded on the draft itself so email tracking is
    never blocked by this."""
    

    enquiry_items = list(tracked_email.items.all())
    draft, _ = QuotationDraft.objects.get_or_create(tracked_email=tracked_email)

    if draft.status == QuotationDraft.STATUS_CONFIRMED and draft.quotation_id:
        # Already turned into a real quotation -- never create a second one
        # for the same email (e.g. if this is accidentally re-run).
        return

    if not enquiry_items:
        draft.status = QuotationDraft.STATUS_FAILED
        draft.error = "No requirement items were extracted from this email to quote."
        draft.save(update_fields=['status', 'error'])
        return

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    content = _build_draft_content(tracked_email, enquiry_items)
    content.insert(0, {"type": "text", "text": _QUOTATION_DRAFT_PROMPT})

    # Unlike classification, this agent needs at least TWO lookup_item_master
    # calls per item (the prompt requires a retry with different wording
    # before giving up on a match), plus a couple of lookup_customer calls --
    # the shared classifier default is far too tight for that, so scale with
    # item count instead.
    max_iterations = max(settings.EMAILAGENT_AGENT_MAX_ITERATIONS, min(60, len(enquiry_items) * 4 + 10))

    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.EMAILAGENT_CLASSIFICATION_MODEL,
            # See classifier.py's classify_email for why this isn't 4096 --
            # a large item list can need well over that just to emit the
            # tool call's output, and doesn't cost more for a small one.
            max_tokens=16000,
            thinking={"type": "disabled"},
            tools=[lookup_item_master, lookup_customer, submit_quotation_draft],
            messages=[{"role": "user", "content": content}],
            max_iterations=max_iterations,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_quotation_draft":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"draft_quotation agent loop API error: {exc!r}")
    except Exception:
        logger.exception("draft_quotation agent loop unexpected failure")

    if captured is None:
        draft.status = QuotationDraft.STATUS_FAILED
        draft.error = "Agent did not finalize a quotation draft (ran out of iterations, refused, or errored)."
        draft.save(update_fields=['status', 'error'])
        return

    

    matched_customer = None
    customer_id = captured.get('matched_customer_id') or 0
    if customer_id:
        matched_customer = Customer.objects.filter(id=customer_id).first()

    items_by_index = {item.get('enquiry_item_index'): item for item in captured.get('items', [])}
    for i, enquiry_item in enumerate(enquiry_items):
        match = items_by_index.get(i)
        if not match:
            continue
        matched_item = None
        zero_stock_item = None
        item_code = (match.get('item_code') or '').strip()
        if item_code:
            candidate = Items.objects.filter(item_code=item_code).first()
            if candidate:
                # total_available_stock (synced from stock.junaidworld.com's
                # total_stock) is the true across-warehouse figure -- fall
                # back to item_stock (DIP warehouse only) if that sync
                # hasn't populated it for this item yet.
                stock = candidate.total_available_stock
                if stock is None:
                    stock = candidate.item_stock
                # Always quote the best catalog match found, even at 0 stock,
                # rather than silently dropping the line -- regardless of
                # whether a specific brand was requested. view_quotation_details
                # (so/views_quotation.py) checks live stock on every quoted item
                # and keeps the quotation Pending -- it is never auto-approved
                # while one is at 0 stock -- so a human confirms availability
                # before approving/sending.
                matched_item = candidate
                if not stock or stock <= 0:
                    zero_stock_item = candidate
        enquiry_item.matched_item = matched_item
        enquiry_item.matched_price = matched_item.item_price if matched_item else None
        enquiry_item.matched_unit = match.get('unit') if match.get('unit') in ('pcs', 'ctn', 'roll') else 'pcs'
        agent_notes = ''
        zero_stock_note = ''
        if zero_stock_item:
            zero_stock_note = (
                f"Catalog match {zero_stock_item.item_code} is currently 0 stock -- quoted anyway; "
                "verify availability before approving."
            )
        else:
            agent_notes = match.get('notes', '') or ('' if matched_item else 'No confident catalog match.')
        default_brand_note = (
            # Python-authored (not the agent's own words) so it's reliably
            # detectable later -- see DEFAULT_BRAND_NOTE_PREFIX -- when the
            # quotation is emailed to the client (so/views_quotation.py).
            f"{DEFAULT_BRAND_NOTE_PREFIX} ({matched_item.item_firm})."
            if matched_item and match.get('default_brand_applied') else ''
        )
        enquiry_item.match_notes = build_match_notes(agent_notes, zero_stock_note, default_brand_note)
        enquiry_item.save(update_fields=['matched_item', 'matched_price', 'matched_unit', 'match_notes'])

    draft.matched_customer = matched_customer
    draft.customer_guess = (captured.get('customer_display_name', '') or (tracked_email.sender_name or tracked_email.sender))[:255]
    draft.reasoning = captured.get('reasoning', '')
    draft.generated_at = timezone.now()

    matched_items_count = sum(1 for item in enquiry_items if item.matched_item)
    if matched_items_count == 0:
        draft.status = QuotationDraft.STATUS_FAILED
        draft.error = (
            "Could not confidently match any of the requirement items to the "
            "catalog -- skipping auto-quotation; review this enquiry manually."
        )
        draft.save()
        return

    quotation_customer = matched_customer
    customer_display_name = ''
    if not quotation_customer:
        quotation_customer = Customer.objects.filter(customer_name=WALKIN_CUSTOMER_NAME).first()
        customer_display_name = draft.customer_guess
        if not quotation_customer:
            draft.status = QuotationDraft.STATUS_FAILED
            draft.error = (
                f"No matched customer, and the walk-in '{WALKIN_CUSTOMER_NAME}' record is "
                "missing -- cannot auto-create a quotation."
            )
            draft.save()
            return

    # Multiple distinct attachment scopes (e.g. two separate BOQs for two
    # different buildings, see EnquiryItem.source_attachment) get drafted as
    # SEPARATE quotations, one per scope, instead of one merged quotation
    # covering unrelated requirements -- the FIRST scope (in the order the
    # classifier listed the attachments) becomes this email's normal,
    # PRIMARY QuotationDraft/quotation exactly as always; every further
    # scope gets its own real Quotation, tracked via an
    # AdditionalQuotationDraft (see its docstring for why that's a separate
    # model rather than a second QuotationDraft). A single-scope enquiry --
    # the overwhelming common case -- behaves identically to before this
    # existed: _group_items_by_scope returns one group holding all items.
    scoped_groups = _group_items_by_scope(enquiry_items)
    is_multi_scope = len(scoped_groups) > 1

    pending_groups = list(scoped_groups)
    primary_label, primary_items = pending_groups.pop(0)
    scope_note = (
        f"Scope: {primary_label} -- this enquiry covered multiple distinct requirement "
        "sources (see the related quotation(s) noted below for the others)."
        if is_multi_scope and primary_label else ''
    )
    quotation, matched_count = _build_quotation_from_matched_items(
        tracked_email, primary_items, quotation_customer, customer_display_name, matched_customer,
        scope_note=scope_note,
    )
    if matched_count == 0 and not is_multi_scope:
        # Single-scope enquiry where NOTHING matched at all -- same as
        # always, an empty quotation from a single, wholly-unmatchable
        # requirement isn't worth keeping.
        quotation.delete()
        draft.status = QuotationDraft.STATUS_FAILED
        draft.error = (
            "Could not confidently match any of the requirement items to the "
            "catalog -- skipping auto-quotation; review this enquiry manually."
        )
        draft.save()
        return
    # For a MULTI-scope enquiry, every scope keeps its quotation even when
    # nothing in it auto-matched (e.g. an attachment of hardware/consumables
    # a plumbing/electrical catalog doesn't carry) -- the customer did ask
    # for that scope specifically, so it still needs a quotation for a human
    # to complete manually, not silence. The existing "needs attention" UI
    # (view_quotation_details.is_incomplete_agent_quotation) already handles
    # a zero-item auto-drafted quotation, listing every requirement line in
    # remarks for manual add -- see _build_quotation_from_matched_items.

    draft.status = QuotationDraft.STATUS_CONFIRMED
    draft.quotation = quotation
    draft.error = ''
    draft.save()

    if not pending_groups:
        return

    sibling_quotations = [quotation]
    for scope_label, scope_items in pending_groups:
        additional = AdditionalQuotationDraft.objects.create(
            tracked_email=tracked_email, source_attachment=scope_label,
        )
        try:
            scope_note = (
                f"Scope: {scope_label} -- see the related quotation(s) noted below for the "
                "enquiry's other scope(s)."
            )
            scope_quotation, scope_matched_count = _build_quotation_from_matched_items(
                tracked_email, scope_items, quotation_customer, customer_display_name, matched_customer,
                scope_note=scope_note,
            )
        except Exception as exc:
            logger.exception(f"Additional-scope quotation drafting failed ({scope_label}) for TrackedEmail {tracked_email.id}")
            additional.error = str(exc)[:2000]
            additional.save(update_fields=['error'])
            continue
        # Kept even when scope_matched_count is 0 -- see the note above.
        additional.quotation = scope_quotation
        additional.reasoning = draft.reasoning
        additional.save(update_fields=['quotation', 'reasoning'])
        sibling_quotations.append(scope_quotation)

    if len(sibling_quotations) > 1:
        numbers = [q.quotation_number for q in sibling_quotations]
        for q in sibling_quotations:
            others = ', '.join(n for n in numbers if n != q.quotation_number)
            q.remarks += f"\n\n🔗 Related quotations from the same enquiry (different scope/attachment): {others}"
            q.save(update_fields=['remarks'])


def rematch_unmatched_items(tracked_email, unmatched_items) -> list:
    """Re-runs catalog matching for a specific subset of an RFQ's requirement
    items that had no confident match when the quotation was first drafted
    (e.g. before a trade-name synonym was added to the matching rules).
    Never touches items outside `unmatched_items`, never re-picks the
    customer, and never raises -- returns [] on any failure so the caller can
    safely skip that email and move on to the next one.

    Returns a list of (enquiry_item, matched_dict) pairs -- one per item in
    `unmatched_items` the agent could now find a catalog match for. Callers
    are responsible for actually applying the match (updating the
    EnquiryItem and creating a QuotationItem)."""
    if not unmatched_items:
        return []

    lines = [
        f"Subject: {tracked_email.subject}",
        "",
        "Items still needing a catalog match "
        "(index | description | category | brand | quantity | unit | notes):",
    ]
    for i, item in enumerate(unmatched_items):
        lines.append(
            f"{i} | {item.description} | {item.category} | {item.brand} | "
            f"{item.quantity} | {item.unit} | {item.notes}"
        )
    content = [
        {"type": "text", "text": _ITEM_REMATCH_PROMPT},
        {"type": "text", "text": "\n".join(lines)},
    ]

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    max_iterations = max(settings.EMAILAGENT_AGENT_MAX_ITERATIONS, min(40, len(unmatched_items) * 4 + 6))

    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.EMAILAGENT_CLASSIFICATION_MODEL,
            # See classifier.py's classify_email for why this isn't 4096 --
            # a large item list can need well over that just to emit the
            # tool call's output, and doesn't cost more for a small one.
            max_tokens=16000,
            thinking={"type": "disabled"},
            tools=[lookup_item_master, submit_item_rematch],
            messages=[{"role": "user", "content": content}],
            max_iterations=max_iterations,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_item_rematch":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"rematch_unmatched_items agent loop API error: {exc!r}")
        return []
    except Exception:
        logger.exception("rematch_unmatched_items agent loop unexpected failure")
        return []

    if captured is None:
        return []

    results = []
    for entry in captured.get('items', []):
        idx = entry.get('enquiry_item_index')
        if not isinstance(idx, int) or not (0 <= idx < len(unmatched_items)):
            continue
        results.append((unmatched_items[idx], entry))
    return results


def recheck_item_brand_matches(tracked_email, enquiry_items) -> list:
    """Re-runs catalog matching for ALL of an RFQ's requirement items --
    including ones that already have a match -- against the current
    brand-handling rules (brand aliases, brand preference, cheapest-in-stock
    fallback), so a match picked before those rules existed can be replaced
    with a better one. Never re-picks the customer, and never raises --
    returns [] on any failure so the caller can safely skip that email.

    Returns a list of (enquiry_item, matched_dict) pairs -- one per item the
    agent returned a result for (whether it kept the existing item_code or
    suggested a different one). Callers are responsible for comparing
    against the current match and only acting on genuine changes."""
    if not enquiry_items:
        return []

    lines = [
        f"Subject: {tracked_email.subject}",
        "",
        "Items to re-check (index | description | category | requested brand | "
        "quantity | unit | notes | currently quoted as):",
    ]
    for i, item in enumerate(enquiry_items):
        if item.matched_item_id:
            current = (
                f"{item.matched_item.item_code} | {item.matched_item.item_description} "
                f"| brand={item.matched_item.item_firm} | price={item.matched_price}"
            )
        else:
            current = "none"
        lines.append(
            f"{i} | {item.description} | {item.category} | {item.brand} | "
            f"{item.quantity} | {item.unit} | {item.notes} | {current}"
        )
    content = [
        {"type": "text", "text": _ITEM_BRAND_RECHECK_PROMPT},
        {"type": "text", "text": "\n".join(lines)},
    ]

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    max_iterations = max(settings.EMAILAGENT_AGENT_MAX_ITERATIONS, min(40, len(enquiry_items) * 4 + 6))

    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.EMAILAGENT_CLASSIFICATION_MODEL,
            # See classifier.py's classify_email for why this isn't 4096 --
            # a large item list can need well over that just to emit the
            # tool call's output, and doesn't cost more for a small one.
            max_tokens=16000,
            thinking={"type": "disabled"},
            tools=[lookup_item_master, submit_item_rematch],
            messages=[{"role": "user", "content": content}],
            max_iterations=max_iterations,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_item_rematch":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"recheck_item_brand_matches agent loop API error: {exc!r}")
        return []
    except Exception:
        logger.exception("recheck_item_brand_matches agent loop unexpected failure")
        return []

    if captured is None:
        return []

    results = []
    for entry in captured.get('items', []):
        idx = entry.get('enquiry_item_index')
        if not isinstance(idx, int) or not (0 <= idx < len(enquiry_items)):
            continue
        results.append((enquiry_items[idx], entry))
    return results


_FOLLOWUP_MERGE_PROMPT = (
    "A quotation was already auto-created from the ORIGINAL enquiry below. A FOLLOW-UP email just "
    "arrived in the same thread (same customer, same conversation) -- it is normally a correction or "
    "addition to the original request (e.g. a brand fix, a quantity change, an extra item) rather "
    "than a brand-new enquiry -- this may be the first follow-up, or a later one arriving days after "
    "the previous one; treat it the same way either time. Read the follow-up's own text below to "
    "understand what changed, then re-check EVERY item from the ORIGINAL list against the rules "
    "below in light of that change:\n"
    "  - KEEP the current item_code if the follow-up doesn't affect that item and it's still the "
    "best match.\n"
    "  - REPLACE it with a different item_code if the follow-up's correction (e.g. a new brand) "
    "points to a better one.\n"
    "  - Only use \"\" (no match) for an item that genuinely has no reasonable catalog match at all "
    "-- never turn an already-matched item into unmatched just because a marginally different "
    "option exists.\n\n"
    "Whenever the follow-up asks to change the BRAND on one or more items (whether stated per item "
    "or once for the whole enquiry, e.g. \"please quote in Pilsa instead\"), set that item's \"notes\" "
    "to say so explicitly, e.g. \"Customer requested brand change to Pilsa\" -- a human reviewing the "
    "quotation should immediately understand WHY that line's brand changed, not have to guess from "
    "the item code alone.\n\n"
    + _ITEM_MATCH_RETRY_RULES +
    "When done, call submit_item_rematch exactly once with one entry per ORIGINAL item listed below "
    "(same enquiry_item_index values given to you), setting item_code to the SAME code shown as "
    "currently quoted if you're keeping it, a DIFFERENT code if the follow-up changes what should be "
    "quoted, or \"\" only for a genuinely unmatchable item -- that is the only way to record a "
    "result; do not just describe your answer in text."
)


def merge_followup_into_quotation(tracked_email, original_tracked_email) -> dict:
    """`tracked_email` is a same-thread follow-up to `original_tracked_email`,
    which already has a CONFIRMED QuotationDraft/Quotation. This NEVER
    creates a second quotation for the same enquiry thread -- it either
    updates the original quotation's items in place (only when it's still
    untouched by a HUMAN since the agent created it: no QuotationLog entries,
    remarks still starting with AUTO_DRAFT_MARKER -- same signal
    rematch_unmatched_items / recheck_item_brand_matches use -- and status is
    still 'Pending' or 'Approved'; note that 'Approved' does NOT by itself
    mean a human touched it, since the auto-approval check in
    so/views_quotation.py's view_quotation_details flips Pending -> Approved
    purely mechanically on page view, with no log entry and no remarks
    change, whenever every line is priced above cost and in stock) or, if a
    human actually edited/discount-approved it (or it's in some other
    status entirely, e.g. cancelled), leaves it alone and reports that a
    human needs to reconcile the follow-up manually. When a merge DOES land
    on a quotation that was already Approved, status is reset to 'Pending'
    afterward -- exactly the same re-confirmation gate any other edit goes
    through (view_quotation_details auto-re-approves it if it's still clean,
    otherwise a human reviews it) -- and a human still has to manually click
    "Send Quotation" again to actually notify the client, same as always;
    nothing here ever emails anyone on its own. Never raises -- any failure
    is reported the same way, as something for a human to reconcile.

    Returns a dict:
        merged (bool): True if the quotation was actually updated here.
        quotation: the original enquiry's Quotation, or None if it never
            had one to merge into.
        summary (str) / issues (list[str]): for the caller to log via supervisor.
    """
    original_draft = getattr(original_tracked_email, 'quotation_draft', None)
    if not original_draft or original_draft.status != QuotationDraft.STATUS_CONFIRMED or not original_draft.quotation_id:
        return {
            'merged': False, 'quotation': None, 'summary': '',
            'issues': ["Follow-up in an already-tracked thread, but the original email has no "
                       "confirmed quotation to merge into -- drafted independently instead."],
        }

    quotation = original_draft.quotation
    was_approved = quotation.status == 'Approved'
    untouched = (
        quotation.status in ('Pending', 'Approved')
        and not quotation.logs.exists()
        and (quotation.remarks or '').startswith(AUTO_DRAFT_MARKER)
    )
    if not untouched:
        return {
            'merged': False, 'quotation': quotation,
            'summary': f"Follow-up received for {quotation.quotation_number}, which is already "
                       f"{quotation.status} -- not auto-merged.",
            'issues': [f"This is a follow-up/correction in the same thread as {quotation.quotation_number} "
                       f"(already {quotation.status}, and has been manually edited/discount-reviewed) -- "
                       "needs manual reconciliation; nothing was changed automatically and no duplicate "
                       "quotation was created."],
        }

    original_items = list(original_tracked_email.items.select_related('matched_item').all())
    if not original_items:
        return {
            'merged': False, 'quotation': quotation, 'summary': '',
            'issues': ["Follow-up in an already-tracked thread, but the original email had no "
                       "requirement items to merge against -- drafted independently instead."],
        }

    lines = [
        f"ORIGINAL enquiry subject: {original_tracked_email.subject}",
        "",
        f"FOLLOW-UP email (From: {tracked_email.sender_name} <{tracked_email.sender}>, "
        f"Subject: {tracked_email.subject}):",
        tracked_email.body_text or "(empty body)",
        "",
        "ORIGINAL items to re-check (index | description | category | requested brand | "
        "quantity | unit | notes | currently quoted as):",
    ]
    for i, item in enumerate(original_items):
        if item.matched_item_id:
            current = (
                f"{item.matched_item.item_code} | {item.matched_item.item_description} "
                f"| brand={item.matched_item.item_firm} | price={item.matched_price}"
            )
        else:
            current = "none"
        lines.append(
            f"{i} | {item.description} | {item.category} | {item.brand} | "
            f"{item.quantity} | {item.unit} | {item.notes} | {current}"
        )
    content = [
        {"type": "text", "text": _FOLLOWUP_MERGE_PROMPT},
        {"type": "text", "text": "\n".join(lines)},
    ]

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    max_iterations = max(settings.EMAILAGENT_AGENT_MAX_ITERATIONS, min(40, len(original_items) * 4 + 6))

    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.EMAILAGENT_CLASSIFICATION_MODEL,
            # See classifier.py's classify_email for why this isn't 4096 --
            # a large item list can need well over that just to emit the
            # tool call's output, and doesn't cost more for a small one.
            max_tokens=16000,
            thinking={"type": "disabled"},
            tools=[lookup_item_master, submit_item_rematch],
            messages=[{"role": "user", "content": content}],
            max_iterations=max_iterations,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_item_rematch":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"merge_followup_into_quotation agent loop API error: {exc!r}")
        return {'merged': False, 'quotation': quotation, 'summary': '',
                'issues': ["Follow-up merge failed (API error) -- needs manual reconciliation."]}
    except Exception:
        logger.exception("merge_followup_into_quotation agent loop unexpected failure")
        return {'merged': False, 'quotation': quotation, 'summary': '',
                'issues': ["Follow-up merge failed (unexpected error) -- needs manual reconciliation."]}

    if captured is None:
        return {'merged': False, 'quotation': quotation, 'summary': '',
                'issues': ["Follow-up merge agent did not finalize a result -- needs manual reconciliation."]}

    results = []
    for entry in captured.get('items', []):
        idx = entry.get('enquiry_item_index')
        if not isinstance(idx, int) or not (0 <= idx < len(original_items)):
            continue
        results.append((original_items[idx], entry))

    changed = 0
    total_delta = 0.0
    new_quotation_items = []
    brand_change_notes = []  # human-readable "X changed from brand A to brand B" lines,
    # collected across this merge so they can be surfaced together in the
    # quotation's remarks -- see BRAND_CHANGE_NOTE_PREFIX.
    for enquiry_item, match in results:
        suggested_code = (match.get('item_code') or '').strip()
        previous_item = enquiry_item.matched_item if enquiry_item.matched_item_id else None
        current_code = previous_item.item_code if previous_item else ''
        if suggested_code == current_code or not suggested_code:
            # Unchanged, or the agent found no match -- never let a merge
            # downgrade an existing match to unmatched.
            continue

        candidate = Items.objects.filter(item_code=suggested_code).first()
        if not candidate:
            continue
        stock = candidate.total_available_stock
        if stock is None:
            stock = candidate.item_stock
        # Always quote the best match found, even at 0 stock, rather than
        # skipping it -- regardless of brand (see the matching rule in
        # draft_quotation above). view_quotation_details keeps the quotation
        # Pending while any line is at 0 stock, so a human confirms
        # availability before approving/sending.

        # A genuine brand switch on an already-matched line (as opposed to a
        # first-time match, or a same-brand item-code swap) -- flagged so the
        # reviewer can see WHY the line changed without reopening the email.
        brand_changed = bool(previous_item) and (previous_item.item_firm or '') != (candidate.item_firm or '')
        if brand_changed:
            brand_change_notes.append(
                f"{enquiry_item.description[:60]}: {previous_item.item_firm or 'unspecified'} -> {candidate.item_firm}"
            )

        unit = match.get('unit') if match.get('unit') in ('pcs', 'ctn', 'roll') else 'pcs'
        qty, qty_note = _resolve_quantity(enquiry_item.quantity, enquiry_item.unit, candidate)
        price = _resolve_price(original_draft.matched_customer, candidate)
        line_total = qty * price

        if enquiry_item.matched_item_id:
            existing_qis = list(QuotationItem.objects.filter(
                quotation=quotation, item_id=enquiry_item.matched_item_id,
            ))
            if len(existing_qis) != 1:
                # Can't unambiguously locate the current line -- leave it alone.
                continue
            old_qi = existing_qis[0]
            total_delta += (line_total - old_qi.line_total)
            old_qi.item = candidate
            old_qi.unit = unit
            old_qi.quantity = qty
            old_qi.price = price
            old_qi.line_total = line_total
            old_qi.save(update_fields=['item', 'unit', 'quantity', 'price', 'line_total'])
        else:
            new_quotation_items.append(QuotationItem(
                quotation=quotation, item=candidate, quantity=qty, unit=unit, price=price, line_total=line_total,
            ))
            total_delta += line_total

        zero_stock_note = (
            f"Catalog match {candidate.item_code} is currently 0 stock -- quoted anyway; "
            "verify availability before approving."
        ) if (not stock or stock <= 0) else ''
        default_brand_note = (
            f"{DEFAULT_BRAND_NOTE_PREFIX} ({candidate.item_firm})."
            if match.get('default_brand_applied') else ''
        )
        brand_change_note = (
            f"{BRAND_CHANGE_NOTE_PREFIX} from {previous_item.item_firm or 'unspecified'} to "
            f"{candidate.item_firm} (follow-up received {tracked_email.received_at:%d %b %Y})."
            if brand_changed else ''
        )
        enquiry_item.matched_item = candidate
        enquiry_item.matched_price = price
        enquiry_item.matched_unit = unit
        enquiry_item.matched_quantity = qty
        enquiry_item.match_notes = build_match_notes(
            "; ".join(filter(None, [(match.get('notes', '') or ''), qty_note])),
            zero_stock_note, default_brand_note, brand_change_note,
        )
        enquiry_item.save(update_fields=[
            'matched_item', 'matched_price', 'matched_unit', 'matched_quantity', 'match_notes',
        ])
        changed += 1

    reopened = changed and was_approved
    still_unmatched = []
    with transaction.atomic():
        QuotationItem.objects.bulk_create(new_quotation_items)
        quotation.total_amount = (quotation.total_amount or 0.0) + total_delta
        quotation.grand_total = (quotation.grand_total or 0.0) + total_delta

        still_unmatched = [
            f"{it.description}{f' -- {it.match_notes}' if it.match_notes else ''}"
            for it in original_tracked_email.items.filter(matched_item__isnull=True)
        ]
        base_remarks = quotation.remarks.split('\n\nCould not auto-match', 1)[0]
        base_remarks = base_remarks.split('\n\n⚠ Reopened after client follow-up', 1)[0]
        if reopened:
            base_remarks += (
                "\n\n⚠ Reopened after client follow-up -- this quotation was already Approved, but a "
                "same-thread reply changed the item/brand on one or more lines, so it was reset to "
                "Pending for re-confirmation before it's sent to the client again."
            )
        if still_unmatched:
            base_remarks += (
                "\n\nCould not auto-match against the catalog -- add manually: " + "; ".join(still_unmatched)
            )
        if brand_change_notes:
            # Appended (not stripped/replaced like the two blocks above) so a
            # customer's brand-change history accumulates across multiple
            # follow-ups over multiple days, instead of only showing the
            # latest one.
            base_remarks += (
                f"\n\n📌 Customer requested brand change (follow-up received "
                f"{tracked_email.received_at:%d %b %Y}) -- " + "; ".join(brand_change_notes)
            )
        quotation.remarks = base_remarks
        update_fields = ['total_amount', 'grand_total', 'remarks']
        if reopened:
            # Reset to the same starting point any other edit leaves a quotation
            # in -- view_quotation_details' existing auto-approval check will
            # re-approve it on next view if every line is still clean (priced
            # above cost, in stock), or leave it Pending for a human otherwise.
            # Sending the revised quotation to the client is still a separate,
            # always-manual "Send Quotation" click -- never automatic.
            quotation.status = 'Pending'
            update_fields.append('status')
        quotation.save(update_fields=update_fields)

    issues = []
    if still_unmatched:
        issues.append(f"{len(still_unmatched)} item(s) still unmatched after merging this follow-up.")
    if reopened:
        issues.append(
            f"{quotation.quotation_number} was already Approved -- reset to Pending after applying this "
            "follow-up's brand/item change; review and re-send to the client once confirmed."
        )
    if brand_change_notes:
        issues.append(f"Customer requested brand change on {quotation.quotation_number}: " + "; ".join(brand_change_notes))

    return {
        'merged': True,
        'quotation': quotation,
        'summary': f"Follow-up merged into {quotation.quotation_number} -- changed {changed} item(s), "
                   f"net AED {total_delta:,.2f}.",
        'issues': issues,
    }
