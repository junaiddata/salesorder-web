"""Post-Sales-Order-creation stock shortage check for LPO-sourced orders.

Whenever an LPO auto- or manually-converts into a real so.SalesOrder (see
emailagent.lpo_agent.match_and_maybe_convert and
emailagent.views.lpo_request_convert), recompute() re-derives the CURRENT
picture of whether the catalog has enough stock to fulfil ALL currently
pending LPO-sourced sales orders that need each item -- not just the one
just created, since the same item is often needed by several LPOs at once
and procurement needs the combined picture. Updates the single
StockShortageReport row IN PLACE (see emailagent.models.
StockShortageReport -- a singleton, not one row per check) with every
item that's short and a per-LPO breakdown of how much each one needs and
its payment terms -- so an LPO that arrives tomorrow needing an
already-listed item simply lands in that item's existing row on the next
recompute, rather than starting a separate report.

"Available stock" is read from Items.total_available_stock -- NOT a
synchronous HTTP call to the external stock API on every Sales Order
creation. Two things ruled that out:
  1. This app already has an established, consistent answer for what
     "current stock" means: so.api_client.SAPAPIClient's own stock
     lookups and so/purchase_stock_requirement_views.py both read
     Items.total_available_stock/dip_warehouse_stock, which
     so/management/commands/import_items2.py keeps fresh from
     https://stock.junaidworld.com/api/stock on its own schedule.
     (The URL this module originally called --
     https://junaiddataanalyst.pythonanywhere.com/api/stock, copied from
     the older so/management/commands/update_stock.py -- is a different,
     now-404ing endpoint; that mismatch was the bug reported after the
     first version of this module shipped.)
  2. The real endpoint returns the FULL catalog as a large gzip payload
     (import_items2.py's own comments size its read timeout at up to 600s)
     -- fetching that synchronously inside the Sales-Order-creation
     request/agent flow would make every LPO conversion slow, exactly the
     kind of blocking dependency this module's docstring below promises
     never to introduce.

Deliberately best-effort/non-blocking regardless: a failure reading stock
must never prevent the Sales Order itself from being created -- every
entry point catches its own errors, same contract as emailagent.lpo_agent.
"""
import logging

logger = logging.getLogger(__name__)


def get_available_stock(item):
    """so.models.Items.total_available_stock for `item`, or None if it's
    never been synced (see this module's docstring for why that field --
    not a live HTTP call -- is this app's definition of "current stock").
    """
    if item.total_available_stock is None:
        return None
    return float(item.total_available_stock)


#: Cumulative "LPO Sent to Supplier" tracking starts here -- fixed, not a
#: rolling "June of the current year". Purchase orders placed before this
#: date are old enough to already be closed/received in the normal course
#: of business, so counting them would just mix stale, already-fulfilled
#: POs into what's meant to be a running total of what's been sent since
#: this tracking point.
ALREADY_ORDERED_TRACKING_START = "2026-06-01"


def _already_ordered_from_supplier(item_codes):
    """Purchase-order-wise breakdown of quantity placed on purchase orders
    we've sent to OUR OWN suppliers (so.models.SAPPurchaseOrderItem) for
    each of `item_codes` since ALREADY_ORDERED_TRACKING_START -- NOT to be
    confused with a customer's LPO to us (so.models.LPORequest / the "LPO"
    column already on this report), which is the opposite direction. That
    table is synced locally ahead of time by the PC-side
    `sync_purchaseorders_api` management command reading SAP's own Purchase
    Order API (so.api_client.SAPAPIClient) -- so, like get_available_stock
    above, this is a local DB read, never a live HTTP call, keeping this
    module's no-blocking-external-call guarantee (see module docstring)
    intact even though it now also reflects procurement state.

    Deliberately includes EVERY line placed since the tracking start date --
    open or already closed/received -- so the total only ever grows as new
    POs go out, rather than dropping back down once a PO is fulfilled (which
    is what filtering on open row_status alone would do). Returns
    {item_code: [{'po_number', 'posting_date', 'quantity'}, ...]} sorted
    newest-first; an item with no PO lines in the window is simply absent
    (treat as an empty list / 0 total)."""
    from so.models import SAPPurchaseOrderItem

    codes = [c for c in item_codes if c]
    if not codes:
        return {}
    rows = (SAPPurchaseOrderItem.objects
            .filter(item_no__in=codes,
                    purchaseorder__posting_date__gte=ALREADY_ORDERED_TRACKING_START)
            .select_related('purchaseorder')
            .order_by('item_no', '-purchaseorder__posting_date'))
    breakdown_by_code = {}
    for row in rows:
        posting_date = row.purchaseorder.posting_date
        breakdown_by_code.setdefault(row.item_no, []).append({
            'po_number': row.purchaseorder.po_number,
            # isoformat string, not a raw date -- report.lines is a JSONField
            # (StockShortageReport.lines) and json.dumps can't serialize a
            # datetime.date, which silently failed report.save() inside
            # recompute()'s broad except-and-log (the report just never
            # updated) until this was caught and fixed.
            'posting_date': posting_date.isoformat() if posting_date else None,
            'quantity': float(row.quantity),
        })
    return breakdown_by_code


def _open_ordered_from_supplier(item_codes):
    """Quantity still OUTSTANDING (not yet received) on purchase orders we've
    placed with our own suppliers -- used only to net against `final_qty`
    when deriving `final_purchase_qty` (how much MORE still needs to be
    newly ordered). Kept separate from _already_ordered_from_supplier's
    cumulative-since-June "LPO Sent to Supplier" display figure: that one
    includes already-received POs, and netting the shortfall against those
    too would double count -- a received PO's quantity already lowered the
    shortfall via Items.total_available_stock, so subtracting it again here
    would understate what's really still left to order.

    Uses remaining_open_quantity when set, else the line's own quantity --
    same definition so/purchase_stock_requirement_views.py uses for what it
    calls "LPO given" (open POs). Returns {item_code: qty}; an item with no
    open PO lines is simply absent (treat as 0)."""
    from django.db.models import F, Sum, Value
    from django.db.models import DecimalField as _DecimalField
    from django.db.models.functions import Coalesce
    from so.models import SAPPurchaseOrderItem
    from so.sap_purchaseorder_views import _open_row_status_q_po

    codes = [c for c in item_codes if c]
    if not codes:
        return {}
    rows = (SAPPurchaseOrderItem.objects
            .filter(_open_row_status_q_po(), item_no__in=codes)
            .values('item_no')
            .annotate(total=Sum(Coalesce(F('remaining_open_quantity'), F('quantity'),
                                          Value(0, output_field=_DecimalField())))))
    return {row['item_no']: float(row['total'] or 0) for row in rows}


def _pending_lpo_sales_orders():
    """Every so.SalesOrder created from an LPO (has a linked LPORequest --
    see so.models.SalesOrder's reverse `source_lpo_request` accessor) that
    hasn't yet reached order_status='SO Created' -- i.e. still awaiting
    fulfilment/dispatch, so its items still count as live demand against
    current stock. An order already marked 'SO Created' is assumed to
    already be handled (stock committed/dispatched) and is excluded so it
    doesn't keep inflating required quantities indefinitely."""
    from so.models import SalesOrder

    return (SalesOrder.objects
            .filter(source_lpo_request__isnull=False)
            .exclude(order_status='SO Created')
            .select_related('source_lpo_request', 'customer')
            .prefetch_related('items__item'))


def recompute(triggered_by=None):
    """Re-derives required quantity per catalog item across EVERY pending
    LPO-sourced sales order (see _pending_lpo_sales_orders) from scratch,
    checks it against Items.total_available_stock (see
    get_available_stock), and updates the singleton StockShortageReport
    (see StockShortageReport.current) in place with every item that's
    short (or, for an item whose stock has never been synced at all, with
    availability left unknown rather than silently dropped).

    `triggered_by` is an optional so.SalesOrder -- purely informational
    (StockShortageReport.last_triggered_by), not part of what gets
    computed; pass None for a manual refresh with no specific trigger.

    Returns the updated singleton report. Never raises -- a failure is
    logged and swallowed (returning the report unchanged) so it can never
    block Sales Order creation."""
    from .models import StockShortageReport

    report = StockShortageReport.current()
    try:
        required_by_item = {}   # item_id -> {'item': Items, 'qty': float}
        lpo_breakdown = {}      # item_id -> {lpo_id: {...}}

        for so in _pending_lpo_sales_orders():
            lpo = getattr(so, 'source_lpo_request', None)
            for order_item in so.items.all():
                item = order_item.item
                if item is None:
                    continue
                bucket = required_by_item.setdefault(item.id, {'item': item, 'qty': 0.0})
                bucket['qty'] += order_item.quantity

                if lpo is not None:
                    per_item = lpo_breakdown.setdefault(item.id, {})
                    entry = per_item.setdefault(lpo.id, {
                        'lpo_id': lpo.id,
                        'lpo_number': lpo.lpo_number or f'LPO #{lpo.pk}',
                        'sales_order_number': so.order_number,
                        'customer_name': so.customer.customer_name if so.customer_id else (lpo.customer_name_stated or '—'),
                        'quantity': 0.0,
                        'payment_terms': lpo.payment_terms or '—',
                    })
                    entry['quantity'] += order_item.quantity

        item_codes = [bucket['item'].item_code for bucket in required_by_item.values()]
        already_ordered_breakdown_by_code = _already_ordered_from_supplier(item_codes)
        open_ordered_by_code = _open_ordered_from_supplier(item_codes)

        lines = []
        any_unknown = False
        for item_id, bucket in required_by_item.items():
            item = bucket['item']
            required_qty = bucket['qty']
            available_qty = get_available_stock(item)
            already_ordered_breakdown = already_ordered_breakdown_by_code.get(item.item_code, [])
            already_ordered_qty = sum(entry['quantity'] for entry in already_ordered_breakdown)
            open_ordered_qty = open_ordered_by_code.get(item.item_code, 0.0)

            if available_qty is None:
                # Never synced -- list it anyway (rather than dropping it)
                # so procurement still sees total demand even without a
                # known stock figure.
                final_qty = None
                final_purchase_qty = None
                any_unknown = True
            else:
                final_qty = max(required_qty - available_qty, 0.0)
                if final_qty <= 0:
                    continue  # fully covered by stock -- not a shortage
                # What's still genuinely left to place a NEW supplier order
                # for, after also netting out what's still OUTSTANDING on
                # an existing supplier PO -- e.g. shortfall 200, 100 still
                # open on order -> only 100 more actually needs ordering.
                # Nets against open_ordered_qty (not the cumulative-since-
                # June already_ordered_qty below): a received PO already
                # lowered final_qty via available_qty, so netting the
                # shortfall against it a second time here would double
                # count and understate what's really still left to order.
                final_purchase_qty = max(final_qty - open_ordered_qty, 0.0)

            lines.append({
                'item_code': item.item_code,
                'brand': item.item_firm or '',
                'description': item.item_description,
                'total_required_qty': required_qty,
                'available_qty': available_qty,
                'final_qty': final_qty,
                'already_ordered_qty': already_ordered_qty,
                'already_ordered_breakdown': already_ordered_breakdown,
                'final_purchase_qty': final_purchase_qty,
                'lpo_breakdown': sorted(
                    lpo_breakdown.get(item_id, {}).values(), key=lambda e: -e['quantity'],
                ),
            })

        lines.sort(key=lambda line: -(line['final_qty'] if line['final_qty'] is not None else line['total_required_qty']))

        report.lines = lines
        report.stock_api_error = (
            'One or more items have never had stock synced (Items.total_available_stock is unset) -- '
            'run the import_items2 stock sync to get their real availability.'
        ) if any_unknown else ''
        if triggered_by is not None:
            report.last_triggered_by = triggered_by
        report.save()
        return report
    except Exception:
        logger.exception("stock_check.recompute failed")
        return report


def run_stock_check_for_sales_order(sales_order):
    """Thin wrapper around recompute() for the two call sites that just
    created `sales_order` from an LPO -- see recompute's docstring."""
    return recompute(triggered_by=sales_order)
