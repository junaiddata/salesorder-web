"""Product cost and selling price.

Cost comes from the stock data (the `so.Items` copy of the stock API: UPC code == the product's Model No, `item_cost` == cost
price). Selling price is 2 x cost. Admin may type either value; a typed value is then kept: later stock refreshes never overwrite
it, until admin resets it to automatic.

Only users with the Admin role (or superusers) may see or change cost. This module is the only place that reads `so`, and it
never raises: if the stock data cannot be read, the cost is simply left as it is.
"""
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


log = logging.getLogger('quotation')

PRICE_FACTOR = Decimal('2')
CENT = Decimal('0.01')
MAX_VALUE = Decimal('9999999999.99')


def is_price_admin(user):
    """The Admin role (so.Role) or a superuser."""
    if not (user and user.is_authenticated):
        return False
    if user.is_superuser:
        return True
    role = getattr(user, 'role', None)   # so.Role, one-to-one; missing for users without a role
    return bool(role and role.role == 'Admin')


def auto_price(cost):
    return None if cost is None else (Decimal(cost) * PRICE_FACTOR).quantize(CENT, ROUND_HALF_UP)


def parse_money(text):
    """'1,250.5' -> Decimal('1250.50'); blank -> None; anything invalid or negative -> ValueError."""
    text = (text or '').strip().replace(',', '')
    if not text:
        return None
    try:
        value = Decimal(text).quantize(CENT, ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError('not a number')
    if value < 0 or value > MAX_VALUE:
        raise ValueError('out of range')
    return value


def _to_cost(value):
    try:
        cost = Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return cost if cost > 0 else None   # 0 in the stock data means "no cost known"


def stock_cost(model_no):
    """Cost of the stock item whose UPC code equals this Model No (ignoring case, spaces, dashes and slashes), or None."""
    key = _loose(model_no)
    if not key:
        return None
    try:
        from django.db.models import Value
        from django.db.models.functions import Replace, Upper
        from so.models import Items
        item = (Items.objects.exclude(item_upvc__isnull=True).exclude(item_upvc='')
                .annotate(k=Upper(Replace(Replace(Replace(Replace('item_upvc', Value('-'), Value('')), Value(' '), Value('')),
                                                  Value('/'), Value('')), Value('\\'), Value(''))))
                .filter(k=key).order_by('id').first())
        return _to_cost(item.item_cost) if item else None
    except Exception:
        log.exception('Stock cost lookup failed for %s', model_no)
        return None


def stock_cost_map():
    """{loose Model No: cost} for every stock item that has a UPC code and a cost (one query, for bulk work)."""
    try:
        from so.models import Items
        out = {}
        for upc, cost in Items.objects.exclude(item_upvc__isnull=True).exclude(item_upvc='').order_by('-id').values_list('item_upvc', 'item_cost'):
            c = _to_cost(cost)
            if c is not None:
                out[_loose(upc)] = c   # ordered newest first, so the oldest item ends up winning (same as stock_cost)
        return out
    except Exception:
        log.exception('Stock cost map failed')
        return {}


def _loose(text):
    return re.sub(r'[\s\-/\\]', '', (text or '').upper())


def refresh_cost(product, cost_map=None):
    """Fill the cost from the stock data unless admin set it by hand. Returns True when a stock cost was found."""
    if product.cost_manual:
        return False
    cost = cost_map.get(_loose(product.model_no)) if cost_map is not None else stock_cost(product.model_no)
    if cost is None:
        return False
    product.cost = cost
    return True
