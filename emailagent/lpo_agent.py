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


_COMPANY_WORD_RE = re.compile(r'[A-Z0-9]+')
# Only spelling variants of the same legal form are ignored. Distinct forms
# (PJSC, FZE, WLL, LTD, ...) stay significant -- "X PJSC" and "X LLC" can be
# different entities, and this match can auto-create a real Sales Order.
_LEGAL_SUFFIX_WORDS = {'LLC', 'CO', 'COMPANY', 'EST', 'ESTABLISHMENT'}


def _normalize_company_name(text):
    """'Menasco Mech. Contracting, (L.L.C.)' and 'MENASCO MECH CONTRACTING LLC'
    both -> 'MENASCO MECH CONTRACTING': uppercased, punctuation dropped, runs
    of single letters rejoined (L.L.C -> LLC), legal-form words removed."""
    words = _COMPANY_WORD_RE.findall((text or '').upper().replace('&', ' AND '))
    merged, run = [], ''
    for word in words:
        if len(word) == 1 and word.isalpha():
            run += word
            continue
        if run:
            merged.append(run)
            run = ''
        merged.append(word)
    if run:
        merged.append(run)
    return ' '.join(w for w in merged if w not in _LEGAL_SUFFIX_WORDS)


def _resolve_single_customer(customer_name):
    """Resolves a stated customer name to exactly one so.Customer, or None
    if it's blank, unresolved, or ambiguous (2+ matches) -- shared by
    find_matching_quotation's customer+item fallback and
    build_sales_order_directly_from_lpo, so an ambiguous customer name is
    never silently guessed at by either path.

    Only when the plain substring lookup finds NOTHING, falls back to an
    exact comparison of _normalize_company_name on both sides, so formatting
    drift ("(L.L.C.)" vs "LLC", stray commas) alone can't drop a match -- still
    requiring exactly one hit. A name that is already ambiguous stays so."""
    from so.models import Customer

    customer_name = (customer_name or '').strip()
    if not customer_name:
        return None
    matches = list(Customer.objects.filter(customer_name__icontains=customer_name)[:2])
    if matches:
        return matches[0] if len(matches) == 1 else None

    normalized = _normalize_company_name(customer_name)
    if not normalized:
        return None
    normalized_matches = [
        customer_id for customer_id, name in Customer.objects.values_list('id', 'customer_name')
        if _normalize_company_name(name) == normalized
    ]
    if len(normalized_matches) != 1:
        return None
    return Customer.objects.get(id=normalized_matches[0])


def _normalize_trn(text):
    """Digits only; '' unless it's a full 15-digit UAE TRN."""
    digits = re.sub(r'\D', '', text or '')
    return digits if len(digits) == 15 else ''


def _resolve_customer_by_trn(trn):
    """Exactly one so.Customer whose vat_number is this TRN, or None. Our own
    TRN (settings.EMAILAGENT_OWN_TRNS) never resolves -- it's printed on every
    LPO sent to us and is wrongly stored on some customer rows."""
    from so.models import Customer

    trn = _normalize_trn(trn)
    if not trn or trn in {_normalize_trn(t) for t in settings.EMAILAGENT_OWN_TRNS}:
        return None
    matches = [c for c in Customer.objects.filter(vat_number__contains=trn)[:5]
               if _normalize_trn(c.vat_number) == trn]
    return matches[0] if len(matches) == 1 else None


def _resolve_lpo_customer(lpo_request):
    """Returns (customer_or_None, how) -- `how` is 'TRN' or 'name' on success,
    or a conflict explanation when the stated TRN and the stated name each
    resolve to a DIFFERENT customer (never guessed between)."""
    by_trn = _resolve_customer_by_trn(lpo_request.customer_trn_stated)
    by_name = _resolve_single_customer(lpo_request.customer_name_stated)
    if by_trn and by_name and by_trn.id != by_name.id:
        return None, (
            f"TRN {lpo_request.customer_trn_stated} belongs to {by_trn.customer_name} but the name "
            f"resolves to {by_name.customer_name}"
        )
    if by_trn:
        return by_trn, 'TRN'
    if by_name:
        return by_name, 'name'
    return None, ''


def _read_lpo_document_for_customer(attachment):
    """(text, [(bytes, media_type)]) for the stored LPO attachment: a PDF's
    text plus page 1 ALWAYS rendered as an image (the letterhead), or an image
    attachment as-is. ('', []) when there's nothing readable."""
    from . import classifier

    if not attachment or not attachment.file:
        return '', []
    content_type = attachment.content_type or ''
    if content_type != 'application/pdf' and not content_type.startswith('image/'):
        return '', []
    attachment.file.open('rb')
    try:
        data = attachment.file.read()
    finally:
        attachment.file.close()
    if not data:
        return '', []

    if content_type == 'application/pdf':
        text = classifier.extract_pdf_text(data, max_chars=8000)
        images = [(png, 'image/png') for png in classifier.render_pdf_pages_as_images(data, max_pages=1)]
        return text, images
    return '', [(data, classifier._image_media_type(content_type))]


def refine_lpo_customer(lpo_request):
    """LPO-only second pass that re-reads the buyer's name/TRN from the LPO
    document itself (classifier.extract_lpo_customer, with page 1 sent as an
    image so a name that exists only in a letterhead logo is still seen).
    Overwrites customer_name_stated only when it returns a name, so the
    classifier's value stays as the fallback. Mutates `lpo_request` without
    saving. Never raises; returns True if anything was updated."""
    from . import classifier

    tracked_email = lpo_request.tracked_email
    try:
        text, images = _read_lpo_document_for_customer(lpo_request.source_attachment)
        if not text.strip() and not images:
            return False
        email_context = (
            f"From: {tracked_email.sender_name} <{tracked_email.sender}>\n"
            f"Subject: {tracked_email.subject}\n"
            f"Body:\n{(tracked_email.body_text or '')[:3000]}"
        )
        extracted = classifier.extract_lpo_customer(text, images, email_context)
    except Exception:
        logger.exception(f"refine_lpo_customer failed for LPORequest {lpo_request.pk}")
        return False

    name = extracted['customer_name'][:255]
    if _normalize_company_name(name).startswith('JUNAID SANITARY'):
        name = ''  # our own name from the supplier block, not the buyer
    trn = extracted['customer_trn'][:50]
    if _normalize_trn(trn) in {_normalize_trn(t) for t in settings.EMAILAGENT_OWN_TRNS}:
        trn = ''

    if name:
        lpo_request.customer_name_stated = name
        lpo_request.customer_name_source = (extracted['name_source'] or 'LPO document')[:100]
    if trn:
        lpo_request.customer_trn_stated = trn
    return bool(name or trn)


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

    customer, _ = _resolve_lpo_customer(lpo_request)
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
# An LPO line with fewer than this many meaningful tokens (see
# _canonical_item_tokens) has too little signal to match against the 10k+ item
# catalog safely -- treated as unmatched rather than guessing.

_ITEM_MATCH_MAX_NARROWING = 3
# How many of a line's words are used to narrow the catalog in the database
# before the exact subset test runs in Python. Three is comfortably enough to
# cut any line down to a scannable set; more just costs extra queries.

_ITEM_MATCH_SCAN_LIMIT = 2000
# Upper bound on rows _match_catalog_item will examine after narrowing, set
# well above any legitimate narrowing (the widest measured on this catalog is
# ~350 rows) so it acts purely as a safety stop. Hitting it means the line was
# too vague to narrow usefully, which is treated as "no confident match"
# rather than matching against an arbitrary subset.


# --- Normalization layer -----------------------------------------------------
# A customer's LPO and our catalog describe the same product in different
# words: "Water Heater 50 Ltr. Horizontal [PRO R] Ariston Italy" is
# "W/H ARISTON PRO 1 R 50 H MT" (firm ARISTON - ITALY) in the item master.
# Comparing those two strings word-for-word can never succeed, so BOTH sides
# are put through _canonical_item_tokens first and only then compared. Every
# rule below rewrites both sides identically -- nothing here is applied to the
# LPO alone, which is what keeps the comparison honest.

_ITEM_PHRASE_SYNONYMS = (
    # (pattern, canonical token). Trade abbreviations only, each one verified
    # against how this catalog actually writes the product. The canonical form
    # is deliberately a single made-up word so it can never collide with a real
    # catalog word, and so the multi-word side ("water heater") and the
    # abbreviated side ("W/H") collapse to the same token.
    (re.compile(r'\bw\s*/\s*h\b'), ' waterheater '),
    (re.compile(r'\bwater\s+heater\b'), ' waterheater '),
    (re.compile(r'\bfl\s*/\s*drain\b'), ' floordrain '),
    (re.compile(r'\bfloor\s+drain\b'), ' floordrain '),
    (re.compile(r'\bw\s*/\s*m\b'), ' wallmounted '),
    (re.compile(r'\bwall\s+mounted\b'), ' wallmounted '),
    # Colour, spelled out on one side and abbreviated on the other.
    (re.compile(r'\bg(?:y|ray)\b'), ' grey '),
    (re.compile(r'\bor\b'), ' orange '),
)

_MIXED_FRACTION_RE = re.compile(r'(\d+)\s*-\s*(\d+)\s*/\s*(\d+)')
_SIMPLE_FRACTION_RE = re.compile(r'(?<![\d.])(\d+)\s*/\s*(\d+)')
# Imperial sizes are ONE size, never the digits they are written with: a
# 2-1/2" valve is 2.5, not a 2 and a 1 and a 2. Collapsing them first is what
# stops a "BRASS GATEVALVE 2-1/2 PEG IMP (1068-2-1/2)" line being satisfied by
# the 1/2" row of the same valve -- every digit it needs happens to appear
# there, so without this it reads as a clean match and bills the wrong size.
# Runs after the phrase synonyms above so "W/H" is already gone, and only ever
# fires between two digits, leaving R/R, FL/DRAIN and 50V/5 untouched.

_ITEM_TOKEN_SEARCH_VARIANTS = {
    # How each canonical token above can literally appear in the catalog text,
    # used ONLY to narrow the queryset in the database (icontains). The exact
    # decision is always made by the token subset test in Python.
    'waterheater': ('w/h', 'water heater', 'waterheater'),
    'floordrain': ('fl/drain', 'floor drain', 'floordrain'),
    'wallmounted': ('w/m', 'wall mounted', 'wallmounted'),
}

_ITEM_NON_SEARCHABLE_TOKENS = frozenset({'horiz', 'vert', 'grey', 'orange'})
# Canonical tokens that must never be used as a database search term, because
# the catalog writes them in a form the canonical spelling doesn't appear in:
# orientation as a bare "H"/"V" (far too common a substring to narrow on) and
# colour as either spelling ("...SS GY" and "...SS GREY" are both in there).
# Searching for one spelling silently drops every row using the other -- which
# is worse than not narrowing at all, since it can leave a single survivor that
# then looks like a unique match. They are still fully enforced by the subset
# test below; they just don't get to choose the candidates.

_ITEM_ORIENTATION_TOKENS = {
    # Orientation is written out on an LPO and abbreviated to a single letter
    # in the catalog ("50 H MT", "50V/5"), so both collapse to one token. This
    # is what stops a horizontal LPO line matching the vertical model of the
    # same heater -- previously the letter was dropped entirely as too short.
    'horizontal': 'horiz', 'horiz': 'horiz', 'hor': 'horiz', 'h': 'horiz',
    'vertical': 'vert', 'vert': 'vert', 'ver': 'vert', 'v': 'vert',
}

_ITEM_NOISE_TOKENS = frozenset({
    # Words that carry no identifying information, dropped from both sides.
    # Units of measure are safe to drop because the NUMBER they belong to is
    # kept and still has to match ("50 LTR" -> {50}, "110MM" -> {110}); what
    # they fix is one side spelling the unit and the other not.
    'ltr', 'ltrs', 'litre', 'litres', 'liter', 'liters', 'l',
    'mm', 'cm', 'inch', 'inches', 'in', 'x',
    'nos', 'no', 'pcs', 'pc', 'piece', 'pieces', 'qty', 'quantity',
    'and', 'with', 'the', 'of', 'for', 'a', 'an', 'as', 'per', 'approx',
})

_NUMBER_LETTER_RE = re.compile(r'(\d)\s*([a-z])')
_LETTER_NUMBER_RE = re.compile(r'([a-z])\s*(\d)')
_ITEM_TOKEN_RE = re.compile(r'[a-z]+|\d+(?:\.\d+)?')


def _canonical_number(token):
    """'50.00' -> '50', '1.20' -> '1.2', so the same size written two ways is
    one token. Left untouched if it isn't a plain small number."""
    try:
        value = float(token)
    except (TypeError, ValueError):
        return token
    if not (0 < abs(value) < 1e6):
        return token
    return f"{value:g}"


def _expand_fraction(match):
    """'2-1/2' -> ' 2.5 ', '3/4' -> ' 0.75 '. A nonsense denominator is left
    exactly as written rather than raising -- it simply won't match anything."""
    groups = match.groups()
    whole, numerator, denominator = ('0',) * (3 - len(groups)) + groups
    if int(denominator) == 0:
        return match.group(0)
    return f" {int(whole) + int(numerator) / int(denominator):g} "


def _canonical_item_tokens(text):
    """The shared vocabulary both an LPO line and a catalog row are reduced to
    before they are compared: trade synonyms collapsed, units and filler
    dropped, glued size/letter codes split apart ("50V/5" -> 50, v, 5) and
    orientation letters spelled out.

    Note this deliberately KEEPS numbers and single letters, which the old
    3+ character rule threw away -- on this catalog "50", "R" and "H" are
    precisely what separate one Ariston water heater from the next, so
    ignoring them risked ordering the wrong model, not just missing one."""
    text = (text or '').lower()
    for pattern, replacement in _ITEM_PHRASE_SYNONYMS:
        text = pattern.sub(replacement, text)
    text = _MIXED_FRACTION_RE.sub(_expand_fraction, text)
    text = _SIMPLE_FRACTION_RE.sub(_expand_fraction, text)
    # "50V/5" / "1.2KW" / "R50" are one word to the tokenizer but two facts to
    # a reader -- split so they compare against a catalog that spaces them out.
    text = _NUMBER_LETTER_RE.sub(r'\1 \2', text)
    text = _LETTER_NUMBER_RE.sub(r'\1 \2', text)

    tokens = set()
    for raw in _ITEM_TOKEN_RE.findall(text):
        if raw in _ITEM_NOISE_TOKENS:
            continue
        tokens.add(_ITEM_ORIENTATION_TOKENS.get(raw) or _canonical_number(raw))
    return tokens


def _catalog_row_tokens(item):
    """An item's own vocabulary: its description PLUS its firm. The brand and
    the country of origin an LPO names ("Ariston Italy") frequently live only
    in item_firm ("ARISTON - ITALY"), so a description-only comparison drops
    them -- and it is exactly that country word that tells the Italian model
    apart from the Bangladeshi one."""
    return _canonical_item_tokens(item.item_description) | _canonical_item_tokens(item.item_firm)


def _pick_by_literal_wording(candidates, description):
    """Of several candidates, the one that also matches the line EXACTLY as
    written -- word for word, before any normalization (the rule this matcher
    used to apply on its own).

    Normalizing necessarily loosens: "6x4x4" becomes 6 and 4, which a plain
    "6X4" row now also satisfies. Where one candidate still carries the
    customer's own literal wording and the others only survive because of that
    loosening, the literal one is what they asked for."""
    literal_tokens = {t for t in _tokens(description) if len(t) >= 3}
    if not literal_tokens:
        return None
    literal_matches = [
        item for item in candidates
        if literal_tokens <= _tokens(item.item_description)
    ]
    return literal_matches[0] if len(literal_matches) == 1 else None


_PRICE_TIEBREAK_MAX_DIFF = 0.10
_PRICE_TIEBREAK_MIN_RUNNER_UP_DIFF = 0.30
# Price only settles a tie when it settles it OUTRIGHT: the winner within 10%
# of the rate the customer printed on the LPO and every other candidate at
# least 30% away. Anything closer than that is two plausible models, which is a
# question for a human rather than a coin toss on a real Sales Order.


def _pick_by_lpo_price(candidates, price):
    """Of several equally-worded catalog rows, the one whose list price matches
    what the customer is actually ordering at. Returns None unless one wins
    outright (see the thresholds above)."""
    if not price or price <= 0:
        return None

    scored = []
    for item in candidates:
        catalog_price = float(item.item_price or 0)
        if catalog_price <= 0:
            continue
        scored.append((abs(catalog_price - price) / max(catalog_price, price), item))
    if len(scored) < 2:
        return None

    scored.sort(key=lambda pair: pair[0])
    best_diff, best_item = scored[0]
    runner_up_diff = scored[1][0]
    if best_diff <= _PRICE_TIEBREAK_MAX_DIFF and runner_up_diff >= _PRICE_TIEBREAK_MIN_RUNNER_UP_DIFF:
        return best_item
    return None


def _pick_by_customer_history(candidates, customer):
    """Of several equally-worded catalog rows, the one THIS customer has
    actually bought before -- from our own Sales Orders and from the SAP sales
    history. Only decides when exactly one candidate has any history with
    them; two of them having history means their wording genuinely doesn't say
    which, so it goes to a human."""
    if customer is None:
        return None

    from so.models import HistoricalSalesLine, OrderItem

    by_id = {item.id: item for item in candidates}
    bought_ids = set(
        OrderItem.objects.filter(order__customer=customer, item_id__in=by_id)
        .values_list('item_id', flat=True)
    )
    bought_ids.update(
        HistoricalSalesLine.objects.filter(customer=customer, item_id__in=by_id)
        .values_list('item_id', flat=True)
    )
    if len(bought_ids) == 1:
        return by_id[next(iter(bought_ids))]
    return None


def _match_catalog_item(description, price=None, customer=None):
    """Best-effort match of one LPO line's free-text description to a real
    so.Items catalog row -- deliberately conservative, since this feeds a
    real financial document (a Sales Order) with no human review (see
    build_sales_order_directly_from_lpo): every one of the line's own
    meaningful tokens must ALSO appear in the candidate's own tokens -- not
    just a partial overlap -- with both sides first reduced to the shared
    vocabulary of _canonical_item_tokens.

    That subset test is the only thing that admits a candidate. When it admits
    exactly one, that is the match, exactly as before. When it admits several
    -- routinely the same model in two wattages -- the line's own wording has
    genuinely not said which, so one is chosen ONLY if the price the customer
    printed on the LPO, or their own buying history, points at one of them
    outright; otherwise this still returns None and a human picks.

    Returns the matched Items row, or None if the description has too little
    signal, or the match is missing or ambiguous. `price` is the LPO's own
    unit price for this line and `customer` the resolved so.Customer; both are
    optional, and without them this behaves exactly as the subset test alone."""
    hint_tokens = _canonical_item_tokens(description)
    word_tokens = [t for t in hint_tokens if t.isalpha() and len(t) >= 3]
    if len(hint_tokens) < _ITEM_MATCH_MIN_TOKENS or not word_tokens:
        return None

    search_tokens = [t for t in word_tokens if t not in _ITEM_NON_SEARCHABLE_TOKENS]
    if not search_tokens:
        return None

    candidates_qs = _narrowed_catalog_queryset(search_tokens)
    if candidates_qs is None:
        return None

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

    matches = [item for item in candidates if hint_tokens <= _catalog_row_tokens(item)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None

    # Several rows fit the wording equally well -- break the tie only on
    # evidence from this very order, never on a preference of our own (no
    # "pick the one in stock": stock says nothing about which model the
    # customer meant, and getting that wrong bills them for the wrong item).
    # Tried in order and lazily -- the wording is free to re-check, the buying
    # history costs queries, so it is only reached if nothing cheaper decided.
    for tiebreak, resolve in (
        ('the line as literally written', lambda: _pick_by_literal_wording(matches, description)),
        ('LPO price', lambda: _pick_by_lpo_price(matches, price)),
        ("customer's buying history", lambda: _pick_by_customer_history(matches, customer)),
    ):
        picked = resolve()
        if picked is not None:
            logger.info(
                f"_match_catalog_item: {description!r} matched {len(matches)} catalog rows; "
                f"{tiebreak} settled it on {picked.item_code} ({picked.item_description!r})."
            )
            return picked

    logger.debug(
        f"_match_catalog_item: {description!r} fits {len(matches)} catalog rows "
        f"({', '.join(item.item_code for item in matches[:5])}) and nothing settled which "
        "-- leaving unmatched for human review."
    )
    return None


def _narrowed_catalog_queryset(word_tokens):
    """The catalog cut down to the rows worth token-testing in Python -- 10k+
    rows is too many to score one by one. Narrows on the line's longest (most
    distinctive) words, each searched in the spellings the catalog might
    actually use (see _ITEM_TOKEN_SEARCH_VARIANTS), across description AND
    firm.

    Returns None when the line cannot match anything: a word absent from the
    entire catalog can't be in any candidate's token set either, so the subset
    test would reject every row anyway."""
    from django.db.models import Q

    from so.models import Items

    candidates_qs = Items.objects.all()
    narrowed_by = 0
    for token in sorted(word_tokens, key=len, reverse=True):
        if narrowed_by >= _ITEM_MATCH_MAX_NARROWING:
            break
        condition = Q()
        for variant in _ITEM_TOKEN_SEARCH_VARIANTS.get(token, (token,)):
            condition |= Q(item_description__icontains=variant) | Q(item_firm__icontains=variant)
        narrowed_qs = candidates_qs.filter(condition)
        if not narrowed_qs.exists():
            return None
        candidates_qs = narrowed_qs
        narrowed_by += 1
    return candidates_qs if narrowed_by else None


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
      - EVERY line item must resolve to exactly one catalog item (see
        _match_catalog_item -- its wording must fit exactly one row, or fit
        several and be settled outright by this LPO's own price or the
        customer's buying history) -- an order silently missing a line the
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

    customer, matched_by = _resolve_lpo_customer(lpo_request)
    if not customer:
        trn_note = f" (TRN {lpo_request.customer_trn_stated})" if lpo_request.customer_trn_stated else ''
        conflict_note = f": {matched_by}" if matched_by else ''
        return None, (
            f"Customer '{lpo_request.customer_name_stated or '(not stated)'}'{trn_note} could not be resolved "
            f"to exactly one existing customer{conflict_note}."
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
        # Price and customer are passed purely as tie-breakers for a line whose
        # wording fits more than one catalog row (see _match_catalog_item) --
        # they never admit a row the wording itself didn't already fit. Both
        # are known-good here: the gates above have already established a
        # single customer and a stated price on every line.
        catalog_item = _match_catalog_item(lpo_item.description, price=lpo_item.price, customer=customer)
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
        f"stated rates, for customer {customer.customer_name} (matched by {matched_by}). "
        "No quotation was involved."
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
        lpo_request.customer_name_source = 'email classifier' if result.lpo_customer_name else ''
        lpo_request.customer_trn_stated = ''
        lpo_request.referenced_quotation_number = result.lpo_referenced_quotation_number
        lpo_request.delivery_terms = result.lpo_delivery_terms
        lpo_request.payment_terms = result.lpo_payment_terms
        lpo_request.total_amount = _parse_amount(result.lpo_total_amount)
        lpo_request.total_discount = _parse_amount(result.lpo_total_discount)
        lpo_request.total_excl_vat = _parse_amount(result.lpo_total_excl_vat)
        lpo_request.total_vat = _parse_amount(result.lpo_total_vat)
        lpo_request.amount_in_words = result.lpo_amount_in_words
        lpo_request.source_attachment = _resolve_source_attachment(tracked_email, result)
        # The classifier reads the LPO PDF as text only (a name that exists
        # only in the letterhead logo is invisible to it) -- re-read just the
        # buyer from the document with page 1 as an image. Never raises; keeps
        # the classifier's name when it finds nothing.
        refine_lpo_customer(lpo_request)
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
