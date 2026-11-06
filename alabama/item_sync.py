"""Alabama's own item master: brand rules, feed parsing and the 'active' check.

Everything here touches only alabama.AlabamaItem / AlabamaFeedBrand -- never so.Items (Junaid)."""
import re
from decimal import Decimal, InvalidOperation

from django.db.models import Max

from .models import AlabamaFeedBrand, AlabamaItem

JUNK_CODES = {'', '-1', '0'}
MIN_RUN_FRACTION = 0.8   # a run listing under 80% of the previous run is not stamped as a new run

_NON_ALNUM = re.compile(r'[^A-Z0-9]')


def norm_brand(value):
    """Brand compared ignoring case, spaces, hyphens and punctuation ('ARISTON-CHINA' == 'ARISTON - CHINA')."""
    return _NON_ALNUM.sub('', (value or '').upper())


def load_brand_rules():
    """(exact_set, prefix_list, has_rules) from the ACTIVE AlabamaFeedBrand rows (normalised)."""
    exact, prefixes = set(), []
    for brand, match_type in AlabamaFeedBrand.objects.filter(is_active=True).values_list('brand', 'match_type'):
        n = norm_brand(brand)
        if not n:
            continue
        if match_type == AlabamaFeedBrand.MATCH_PREFIX:
            prefixes.append(n)
        else:
            exact.add(n)
    return exact, prefixes, bool(exact or prefixes)


def brand_is_quotable(brand, rules):
    """True when `brand` matches an active rule. With no active rule at all every brand is quotable (same convention as
    the old Alabama catalog brand list: empty = everything)."""
    exact, prefixes, has_rules = rules
    if not has_rules:
        return True
    n = norm_brand(brand)
    if not n:
        return False
    return n in exact or any(n.startswith(p) for p in prefixes)


def _num(value, default=0.0):
    try:
        if value in ('', None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dec(value):
    try:
        return Decimal(str(_num(value))).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return Decimal('0.00')


def parse_feed_item(raw):
    """One feed row -> dict of AlabamaItem field values, or None when the row is junk (code -1 / 0 / blank)."""
    code = str(raw.get('ItemCode') if raw.get('ItemCode') is not None else '').strip()
    if code in JUNK_CODES:
        return None
    stock = _num(raw.get('StockQuantity'))
    return {
        'item_code': code[:50],
        'item_description': (raw.get('Description') or '').strip()[:500],
        'item_upvc': ((raw.get('UpcCode') or '').strip()[:100]) or None,
        'item_firm': (raw.get('ManufacturerName') or '').strip()[:100],
        'item_cost': _num(raw.get('CostPrice')),            # exactly as the API sends it
        'item_price': _num(raw.get('SellingPrice')),
        'item_stock': int(stock),
        'total_available_stock': _dec(stock),
    }


def latest_run():
    return AlabamaItem.objects.filter(source=AlabamaItem.SOURCE_FEED).aggregate(m=Max('last_seen_at'))['m']


def active_alabama_items(qs=None):
    """Items listed in the latest Alabama sync run (fail open: with no run recorded yet nothing is hidden)."""
    if qs is None:
        qs = AlabamaItem.objects.all()
    run = latest_run()
    return qs if run is None else qs.filter(source=AlabamaItem.SOURCE_FEED, last_seen_at__gte=run)


def quotable_items(qs=None):
    """Active items whose brand is on the Alabama brand list -- what a new Alabama quotation may offer."""
    return active_alabama_items(qs).filter(quotable=True)


def refresh_quotable_flags():
    """Re-apply the brand rules to every AlabamaItem (used after the admin edits the brand list). Returns #changed."""
    rules = load_brand_rules()
    to_update = []
    for obj in AlabamaItem.objects.only('id', 'item_firm', 'quotable'):
        q = brand_is_quotable(obj.item_firm, rules)
        if obj.quotable != q:
            obj.quotable = q
            to_update.append(obj)
    for i in range(0, len(to_update), 500):
        AlabamaItem.objects.bulk_update(to_update[i:i + 500], ['quotable'])
    return len(to_update)


def resolve_for_upload(code, description='', brand=''):
    """(alabama_item, legacy_item) for one Excel-upload line.

    Always returns the Alabama item (created as a placeholder in alabama.AlabamaItem when the code is unknown).
    legacy_item is the so.Items link the history tables used before:
      * ALABAMA_OWN_ITEMS on  -> only looked up, never created, so uploads stop adding rows to Junaid's Items table
                                 (None when Junaid has no such code).
      * ALABAMA_OWN_ITEMS off -> exactly the old behaviour (get_or_create in so.Items)."""
    from django.conf import settings
    from so.models import Items

    code = (code or '').strip()
    alabama_item = get_or_create_for_upload(code, description, brand)
    if getattr(settings, 'ALABAMA_OWN_ITEMS', False):
        legacy = Items.objects.filter(item_code=code).first()
    else:
        legacy, _ = Items.objects.get_or_create(
            item_code=code,
            defaults={'item_description': description or code, 'item_firm': brand or ''},
        )
    return alabama_item, legacy


def get_or_create_for_upload(code, description='', brand=''):
    """The Alabama item for an Excel-upload line: the existing row by code, else a small placeholder (source='upload')
    built from the Excel text -- never a row in Junaid's so.Items. A later feed run replaces a placeholder's data."""
    code = (code or '').strip()
    obj = AlabamaItem.objects.filter(item_code=code).first()
    if obj is not None:
        return obj
    obj, _ = AlabamaItem.objects.get_or_create(
        item_code=code[:50],
        defaults={
            'item_description': (description or code)[:500],
            'item_firm': (brand or '')[:100],
            'source': AlabamaItem.SOURCE_UPLOAD,
            'quotable': False,
        },
    )
    return obj


# ---- switch between the legacy (Junaid so.Items) item link and Alabama's own item master -------------------------
def own_items_enabled():
    from django.conf import settings
    return bool(getattr(settings, 'ALABAMA_OWN_ITEMS', False))


def item_model():
    """The model Alabama item look-ups use: AlabamaItem when ALABAMA_OWN_ITEMS is on, else so.Items (as before)."""
    if own_items_enabled():
        return AlabamaItem
    from so.models import Items
    return Items


def item_fk():
    """Name of the history-line FK to read: 'alabama_item' when ALABAMA_OWN_ITEMS is on, else 'item' (as before)."""
    return 'alabama_item' if own_items_enabled() else 'item'
