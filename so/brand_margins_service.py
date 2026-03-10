"""
Brand Margins Service
Fetches minimum margin % per manufacturer from the brand-margins API and
provides a utility to auto-set sales order approval_status to "MD Approval Required"
when any item's margin falls below the required threshold.

Only operates when current approval_status is "Pending".
"""
import logging
import time
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)

BRAND_MARGINS_API_URL = "https://stock.junaidworld.com/api/brand-margins"
DEFAULT_MARGIN_PCT = 15.0  # fallback when manufacturer not in API
CACHE_TTL_SECONDS = 3600   # 1 hour

# Simple in-process cache (sufficient for sync jobs; no Redis needed)
_cache = {
    'data': None,
    'fetched_at': 0.0,
}


def fetch_brand_margins() -> dict:
    """
    Fetch brand margins from API, caching for CACHE_TTL_SECONDS.
    Returns dict: manufacturer_name (str) -> min_margin_pct (float).
    On network/parse error, returns empty dict and logs a warning.
    """
    now = time.time()
    if _cache['data'] is not None and (now - _cache['fetched_at']) < CACHE_TTL_SECONDS:
        return _cache['data']

    try:
        resp = requests.get(BRAND_MARGINS_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            logger.warning("brand-margins API returned non-dict: %s", type(data))
            return {}
        # Ensure all values are floats
        clean = {str(k): float(v) for k, v in data.items()}
        _cache['data'] = clean
        _cache['fetched_at'] = now
        logger.info("Fetched brand margins for %d manufacturers.", len(clean))
        return clean
    except Exception as exc:
        logger.warning("Failed to fetch brand-margins API: %s. Skipping margin check.", exc)
        return {}


def _get_required_margin(manufacture: str, brand_margins: dict) -> float:
    """
    Look up required margin % for the given manufacturer name.
    Falls back to '- No Manufacturer -' entry if present, then DEFAULT_MARGIN_PCT.
    """
    if not manufacture:
        return brand_margins.get('- No Manufacturer -', DEFAULT_MARGIN_PCT)
    result = brand_margins.get(manufacture)
    if result is not None:
        return float(result)
    # Fallback
    return brand_margins.get('- No Manufacturer -', DEFAULT_MARGIN_PCT)


def check_salesorder_margin(salesorder, brand_margins: dict) -> bool:
    """
    Check all items in a SAPSalesorder against the required brand margins.

    Only acts when approval_status is currently "Pending".
    Never overrides Approved, Rejected, DO Completed, Partial DO,
    Trade License Expired, or MD Approval Required.

    Returns True if approval_status was changed to 'MD Approval Required'.
    """
    # Guard: only change status when Pending
    if salesorder.approval_status != 'Pending':
        return False

    if not brand_margins:
        # API unavailable – skip silently
        return False

    from .models import Items

    items = list(salesorder.items.all())
    if not items:
        return False

    # Batch-load item costs and manufacturers from Items master
    item_codes = [it.item_no for it in items if it.item_no]
    item_master_map = {}
    if item_codes:
        for master in Items.objects.filter(item_code__in=item_codes).only(
            'item_code', 'item_cost', 'item_firm'
        ):
            item_master_map[master.item_code] = {
                'item_cost': float(master.item_cost or 0),
                'item_firm': master.item_firm or '',
            }

    below_margin = False
    for item in items:
        qty = item.quantity or Decimal('0')
        row_total = item.row_total or Decimal('0')

        if qty and qty != 0:
            unit_price = float(row_total / qty)
        else:
            unit_price = 0.0

        if unit_price <= 0:
            # Cannot compute margin; skip this item (treat as passing)
            continue

        master_data = item_master_map.get(item.item_no or '', {})
        cost = master_data.get('item_cost', 0.0)

        margin_pct = ((unit_price - cost) / unit_price) * 100.0

        # Manufacturer: prefer item.manufacture (from SAP), fallback to Items.item_firm
        manufacture = (item.manufacture or '').strip() or master_data.get('item_firm', '')
        required = _get_required_margin(manufacture, brand_margins)

        if margin_pct < required:
            logger.info(
                "SO %s item %s: margin %.2f%% < required %.2f%% (manufacturer: %s).",
                salesorder.so_number,
                item.item_no or item.description,
                margin_pct,
                required,
                manufacture or '—',
            )
            below_margin = True
            break  # One failing item is enough; no need to check the rest

    if below_margin:
        salesorder.approval_status = 'MD Approval Required'
        salesorder.save(update_fields=['approval_status'])
        logger.info("SO %s approval_status set to 'MD Approval Required'.", salesorder.so_number)
        return True

    return False


def run_margin_check_for_queryset(qs) -> int:
    """
    Run margin check for a queryset of SAPSalesorder objects.
    Fetches brand_margins once and processes all qualifying orders.
    Only processes orders with approval_status='Pending'.
    Returns the number of orders updated.
    """
    brand_margins = fetch_brand_margins()
    if not brand_margins:
        logger.warning("Skipping margin check: brand_margins API returned empty or failed.")
        return 0

    # Filter to only Pending SOs to avoid loading all items for non-qualifying orders
    pending_qs = qs.filter(approval_status='Pending')
    updated = 0
    for so in pending_qs.select_related():
        if check_salesorder_margin(so, brand_margins):
            updated += 1

    if updated:
        logger.info("Margin check complete: %d SO(s) set to 'MD Approval Required'.", updated)
    return updated
