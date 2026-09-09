"""Read-only lookup tools the classification agent can call before finalizing
its answer. Each tool wraps its own DB query in try/except and returns a
plain string -- Claude sees a graceful message either way, never a crash.
"""
import logging
from datetime import timedelta

from anthropic import beta_tool
from django.utils import timezone

logger = logging.getLogger(__name__)

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
        # De-duplicated, order-preserving: a word repeated in the enquiry
        # ("4 inch x 3 inch") must not be scored twice for the same hit.
        words = list(dict.fromkeys(w for w in description.split() if len(w) >= 3))

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
        if tied_at_top == len(matches) and total_matches > len(matches):
            header.append(
                f"WARNING: every item shown is tied at the same relevance score, so this ordering "
                "is arbitrary and the best match may well be among the ones not shown -- your "
                "search terms did not distinguish anything. Search again with different wording."
            )

        lines = header
        for it in matches:
            marker = " <-- matches ALL your search terms" if len(words) > 1 and it._covers_all else ""
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
