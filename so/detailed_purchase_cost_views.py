"""
Detailed Purchase Analysis - Monthly Purchase Cost.

Read-only page: for every item, the average price we actually PAID on purchase orders in each
month of the selected year. Uses only the existing DetailedPurchaseOrder / DetailedPurchaseOrderItem
tables (no sync or model changes) and reuses the helpers of detailed_purchase_analysis_views
without modifying them.

Price used everywhere on this page = the PO line `price` exactly as the SAP API sends it.
Monthly figure of an item = Sum(price * quantity) / Sum(quantity) over its PO lines posted in that
month (quantity-weighted average of the API prices). Lines with no quantity or no
value are ignored so they cannot drag the average down.
"""
import calendar
import json
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Min, Q, Sum
from django.db.models.functions import ExtractMonth
from django.shortcuts import render

from .detailed_purchase_analysis_views import (
    DEFAULT_YEAR, FIRST_YEAR, NO_BRAND, NO_BRAND_LABEL, _with_brand,
)
from .models import DetailedPurchaseOrder, DetailedPurchaseOrderItem, Items
from .sap_purchaseorder_views import _user_can_see_price


# A price counts as "changed" when it differs from the previous PO line's price by at least this much.
PRICE_CHANGE_MIN = Decimal('0.01')

# price * quantity, used for the quantity-weighted average of the API prices
PRICE_X_QTY = ExpressionWrapper(F('price') * F('quantity'), output_field=DecimalField(max_digits=20, decimal_places=4))


@login_required
def detailed_purchase_monthly_cost(request):
    show_price = _user_can_see_price(request)

    year_param = request.GET.get('year', '').strip()
    selected_year = int(year_param) if year_param.isdigit() else DEFAULT_YEAR

    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip().upper()
    warehouse = request.GET.get('warehouse', '').strip()
    purchaser_filter = [p.strip() for p in request.GET.getlist('purchaser') if p and p.strip()]
    brand_filter = [b.strip() for b in request.GET.getlist('brand') if b and b.strip()]
    try:
        page_size = int(request.GET.get('page_size', 50))
    except ValueError:
        page_size = 50
    page_size = max(10, min(page_size, 200))

    years = sorted(
        {d.year for d in DetailedPurchaseOrder.objects.exclude(posting_date__isnull=True).dates('posting_date', 'year')}
        | set(range(FIRST_YEAR, DEFAULT_YEAR + 1)),
        reverse=True,
    )
    warehouses = [
        w for w in DetailedPurchaseOrderItem.objects.values_list('warehouse_code', flat=True).distinct().order_by('warehouse_code') if w
    ]
    brands = list(
        Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
        .values_list('item_firm', flat=True).distinct().order_by('item_firm')
    )
    purchasers = [
        p for p in DetailedPurchaseOrder.objects.filter(posting_date__year=selected_year)
        .values_list('salesman_name', flat=True).distinct().order_by('salesman_name') if p
    ]

    params = request.GET.copy()
    params.pop('page', None)

    context = {
        'show_price': show_price,
        'years': years, 'selected_year': selected_year,
        'warehouses': warehouses, 'brands': brands, 'purchasers': purchasers,
        'no_brand': NO_BRAND, 'no_brand_label': NO_BRAND_LABEL,
        'month_names': [calendar.month_abbr[m] for m in range(1, 13)],
        'querystring': params.urlencode(),
        'filters': {
            'q': q, 'status': status, 'warehouse': warehouse,
            'purchaser_filter': purchaser_filter, 'brand_filter': brand_filter, 'page_size': page_size,
        },
        'rows': [], 'page_obj': None, 'total_count': 0,
    }
    if not show_price:
        # Prices are admin-only on the existing pages; same rule here.
        return render(request, 'detailed_purchase_analysis/monthly_cost.html', context)

    qs = _with_brand(DetailedPurchaseOrderItem.objects.select_related('purchaseorder')).filter(
        purchaseorder__posting_date__year=selected_year,
        quantity__gt=0,
        price__gt=0,
    ).exclude(item_no__isnull=True).exclude(item_no='')

    if status in ('O', 'C'):
        qs = qs.filter(purchaseorder__status=status)
    if warehouse:
        qs = qs.filter(warehouse_code=warehouse)
    if purchaser_filter:
        qs = qs.filter(purchaseorder__salesman_name__in=purchaser_filter)
    if brand_filter:
        brand_q = Q(brand__in=[b for b in brand_filter if b != NO_BRAND])
        if NO_BRAND in brand_filter:
            brand_q |= Q(brand='')
        qs = qs.filter(brand_q)
    if q:
        qs = qs.filter(
            Q(purchaseorder__supplier_name__icontains=q) |
            Q(purchaseorder__supplier_code__icontains=q) |
            Q(purchaseorder__po_number__icontains=q) |
            Q(item_no__icontains=q) |
            Q(description__icontains=q)
        )

    # 1) one row per item (paginated)
    item_rows = qs.values('item_no').annotate(
        description=Max('description'), brand_name=Max('brand'),
    ).order_by('item_no')
    paginator = Paginator(item_rows, page_size)
    page_obj = paginator.get_page(request.GET.get('page'))
    page_items = list(page_obj.object_list)
    codes = [r['item_no'] for r in page_items]

    # 2) monthly + yearly aggregates for the items on this page only
    page_qs = qs.filter(item_no__in=codes)
    monthly = page_qs.annotate(month=ExtractMonth('purchaseorder__posting_date')).values('item_no', 'month').annotate(
        qty=Sum('quantity'), value=Sum(PRICE_X_QTY),
        min_price=Min('price'), max_price=Max('price'),
        po_count=Count('purchaseorder_id', distinct=True),
    )
    by_item = {}
    for r in monthly:
        if not r['month'] or not r['qty']:
            continue
        by_item.setdefault(r['item_no'], {})[r['month']] = r

    # PO lines of this page's items, in date order, to count price changes inside each month.
    # Price = the API `price` of the line, rounded to 2 decimals; a "change" is a difference of at least PRICE_CHANGE_MIN from the previous line.
    lines_by_cell = {}
    line_rows = page_qs.values(
        'item_no', 'purchaseorder__posting_date', 'purchaseorder__po_number',
        'purchaseorder__status', 'line_no', 'quantity', 'price',
    ).order_by('item_no', 'purchaseorder__posting_date', 'purchaseorder__po_number', 'line_no')
    for ln in line_rows:
        d = ln['purchaseorder__posting_date']
        if not d or not ln['quantity']:
            continue
        unit = ln['price'].quantize(Decimal('0.01'))
        lines_by_cell.setdefault((ln['item_no'], d.month), []).append({
            'date': d, 'po': ln['purchaseorder__po_number'], 'status': ln['purchaseorder__status'],
            'qty': ln['quantity'], 'price': unit,
        })

    def _analyse(lines):
        """Return (change_count, 'a → b → c' trail of the successive different prices)."""
        changes = 0
        trail = [lines[0]['price']]
        for prev, cur in zip(lines, lines[1:]):
            if abs(cur['price'] - prev['price']) >= PRICE_CHANGE_MIN:
                changes += 1
                trail.append(cur['price'])
        return changes, ' → '.join(f'{p:,.2f}' for p in trail)

    yearly = {
        r['item_no']: r for r in page_qs.values('item_no').annotate(
            qty=Sum('quantity'), value=Sum(PRICE_X_QTY))
    }
    cost_map = dict(Items.objects.filter(item_code__in=codes).values_list('item_code', 'item_cost'))

    rows = []
    for r in page_items:
        code = r['item_no']
        months = []
        year_changes = 0
        for m in range(1, 13):
            cell = by_item.get(code, {}).get(m)
            if cell:
                lines = lines_by_cell.get((code, m), [])
                changes, trail = _analyse(lines) if lines else (0, '')
                year_changes += changes
                months.append({
                    'avg': cell['value'] / cell['qty'],
                    # price of the most recent PO line in the month (lines are in date / PO / line order);
                    # falls back to the average if the lines could not be read
                    'last': lines[-1]['price'] if lines else cell['value'] / cell['qty'],
                    'qty': cell['qty'], 'value': cell['value'],
                    'min': cell['min_price'], 'max': cell['max_price'], 'pos': cell['po_count'],
                    'changes': changes, 'trail': trail, 'lines': lines,
                    'label': f"{r['item_no']} - {calendar.month_name[m]} {selected_year}",
                })
            else:
                months.append(None)
        y = yearly.get(code)
        year_avg = (y['value'] / y['qty']) if y and y['qty'] else None
        cur = cost_map.get(code)
        # Compact data for the trend charts (drawn in the browser, no chart library needed)
        chart_data = {
            'code': code, 'desc': r['description'] or '', 'year': selected_year,
            'cur': round(float(cur), 2) if cur else None,
            'm': [
                None if c is None else {
                    'l': round(float(c['last']), 2), 'a': round(float(c['avg']), 2),
                    'lo': round(float(c['min']), 2), 'hi': round(float(c['max']), 2),
                    'c': c['changes'],
                } for c in months
            ],
        }
        rows.append({
            'chart_json': json.dumps(chart_data),
            'item_code': code, 'description': r['description'] or '', 'brand': r['brand_name'] or '',
            'months': months, 'year_avg': year_avg, 'year_changes': year_changes,
            'year_qty': y['qty'] if y else Decimal('0'),
            'current_cost': cur if cur else None,
        })

    context.update({'rows': rows, 'page_obj': page_obj, 'total_count': paginator.count})
    return render(request, 'detailed_purchase_analysis/monthly_cost.html', context)
