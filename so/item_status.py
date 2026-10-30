"""Which items are Active: those the stock app listed in its latest sync run (see ItemSyncStatus).

Only the places where someone PICKS an item for a new quotation / sales order (and the email agent's item search) use
active_items(); detail pages, PDFs, reports, conversions and edits of existing documents never do, so a document that
already contains an inactive item keeps showing it. Until the sync has recorded a run nothing is hidden (fail open).
"""
from django.db.models import Max

from .models import Items, ItemSyncStatus


def latest_sync_run():
    """Timestamp of the latest recorded stock sync run, or None if no run has been recorded yet."""
    return ItemSyncStatus.objects.aggregate(m=Max('last_seen_at'))['m']


def active_items(qs=None):
    """`qs` (default: every item) limited to the items listed in the latest sync run; unchanged when there is no run yet."""
    if qs is None:
        qs = Items.objects.all()
    run = latest_sync_run()
    return qs if run is None else qs.filter(sync_status__last_seen_at__gte=run)


def active_firms():
    """Brand names (distinct item_firm) that have at least one active item -- for the brand dropdowns."""
    return active_items().values_list('item_firm', flat=True).distinct()


def firms_with_existing(existing_items):
    """Active brands plus the brands of the lines already on a document, so an edit page can still show an old line's
    brand even when every item of that brand has since gone inactive."""
    firms = list(active_firms())
    for item in existing_items:
        firm = getattr(item, 'item_firm', None)
        if firm is not None and firm not in firms:
            firms.append(firm)
    return firms
