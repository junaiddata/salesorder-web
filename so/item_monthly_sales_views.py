"""
Item Monthly Sales - read-only page showing the net SALES AMOUNT of every item, month by month.

Standalone: uses only the existing AR Invoice / AR Credit Memo tables and the Items master, and
reuses the helpers of the existing sales pages without modifying them. Net amount = invoices +
credit memos (credit-memo values are stored negative), same definition as the other sales reports.
"""
import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render

from .brandwise_sales_analysis_views import MONTH_NAMES_SHORT, _net_value_expr
from .item_monthly_sales_export import PDF_ROW_LIMIT, pdf_response, xlsx_response
from .models import Items, SAPARCreditMemo, SAPARCreditMemoItem, SAPARInvoice, SAPARInvoiceItem
from .sap_salesorder_views import salesman_scope_q_salesorder

ZERO = Decimal('0')


@login_required
def item_monthly_sales(request):
    current_year = date.today().year
    years = list(range(2024, current_year + 2))
    try:
        selected_year = int(request.GET.get('year', '').strip() or current_year)
    except (ValueError, TypeError):
        selected_year = current_year
    if selected_year not in years:
        selected_year = current_year

    q = request.GET.get('q', '').strip()
    brand_filter = [b.strip() for b in request.GET.getlist('brand') if b and b.strip()]
    salesman_filter = [s.strip() for s in request.GET.getlist('salesman') if s and s.strip()]
    store = request.GET.get('store', '').strip()
    if store not in ('HO', 'Others'):
        store = ''
    try:
        page_size = int(request.GET.get('page_size', 50))
    except ValueError:
        page_size = 50
    page_size = max(10, min(page_size, 200))

    start, end = date(selected_year, 1, 1), date(selected_year, 12, 31)
    scope_q = salesman_scope_q_salesorder(request.user)
    inv_h = (SAPARInvoice.objects.filter(scope_q).exclude(salesman_name__iexact='Z.DUTY')
             .filter(posting_date__gte=start, posting_date__lte=end))
    cm_h = (SAPARCreditMemo.objects.filter(scope_q).exclude(salesman_name__iexact='Z.DUTY')
            .filter(posting_date__gte=start, posting_date__lte=end))

    # options for the salesman filter (only what this user may see)
    salesmen = [s for s in inv_h.values_list('salesman_name', flat=True).distinct().order_by('salesman_name') if s]

    if salesman_filter:
        inv_h = inv_h.filter(salesman_name__in=salesman_filter)
        cm_h = cm_h.filter(salesman_name__in=salesman_filter)
    if store:
        inv_h = inv_h.filter(store=store)
        cm_h = cm_h.filter(store=store)

    inv_items = (SAPARInvoiceItem.objects.filter(invoice__in=inv_h)
                 .exclude(item_code__isnull=True).exclude(item_code=''))
    cm_items = (SAPARCreditMemoItem.objects.filter(credit_memo__in=cm_h)
                .exclude(item_code__isnull=True).exclude(item_code=''))
    if brand_filter:
        inv_items = inv_items.filter(item__item_firm__in=brand_filter)
        cm_items = cm_items.filter(item__item_firm__in=brand_filter)
    if q:
        inv_items = inv_items.filter(Q(item_code__icontains=q) | Q(item_description__icontains=q))
        cm_items = cm_items.filter(Q(item_code__icontains=q) | Q(item_description__icontains=q))

    items = {}  # item_code -> {'description', 'brand', 'm': [12 amounts]}

    def _add(rows, month_key):
        for r in rows:
            code = (r['item_code'] or '').strip()
            month = r[month_key]
            if not code or not month:
                continue
            it = items.get(code)
            if it is None:
                it = items[code] = {'item_code': code, 'description': '', 'brand': '', 'm': [ZERO] * 12}
            if not it['description'] and r.get('item_description'):
                it['description'] = r['item_description']
            if not it['brand'] and r.get('item__item_firm'):
                it['brand'] = r['item__item_firm']
            it['m'][month - 1] += r['sales'] or ZERO

    _add(inv_items.values('item_code', 'item_description', 'item__item_firm', 'invoice__posting_date__month')
         .annotate(sales=_net_value_expr()), 'invoice__posting_date__month')
    _add(cm_items.values('item_code', 'item_description', 'item__item_firm', 'credit_memo__posting_date__month')
         .annotate(sales=_net_value_expr()), 'credit_memo__posting_date__month')

    # brand fallback from the Items master for lines that are not linked to an item
    missing = [c for c, it in items.items() if not it['brand']]
    if missing:
        for code, firm in Items.objects.filter(item_code__in=missing).values_list('item_code', 'item_firm'):
            items[code]['brand'] = firm or ''

    rows = list(items.values())
    month_totals = [ZERO] * 12
    for it in rows:
        it['total'] = sum(it['m'], ZERO)
        for i, v in enumerate(it['m']):
            month_totals[i] += v
    rows.sort(key=lambda it: (-it['total'], it['item_code']))
    grand_total = sum(month_totals, ZERO)

    export = request.GET.get('export', '').strip().lower()
    if export in ('xlsx', 'pdf'):
        # export covers every matching item (not just one page); same numbers as the screen
        filter_lines = [
            ('Store', store or 'All'),
            ('Brand', ', '.join(brand_filter) or 'All'),
            ('Salesman', ', '.join(salesman_filter) or 'All'),
            ('Search', q or '-'),
        ]
        if export == 'xlsx':
            return xlsx_response(rows, selected_year, filter_lines)
        resp = pdf_response(rows, selected_year, filter_lines)
        if resp is None:
            return HttpResponse(
                f'{len(rows)} items are too many for one PDF (max {PDF_ROW_LIMIT}). '
                'Filter by brand first, or download the Excel file instead.',
                status=400, content_type='text/plain')
        return resp

    paginator = Paginator(rows, page_size)
    page_obj = paginator.get_page(request.GET.get('page'))
    page_rows = list(page_obj.object_list)
    for it in page_rows:
        # compact data for the bar charts (drawn in the browser, no chart library)
        it['chart_json'] = json.dumps({
            'code': it['item_code'], 'desc': it['description'], 'year': selected_year,
            'm': [round(float(v), 2) for v in it['m']],
        })

    brands = list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
                  .values_list('item_firm', flat=True).distinct().order_by('item_firm'))

    params = request.GET.copy()
    params.pop('page', None)

    return render(request, 'item_monthly_sales/page.html', {
        'years': years, 'selected_year': selected_year,
        'brands': brands, 'salesmen': salesmen,
        'month_names': MONTH_NAMES_SHORT,
        'rows': page_rows, 'page_obj': page_obj, 'total_count': paginator.count,
        'month_totals': month_totals, 'grand_total': grand_total,
        'querystring': params.urlencode(),
        'filters': {'q': q, 'brand_filter': brand_filter, 'salesman_filter': salesman_filter,
                    'store': store, 'page_size': page_size},
    })
