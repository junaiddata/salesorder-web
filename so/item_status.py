"""Which items are Active: those the stock app listed in its latest sync run (see ItemSyncStatus).

Only the places where someone PICKS an item for a new quotation / sales order (and the email agent's item search) use
active_items(); detail pages, PDFs, reports, conversions and edits of existing documents never do, so a document that
already contains an inactive item keeps showing it. Until the sync has recorded a run nothing is hidden (fail open).
"""
from django.conf import settings
from django.db.models import Max, Q

from .models import AlabamaCatalogBrand, Items, ItemSyncStatus


def latest_sync_run():
    """Timestamp of the latest recorded stock sync run, or None if no run has been recorded yet."""
    return ItemSyncStatus.objects.aggregate(m=Max('last_seen_at'))['m']


def active_items(qs=None):
    """`qs` (default: every item) limited to the items listed in the latest sync run; unchanged when there is no run yet."""
    if qs is None:
        qs = Items.objects.all()
    run = latest_sync_run()
    return qs if run is None else qs.filter(sync_status__last_seen_at__gte=run)


def alabama_brand_limit(user):
    """Lower-cased brands an Alabama user may pick from, or None when there is no limit (Junaid / anonymous users, or an
    Alabama list with no active brand). The list is managed in the admin site (AlabamaCatalogBrand); while it has no
    active brand, settings.ALABAMA_CATALOG_BRANDS applies (empty = every brand)."""
    role = getattr(user, 'role', None)
    is_alabama = (role is not None and role.company == 'Alabama') or 'alabama' in (getattr(user, 'username', '') or '').lower()
    if not is_alabama:
        return None
    allowed = [b.strip().lower() for b in AlabamaCatalogBrand.objects.filter(is_active=True).values_list('brand', flat=True) if b and b.strip()]
    if not allowed:
        allowed = [b.strip().lower() for b in getattr(settings, 'ALABAMA_CATALOG_BRANDS', []) if b and b.strip()]
    return allowed or None


def limit_to_user_brands(qs, user):
    """`qs` of Items limited to the user's brands (see alabama_brand_limit); unchanged when there is no limit."""
    allowed = alabama_brand_limit(user)
    if allowed is None:
        return qs
    cond = Q()
    for b in allowed:
        cond |= Q(item_firm__iexact=b)
    return qs.filter(cond)


def active_firms(user=None):
    """Brand names (distinct item_firm) that have at least one active item -- for the brand dropdowns. Pass `user` to apply
    the Alabama brand list."""
    return limit_to_user_brands(active_items(), user).values_list('item_firm', flat=True).distinct()


def firms_with_existing(existing_items, user=None):
    """Active brands plus the brands of the lines already on a document, so an edit page can still show an old line's
    brand even when every item of that brand has since gone inactive (or is outside the user's brand list)."""
    firms = list(active_firms(user))
    for item in existing_items:
        firm = getattr(item, 'item_firm', None)
        if firm is not None and firm not in firms:
            firms.append(firm)
    return firms
