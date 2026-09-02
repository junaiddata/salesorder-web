"""Read-only lookup tools the classification agent can call before finalizing
its answer. Each tool wraps its own DB query in try/except and returns a
plain string -- Claude sees a graceful message either way, never a crash.
"""
import logging
from datetime import timedelta

from anthropic import beta_tool
from django.utils import timezone

logger = logging.getLogger(__name__)


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
                f"- \"{e.subject or '(no subject)'}\" received {e.received_at:%Y-%m-%d %H:%M}, "
                f"status={e.status}, {e.items.count()} item(s)"
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
    requirement sentence), the size converted to the other unit system
    (mm<->inch, e.g. 100mm=4in, 150mm=6in, 200mm=8in, 25mm=1in), the brand
    name alone, or a shorter partial word to see the whole category before
    narrowing down. Combining the brand with the core noun in one search
    (e.g. "pegler pressure valve") ranks brand-correct items above
    same-category items of other brands.

    Args:
        description: The item description (or part of it) to search for,
            e.g. "UPVC pipe" or "water heater". Include a brand name in this
            string (e.g. "pegler pressure valve") to prioritize that brand.
    """
    from django.db.models import Case, IntegerField, Q, Value, When

    from so.models import Items

    try:
        description = description.strip()
        words = [w for w in description.split() if len(w) >= 3]

        # Rank EVERY candidate by how many search terms it actually matches
        # -- across both description and brand -- rather than just taking
        # the first N rows the DB happens to return for the full phrase.
        # Without this, a query that already gets 3+ loose hits (e.g. a
        # common brand name that also appears on hundreds of unrelated
        # parts) can bury the one item that matches the FULL request behind
        # arbitrary database order.
        full_phrase_hit = Q(item_description__icontains=description) | Q(item_firm__icontains=description)
        relevance = Case(When(full_phrase_hit, then=Value(100)), default=Value(0), output_field=IntegerField())
        combined_q = full_phrase_hit
        for w in words:
            word_hit = Q(item_description__icontains=w) | Q(item_firm__icontains=w)
            combined_q |= word_hit
            relevance = relevance + Case(When(word_hit, then=Value(1)), default=Value(0), output_field=IntegerField())

        matches = list(
            Items.objects.filter(combined_q)
            .annotate(_relevance=relevance)
            .order_by('-_relevance', 'item_code')[:20]
        )

        if not matches:
            return (
                f"No catalog items found matching '{description}' (checked both description and "
                "brand), even with a per-word search -- try a different phrasing (shorter, "
                "converted units, brand alone, or just the item category) before concluding "
                "nothing matches."
            )

        lines = [f"Catalog items matching '{description}' (best matches first):"]
        for it in matches:
            lines.append(
                f"- {it.item_code} | {it.item_description} | brand={it.item_firm} "
                f"| stock={it.item_stock} | price={it.item_price}"
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
        matches = Customer.objects.filter(customer_name__icontains=name)[:10]
        if not matches:
            return f"No existing customer found matching '{name}'."

        lines = [f"Customers matching '{name}':"]
        for c in matches:
            lines.append(f"- id={c.id} | {c.customer_code} | {c.customer_name}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"lookup_customer failed: {exc!r}")
        return "Lookup failed -- proceed without this information."
