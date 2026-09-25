"""Read-only lookup tools the classification agent can call before finalizing
its answer. Each tool wraps its own DB query in try/except and returns a
plain string -- Claude sees a graceful message either way, never a crash.
"""
import logging
import re
from datetime import timedelta

from anthropic import beta_tool
from django.utils import timezone

logger = logging.getLogger(__name__)

PROMPT_CACHE_CONTROL = {"type": "ephemeral"}
# Prompt-caching marker, applied by every agent that runs a tool loop (see
# classifier.classify_email and quotation_agent's four loops).
#
# The API is stateless, so each iteration of a loop re-sends the WHOLE
# conversation from the start -- including the agent's static instruction
# block, which for one quotation draft means the same ~3,200-token prompt
# paid for 15-60 times over. Marking that block caches it: the first call
# writes it (1.25x), every later call in the same loop reads it (~0.1x)
# instead of paying full price again.
#
# This changes BILLING ONLY. Claude receives byte-identical input either
# way, so no draft, quotation or classification can come out differently
# because of it -- which is why it is safe to apply to agents that produce
# real financial documents.
#
# Single-sourced here so both agent modules mark their prompts identically
# and the TTL (5 minutes by default, which comfortably outlives a loop whose
# iterations are seconds apart) has one place to change.

# Catalog-verified mm<->inch sizes for our UPVC/mUPVC pipe range. Defined
# here (and imported by emailagent.quotation_agent, which states the same
# table in its own item-matching retry rules) so BOTH agents are always told
# the same figures -- they previously disagreed, with the retry rules quoting
# the generic textbook chart (100mm=4in, 150mm=6in) while this module's
# lookup_item_master docstring carried the real catalog values (110mm=4in,
# 160mm=6in). Sending an agent after "100mm" when the catalog row is 110mm
# is a guaranteed miss, so this must stay single-sourced.
PIPE_SIZE_MM_TO_INCH_TEXT = (
    "mUPVC BS5255 36mm=1-1/4in, 43mm=1-1/2in, 56mm=2in; "
    "UPVC BSEN1329 82mm=3in, 110mm=4in, 160mm=6in, 200mm=8in"
)

def accumulate_cache_usage(totals, message):
    """Adds one tool-loop message's token usage into `totals` (a plain dict,
    created empty by the caller).

    Prompt caching's characteristic failure is SILENT -- a later edit puts
    something variable ahead of the cached block, every request misses, and
    nothing errors; the bill is just quietly higher again. These counters are
    the only ground truth that it is still working, so each agent totals them
    up across its loop and logs one line per run (see log_cache_usage).

    Never raises: observability must not be able to break an agent run, so a
    missing/renamed usage field is swallowed rather than allowed to kill a
    quotation draft."""
    try:
        usage = message.usage
        totals['calls'] = totals.get('calls', 0) + 1
        totals['read'] = totals.get('read', 0) + (usage.cache_read_input_tokens or 0)
        totals['written'] = totals.get('written', 0) + (usage.cache_creation_input_tokens or 0)
        totals['uncached'] = totals.get('uncached', 0) + (usage.input_tokens or 0)
    except Exception:
        logger.debug("accumulate_cache_usage: no usage on message", exc_info=True)
    return totals


def log_cache_usage(agent_name, totals):
    """One line per agent run summarizing what prompt caching actually did.

    A healthy multi-call loop reads far more than it writes. `read` staying
    at 0 across a multi-call run means the cache is not being hit at all --
    something variable now precedes the marked block, or the prefix is below
    the model's minimum cacheable size (1024 tokens on Sonnet 5, but 4096 on
    Haiku 4.5, where a ~3,200-token prompt silently will not cache)."""
    calls = totals.get('calls', 0)
    if not calls:
        return
    read, written, uncached = totals.get('read', 0), totals.get('written', 0), totals.get('uncached', 0)
    verdict = 'cache HIT' if read else ('cache miss' if calls > 1 else 'single call, nothing to reuse')
    logger.info(
        f"{agent_name} prompt cache: {calls} model call(s), {read} tokens read from cache, "
        f"{written} written, {uncached} uncached -- {verdict}."
    )


_ITEM_SEARCH_LIMIT = 20

# How much one matched search word is worth, by how many catalog rows that
# word appears in (its document frequency). Without this every word scored a
# flat +1, so a highly distinctive term ranked no higher than a filler one --
# measured on the live catalog, "yee" (147 rows) and "upvc" (1,303 rows) were
# worth exactly the same, which is how a correct match ends up buried behind
# hundreds of rows that merely share one generic word. Ordered most- to
# least-distinctive; first bucket whose ceiling the frequency fits wins.
_TERM_WEIGHT_BUCKETS = ((25, 24), (100, 16), (300, 8), (800, 3))
_TERM_WEIGHT_COMMON = 1

# Outranks any achievable sum of per-word weights, so a row containing the
# customer's exact phrase, or every one of their words, can never be pushed
# below partial matches no matter how many of those there are.
_FULL_PHRASE_SCORE = 1000
_ALL_TERMS_SCORE = 200


# Joint types of UPVC drainage pipes and fittings. A customer says "push fit"
# or "solvent"; the catalog writes the same thing as a 2-letter code (RR, PF,
# SS) that the >=3-letter word filter in lookup_item_master would otherwise
# throw away, so the joint type never influenced ranking. Each entry is
# (label, regex for the customer's wording in the SEARCH, regex for the
# catalog's wording in a description/brand).
#
# Catalog side uses PostgreSQL word boundaries (\y) so "SS" does not match
# BRASS/GLASS/PRESSURE. Standalone catalog "PE" is deliberately NOT matched:
# in this catalog it means polyethylene (PPR AL/PE, HDPE PE 100); plain-end
# pipes are written "P/E" or "PLAIN END". "Plane end" is a common misspelling
# of plain end and does not occur in the catalog, so it is search-side only.
# Search side ignores "solvent cement/glue/adhesive" -- that is the adhesive
# product, not a joint type.
_JOINT_TYPES = (
    (
        'push-fit (RR / PF / RUBBER RING / PUSH FIT)',
        re.compile(r'(?<![\w/])(?:rr|pf|push[\s-]*fit|rubber[\s-]*ring)(?![\w/])', re.I),
        r'\y(?:RR|PF|RUBBER\s*RING|PUSH[\s-]*FIT)\y',
    ),
    (
        'solvent (SS / SOLVENT / P/E / PLAIN END)',
        re.compile(
            r'(?<![\w/])(?:ss|pe|p/e|glue[\s-]*(?:type|joint|fit)|plain[\s-]*end(?:ed)?|plane[\s-]*end(?:ed)?'
            r'|solvent(?![\s-]*(?:cement|glue|adhesive)))(?![\w/])',
            re.I,
        ),
        r'\y(?:SS|SOLVENT|P/E|PLAIN\s*END(?:ED)?)\y',
    ),
)
# Above the most distinctive per-word weight (24) so the requested joint type
# decides between otherwise-identical fittings (e.g. the RR and SS versions).
_JOINT_TYPE_WEIGHT = 30


def _term_weight(document_frequency):
    if document_frequency <= 0:
        return 0
    for ceiling, weight in _TERM_WEIGHT_BUCKETS:
        if document_frequency <= ceiling:
            return weight
    return _TERM_WEIGHT_COMMON


@beta_tool
def search_similar_enquiries(client_email: str, keywords: str = "") -> str:
    """Look up recently tracked enquiries that might be the same underlying
    request to check whether the current email is a duplicate or a
    "reminder" of one already tracked (a reminder often arrives as a
    brand-new email, not a reply, so it won't otherwise be linked to the
    original by conversation thread). The tracked mailbox usually receives
    enquiries as internal forwards, so the real client's email often only
    appears inside the forwarded body text rather than as the tracked
    email's own sender -- this searches both.

    Args:
        client_email: The real originating client's email address (read it
            from the thread content, e.g. the innermost "From:" line, not
            necessarily whoever forwarded the email to this mailbox).
        keywords: Optional space-separated distinctive words from the
            subject or requested items (e.g. a reference/job number like
            "MR-4325", or a product name) to narrow the match further.
    """
    from django.db.models import Q

    from emailagent.models import EnquiryItem, TrackedEmail

    try:
        cutoff = timezone.now() - timedelta(days=90)
        base_qs = TrackedEmail.objects.filter(received_at__gte=cutoff)
        matches = []

        if client_email.strip():
            sender_matches = base_qs.filter(
                Q(sender__iexact=client_email) | Q(body_text__icontains=client_email)
            ).order_by('-received_at')[:5]
            matches.extend(sender_matches)

        words = [w for w in keywords.strip().split() if len(w) >= 3]
        if words:
            keyword_q = Q()
            for w in words:
                keyword_q |= Q(subject__icontains=w) | Q(body_text__icontains=w)
            keyword_matches = base_qs.filter(keyword_q).order_by('-received_at')[:5]
            for e in keyword_matches:
                if e not in matches:
                    matches.append(e)

            item_matches = (EnquiryItem.objects
                             .filter(tracked_email__received_at__gte=cutoff,
                                     description__icontains=words[0])
                             .select_related('tracked_email')
                             .order_by('-tracked_email__received_at')[:5])
            for item in item_matches:
                if item.tracked_email not in matches:
                    matches.append(item.tracked_email)

        if not matches:
            return "No similar enquiries found in the last 90 days."

        lines = ["Recent enquiries that might be related:"]
        for e in matches[:5]:
            lines.append(
                f"- [tracked_email_id={e.id}] \"{e.subject or '(no subject)'}\" received "
                f"{e.received_at:%Y-%m-%d %H:%M}, status={e.status}, {e.items.count()} item(s)"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"search_similar_enquiries failed: {exc!r}")
        return "Lookup failed -- proceed without this information."


@beta_tool
def lookup_item_master(description: str) -> str:
    """Look up items in the product catalog matching a description, to
    verify or correct a requested item's brand/category and check current
    stock/price before finalizing the extracted item list. Searches both the
    item's description AND its brand, and always ranks results by how many
    of your search words actually match -- catalog descriptions often use
    abbreviations and imperial sizing that won't literally contain an
    enquiry's wording (e.g. "FL/DRAIN 4X4" for a "100mm x 100mm floor
    drain", "R/VALVE" for "reducing valve"), and the brand itself is
    sometimes abbreviated in the description (or missing from it entirely)
    even though it's recorded correctly as the item's brand -- so a short
    list of weak/unrelated results, or no results, does NOT mean nothing
    exists. Before concluding there's no match, retry with a different
    phrasing: just the core noun (e.g. "floor drain" instead of the full
    requirement sentence), the size converted to the other unit system, the
    brand name alone, or a shorter partial word to see the whole category
    before narrowing down. Combining the brand with the core noun in one
    search (e.g. "pegler pressure valve") ranks brand-correct items above
    same-category items of other brands.

    Joint type: UPVC drainage pipes/fittings come push-fit or solvent-weld.
    Put the customer's joint-type word in the search -- "push fit", "rubber
    ring", "RR", "PF" (catalog: RR / PF / RUBBER RING / PUSH FIT) or
    "solvent", "SS", "glue type", "plain end", "plane end", "PE" (catalog:
    SS / SOLVENT / P/E / PLAIN END) -- and rows of that joint type are ranked
    above the same item in the other joint type. This works even though these
    codes are under 3 letters, which are otherwise ignored.

    mm<->inch conversion for our UPVC/mUPVC pipe range specifically is NOT
    the generic mm/25.4 formula (or a textbook NPS chart) -- it's each
    product line's own actual stated size, confirmed against the real
    catalog: mUPVC BS5255 36mm=1-1/4in, 43mm=1-1/2in, 56mm=2in; UPVC
    BSEN1329 82mm=3in, 110mm=4in, 160mm=6in, 200mm=8in. Also note BSEN1329
    rows are almost always written under the BARE inch number with no unit
    at all (e.g. "BSEN1329 4X5.8 MTR" IS the 110mm/4in pipe) -- so an
    enquiry stated in mm for one of these needs the inch number searched
    for instead, not the mm figure itself, which mostly doesn't appear in
    the catalog text at all.

    Args:
        description: The item description (or part of it) to search for,
            e.g. "UPVC pipe" or "water heater". Include a brand name in this
            string (e.g. "pegler pressure valve") to prioritize that brand.
    """
    from django.db.models import Case, Count, IntegerField, Q, Value, When

    from so.models import Items

    try:
        description = description.strip()
        # Joint-type words (push fit / RR / PF, solvent / SS / plain end ...)
        # are lifted out of the plain word list and matched against every
        # catalog spelling of that joint type instead -- see _JOINT_TYPES.
        # A search with none of them is left exactly as written.
        joint_labels, joint_hits = [], []
        word_source = description
        for label, search_rx, catalog_rx in _JOINT_TYPES:
            if search_rx.search(word_source):
                word_source = search_rx.sub(' ', word_source)
                joint_labels.append(label)
                joint_hits.append(Q(item_description__iregex=catalog_rx) | Q(item_firm__iregex=catalog_rx))
        # De-duplicated, order-preserving: a word repeated in the enquiry
        # ("4 inch x 3 inch") must not be scored twice for the same hit.
        words = list(dict.fromkeys(w for w in word_source.split() if len(w) >= 3))

        # Rank EVERY candidate by how many search terms it actually matches
        # -- across both description and brand -- rather than just taking
        # the first N rows the DB happens to return for the full phrase.
        # Without this, a query that already gets 3+ loose hits (e.g. a
        # common brand name that also appears on hundreds of unrelated
        # parts) can bury the one item that matches the FULL request behind
        # arbitrary database order.
        full_phrase_hit = Q(item_description__icontains=description) | Q(item_firm__icontains=description)
        word_hits = {
            w: Q(item_description__icontains=w) | Q(item_firm__icontains=w)
            for w in words
        }

        # How distinctive is each word? One round trip counts them all, so
        # a rare, identifying term (e.g. "yee") can outweigh a filler one
        # (e.g. "upvc") instead of both being worth a flat +1 -- see
        # _term_weight. Cheap relative to the search itself, and skipped
        # entirely for a phrase-only search with no scoreable words.
        frequencies = {}
        if word_hits:
            frequencies = Items.objects.aggregate(**{
                f"df_{i}": Count(Case(When(hit, then=Value(1)), output_field=IntegerField()))
                for i, hit in enumerate(word_hits.values())
            })

        relevance = Case(
            When(full_phrase_hit, then=Value(_FULL_PHRASE_SCORE)),
            default=Value(0), output_field=IntegerField(),
        )
        combined_q = full_phrase_hit
        all_terms_hit = None
        for i, word_hit in enumerate(word_hits.values()):
            combined_q |= word_hit
            all_terms_hit = word_hit if all_terms_hit is None else (all_terms_hit & word_hit)
            weight = _term_weight(frequencies.get(f"df_{i}", 0))
            relevance = relevance + Case(
                When(word_hit, then=Value(weight)),
                default=Value(0), output_field=IntegerField(),
            )
        # The requested joint type counts as one more search term: it is
        # scored, can pull a row into the candidates, and a row must carry it
        # to reach the "matches ALL your search terms" tier below.
        for joint_hit in joint_hits:
            combined_q |= joint_hit
            all_terms_hit = joint_hit if all_terms_hit is None else (all_terms_hit & joint_hit)
            relevance = relevance + Case(
                When(joint_hit, then=Value(_JOINT_TYPE_WEIGHT)),
                default=Value(0), output_field=IntegerField(),
            )
        # A row carrying EVERY search word is a categorically better answer
        # than one carrying some of them, however many weak partial matches
        # exist -- scored as its own tier rather than left to outweigh them
        # by luck of the summed total.
        if all_terms_hit is not None:
            relevance = relevance + Case(
                When(all_terms_hit, then=Value(_ALL_TERMS_SCORE)),
                default=Value(0), output_field=IntegerField(),
            )

        # Annotated separately from the score so the per-line marker below
        # states a fact rather than inferring one from a total that enough
        # highly-weighted partial hits could also reach.
        covers_all = Case(
            When(all_terms_hit, then=Value(1)), default=Value(0), output_field=IntegerField(),
        ) if all_terms_hit is not None else Value(0, output_field=IntegerField())

        ranked = (Items.objects.filter(combined_q)
                  .annotate(_relevance=relevance, _covers_all=covers_all)
                  .order_by('-_relevance', 'item_code'))
        total_matches = ranked.count()
        matches = list(ranked[:_ITEM_SEARCH_LIMIT])

        if not matches:
            return (
                f"No catalog items found matching '{description}' (checked both description and "
                "brand), even with a per-word search -- try a different phrasing (shorter, "
                "converted units, brand alone, or just the item category) before concluding "
                "nothing matches."
            )

        # Always state the true total and how arbitrary the cut-off was.
        # Showing a silently-truncated list is how a real match gets missed:
        # the reader cannot tell "these are all that exist" from "these are
        # 20 of 1,982", and so wrongly concludes an item isn't stocked.
        top_score = matches[0]._relevance
        tied_at_top = sum(1 for it in matches if it._relevance == top_score)
        header = [f"Catalog items matching '{description}' (best matches first)."]
        if total_matches > len(matches):
            header.append(
                f"Showing the top {len(matches)} of {total_matches} total matches "
                f"({total_matches - len(matches)} not shown). If none of these is right, that does "
                "NOT mean the item doesn't exist -- search again with more distinctive wording "
                "(the core noun alone, a size, or the brand alone) to bring the rest into range."
            )
        else:
            header.append(f"These are ALL {total_matches} matches -- the list is complete.")
        if joint_labels:
            header.append(
                "Joint type requested in your search: " + "; ".join(joint_labels) + ". Rows of that joint "
                "type rank above the same item in the other joint type. Only UPVC pipes/fittings use "
                "these joint types -- ignore RR on cable, SS meaning stainless steel, PE meaning polyethylene."
            )
        if tied_at_top == len(matches) and total_matches > len(matches):
            header.append(
                f"WARNING: every item shown is tied at the same relevance score, so this ordering "
                "is arbitrary and the best match may well be among the ones not shown -- your "
                "search terms did not distinguish anything. Search again with different wording."
            )

        lines = header
        for it in matches:
            marker = " <-- matches ALL your search terms" if len(words) + len(joint_hits) > 1 and it._covers_all else ""
            lines.append(
                f"- {it.item_code} | {it.item_description} | brand={it.item_firm} "
                f"| stock={it.item_stock} | price={it.item_price}{marker}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"lookup_item_master failed: {exc!r}")
        return "Lookup failed -- proceed without this information."


@beta_tool
def lookup_customer(name: str) -> str:
    """Look up existing customers by name, to check whether an enquiry is
    from a customer already in the system before falling back to a
    walk-in/CASH quotation.

    Args:
        name: Company or contact name to search for -- try the company name
            from the email signature/letterhead first, then the sender's
            own name if that doesn't match anything.
    """
    from so.models import Customer

    try:
        # Ordered so the same name always returns the same customers: this
        # slice previously had no ordering at all, leaving which 10 of the
        # 4,600+ customers came back down to database row order -- and this
        # choice decides who a quotation is billed to (and therefore which
        # CustomerPrice and salesman apply), so it must not vary run to run.
        found = Customer.objects.filter(customer_name__icontains=name).order_by('customer_name')
        total = found.count()
        matches = list(found[:10])
        if not matches:
            return f"No existing customer found matching '{name}'."

        lines = [f"Customers matching '{name}':"]
        for c in matches:
            lines.append(f"- id={c.id} | {c.customer_code} | {c.customer_name}")
        if total > len(matches):
            lines.append(
                f"({total} customers match '{name}' -- only the first {len(matches)} are listed. "
                "If none is the right one, search a longer/more specific part of the company name "
                "rather than picking from this partial list.)"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"lookup_customer failed: {exc!r}")
        return "Lookup failed -- proceed without this information."
