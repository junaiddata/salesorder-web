"""
Detailed Purchase Analysis: open + closed purchase orders (item level) with warehouse.
Independent from sap_purchaseorder_views.py.
"""
import json
import logging
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from collections import defaultdict

from django.db.models import Q, Sum, Max, Count, Value, DecimalField, OuterRef, Subquery
from django.db.models import CharField
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import DetailedPurchaseOrder, DetailedPurchaseOrderItem, Items
from .detailed_purchase_analysis_services import save_detailed_purchaseorders
from .sap_purchaseorder_views import _user_can_see_price

logger = logging.getLogger(__name__)

_ZERO = Value(0, output_field=DecimalField())

# Year filter default when the page is opened without ?year=
DEFAULT_YEAR = 2026
# Earliest year always offered in the Year dropdown
FIRST_YEAR = 2024


# Pseudo-brand for PO lines whose item is missing from the Item Master (or has no firm)
NO_BRAND = '__none__'
NO_BRAND_LABEL = 'No Brand (not in Item Master)'


def _with_brand(qs):
    """Annotate each PO line with its brand (Items.item_firm, matched on item code); '' if unknown."""
    return qs.annotate(
        brand=Coalesce(
            Subquery(Items.objects.filter(item_code=OuterRef('item_no')).values('item_firm')[:1]),
            Value(''),
            output_field=CharField(),
        )
    )


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


@csrf_exempt
@require_POST
def detailed_purchase_analysis_receive(request):
    """Receive mapped open+closed purchase orders pushed by the PC sync script."""
    try:
        data = json.loads(request.body)
        expected_key = getattr(settings, 'VPS_API_KEY', 'your-secret-api-key')
        if not data.get('api_key') or data.get('api_key') != expected_key:
            return JsonResponse({'success': False, 'error': 'Invalid API key'}, status=401)

        purchase_orders = data.get('purchase_orders', [])
        if not purchase_orders:
            return JsonResponse({'success': False, 'error': 'No purchase orders provided'})

        stats = save_detailed_purchaseorders(purchase_orders)
        return JsonResponse({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception('Error in detailed_purchase_analysis_receive')
        return JsonResponse({'success': False, 'error': str(e), 'error_type': type(e).__name__}, status=500)


def _brand_comparison(qs, brand_filter, show_price):
    """
    Brand-wise comparison (same layout idea as Supplier/Brandwise Item Analysis):
      - summary: one column per selected brand
      - ranked: each brand's items ranked side by side (by value, or qty if prices hidden)
    """
    pct = lambda a, b: (a / b * 100) if b else Decimal('0')

    summary_rows = {
        r['brand']: r for r in qs.values('brand').annotate(
            po_count=Count('purchaseorder_id', distinct=True),
            open_po=Count('purchaseorder_id', filter=Q(purchaseorder__status='O'), distinct=True),
            closed_po=Count('purchaseorder_id', filter=Q(purchaseorder__status='C'), distinct=True),
            item_count=Count('item_no', distinct=True),
            qty=Coalesce(Sum('quantity'), _ZERO),
            open_qty=Coalesce(Sum('remaining_open_quantity'), _ZERO),
            value=Coalesce(Sum('row_total'), _ZERO),
            pending=Coalesce(Sum('pending_amount'), _ZERO),
        )
    }

    # brand -> item_no -> aggregates (grouped by PO number in SQL, merged here)
    data = defaultdict(dict)
    grouped = qs.values('brand', 'item_no', 'purchaseorder__po_number').annotate(
        qty=Coalesce(Sum('quantity'), _ZERO),
        open_qty=Coalesce(Sum('remaining_open_quantity'), _ZERO),
        value=Coalesce(Sum('row_total'), _ZERO),
        pending=Coalesce(Sum('pending_amount'), _ZERO),
        description=Max('description'),
        po_date=Max('purchaseorder__posting_date'),
    )
    for r in grouped:
        d = data[r['brand']].setdefault(r['item_no'], {
            'item_code': r['item_no'], 'description': r['description'] or '', 'docs': {},
            'qty': Decimal('0'), 'open_qty': Decimal('0'), 'value': Decimal('0'), 'pending': Decimal('0'),
        })
        d['qty'] += r['qty']
        d['open_qty'] += r['open_qty']
        d['value'] += r['value']
        d['pending'] += r['pending']
        d['docs'][r['purchaseorder__po_number']] = r['po_date']
        if not d['description'] and r['description']:
            d['description'] = r['description']

    sort_key = (lambda i: i['value']) if show_price else (lambda i: i['qty'])
    ranked = {}
    key_of = lambda b: '' if b == NO_BRAND else b
    label_of = lambda b: NO_BRAND_LABEL if b == NO_BRAND else b
    for brand in brand_filter:
        items = sorted(data.get(key_of(brand), {}).values(), key=sort_key, reverse=True)
        for it in items:
            # most recent documents first (by posting date); template shows the first few
            docs = sorted(it['docs'], key=lambda n: (it['docs'][n] or date.min, n), reverse=True)
            it['doc_list'] = docs[:3]
            it['doc_more'] = max(0, len(docs) - 3)
            it['doc_all'] = ', '.join(docs)
        ranked[brand] = items

    grand_value = sum((summary_rows.get(key_of(b), {}).get('value', Decimal('0')) for b in brand_filter), Decimal('0'))
    brand_summaries = []
    for brand in brand_filter:
        r = summary_rows.get(key_of(brand), {})
        qty = r.get('qty', Decimal('0'))
        value = r.get('value', Decimal('0'))
        brand_summaries.append({
            'brand': label_of(brand),
            'po_count': r.get('po_count', 0), 'open_po': r.get('open_po', 0), 'closed_po': r.get('closed_po', 0),
            'item_count': r.get('item_count', 0),
            'qty': qty, 'open_qty': r.get('open_qty', Decimal('0')),
            'value': value, 'pending': r.get('pending', Decimal('0')),
            'avg_rate': (value / qty) if qty else Decimal('0'),
            'share': pct(value, grand_value),
        })

    rank_rows = []
    for i in range(max((len(v) for v in ranked.values()), default=0)):
        rank_rows.append({
            'rank': i + 1,
            'cells': [ranked[b][i] if i < len(ranked[b]) else None for b in brand_filter],
        })
    return brand_summaries, rank_rows


@login_required
def detailed_purchase_analysis_list(request):
    """Item-level list of open and closed purchase orders with filters."""
    qs = _with_brand(DetailedPurchaseOrderItem.objects.select_related('purchaseorder'))
    # Year filter: ?year=2026 (default DEFAULT_YEAR) or ?year=all
    year_param = request.GET.get('year', '').strip().lower()
    if year_param == 'all':
        selected_year = None
    elif year_param.isdigit():
        selected_year = int(year_param)
    else:
        selected_year = DEFAULT_YEAR
    if selected_year:
        qs = qs.filter(purchaseorder__posting_date__year=selected_year)

    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip().upper()  # O / C / '' (all)
    warehouse = request.GET.get('warehouse', '').strip()
    purchaser_filter = [p.strip() for p in request.GET.getlist('purchaser') if p and p.strip()]
    brand_filter = [b.strip() for b in request.GET.getlist('brand') if b and b.strip()]
    start = _parse_date(request.GET.get('start', '').strip())
    end = _parse_date(request.GET.get('end', '').strip())

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
    if start:
        qs = qs.filter(purchaseorder__posting_date__gte=start)
    if end:
        qs = qs.filter(purchaseorder__posting_date__lte=end)
    if q:
        qs = qs.filter(
            Q(purchaseorder__po_number__icontains=q) |
            Q(purchaseorder__supplier_name__icontains=q) |
            Q(purchaseorder__supplier_code__icontains=q) |
            Q(purchaseorder__bp_reference_no__icontains=q) |
            Q(item_no__icontains=q) |
            Q(description__icontains=q)
        )

    totals = qs.aggregate(
        row_total=Coalesce(Sum('row_total'), _ZERO),
        pending=Coalesce(Sum('pending_amount'), _ZERO),
    )
    po_count = qs.values('purchaseorder_id').distinct().count()
    line_count = qs.count()

    show_price = _user_can_see_price(request)
    view_mode = 'lines' if request.GET.get('view') == 'lines' else 'brand'

    try:
        page_size = int(request.GET.get('page_size', 100))
    except ValueError:
        page_size = 100
    page_size = max(10, min(page_size, 200))

    brand_summaries, rank_rows = [], []
    if view_mode == 'brand' and brand_filter:
        brand_summaries, rank_rows = _brand_comparison(qs, brand_filter, show_price)
        paginator = Paginator(rank_rows, page_size)
        page_obj = paginator.get_page(request.GET.get('page'))
        rank_rows = list(page_obj.object_list)
        total_count = paginator.count
    else:
        qs = qs.order_by('-purchaseorder__posting_date', '-purchaseorder__po_number', 'line_no')
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(request.GET.get('page'))
        total_count = paginator.count

    warehouses = [
        w for w in DetailedPurchaseOrderItem.objects.values_list('warehouse_code', flat=True).distinct().order_by('warehouse_code') if w
    ]
    brands = list(
        Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
        .values_list('item_firm', flat=True).distinct().order_by('item_firm')
    )
    years = sorted(
        {d.year for d in DetailedPurchaseOrder.objects.exclude(posting_date__isnull=True).dates('posting_date', 'year')}
        | set(range(FIRST_YEAR, DEFAULT_YEAR + 1)),
        reverse=True,
    )
    purchasers = [
        p for p in DetailedPurchaseOrder.objects.filter(**({'posting_date__year': selected_year} if selected_year else {})).values_list('salesman_name', flat=True).distinct().order_by('salesman_name') if p
    ]

    # Keep filters when paging
    params = request.GET.copy()
    params.pop('page', None)
    params_noview = params.copy()
    params_noview.pop('view', None)

    return render(request, 'detailed_purchase_analysis/list.html', {
        'page_obj': page_obj,
        'total_count': total_count,
        'line_count': line_count,
        'view_mode': view_mode,
        'brand_summaries': brand_summaries,
        'rank_rows': rank_rows,
        'po_count': po_count,
        'totals': totals,
        'warehouses': warehouses,
        'purchasers': purchasers,
        'brands': brands,
        'no_brand': NO_BRAND,
        'no_brand_label': NO_BRAND_LABEL,
        'show_price': show_price,
        'years': years,
        'selected_year': selected_year,
        'querystring': params.urlencode(),
        'querystring_noview': params_noview.urlencode(),
        'filters': {
            'q': q, 'status': status, 'warehouse': warehouse, 'purchaser_filter': purchaser_filter, 'brand_filter': brand_filter,
            'start': request.GET.get('start', ''), 'end': request.GET.get('end', ''),
            'page_size': page_size,
        },
    })


@login_required
def detailed_purchase_analysis_detail(request, po_number):
    po = get_object_or_404(DetailedPurchaseOrder, po_number=po_number)
    items = _with_brand(po.items.all()).order_by('line_no', 'id')
    pending_total = items.aggregate(t=Coalesce(Sum('pending_amount'), _ZERO))['t'] or Decimal('0.00')
    return render(request, 'detailed_purchase_analysis/detail.html', {
        'po': po,
        'items': items,
        'status_label': 'Open' if po.status == 'O' else 'Closed',
        'pending_total': pending_total,
        'show_price': _user_can_see_price(request),
    })
