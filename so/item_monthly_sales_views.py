"""
Item Monthly Sales - read-only page showing, for every item, month by month:
  * view=amount (default): the net SALES AMOUNT (invoices + credit memos, credit-memo values are
    stored negative - same definition as the other sales reports)
  * view=price: the LATEST UNIT PRICE of the month (price of the most recent invoice line). Clicking a
    cell lists every invoice line of that item in that month (loaded on click by `item_monthly_sales_lines`).

Standalone: uses only the existing AR Invoice / AR Credit Memo tables and the Items master, and
reuses the helpers of the existing sales pages without modifying them.

Unit price rules: invoice lines only (credit memos / returns are not selling prices), quantity > 0 and
price > 0, and cancelled invoices (csYes) and their cancellation documents (csCancellation) are skipped.
"""
import calendar
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from .brandwise_sales_analysis_views import MONTH_NAMES_SHORT, _net_value_expr
from .item_monthly_sales_export import PDF_ROW_LIMIT, pdf_response, xlsx_response
from .models import Items, SAPARCreditMemo, SAPARCreditMemoItem, SAPARInvoice, SAPARInvoiceItem
from .sap_salesorder_views import salesman_scope_q_salesorder

ZERO = Decimal('0')
CENT = Decimal('0.01')
MAX_POPUP_LINES = 500


def _filters(request):
    """Read the filters from the query string and build the invoice / credit-memo querysets."""
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
    return {
        'years': years, 'selected_year': selected_year, 'q': q, 'brand_filter': brand_filter,
        'salesman_filter': salesman_filter, 'store': store, 'salesmen': salesmen,
        'inv_items': inv_items, 'cm_items': cm_items,
    }


def _price_lines(inv_items):
    """Invoice lines that count as a selling price (see module docstring)."""
    return (inv_items.filter(quantity__gt=0, price__gt=0)
            .exclude(invoice__cancel_status__in=['csYes', 'csCancellation']))


def _price_change(prices):
    """Change from the first month that has a price (January when it has one) to the latest price of the
    year, as a percentage. None when fewer than two months have a price (nothing to compare)."""
    bought = [i for i, v in enumerate(prices) if v]
    if len(bought) < 2:
        return None
    first_i, last_i = bought[0], bought[-1]
    first_p, last_p = prices[first_i], prices[last_i]
    if not first_p or first_p <= 0:
        return None
    return {
        'pct': (last_p - first_p) / first_p * 100,
        'from_month': calendar.month_abbr[first_i + 1], 'to_month': calendar.month_abbr[last_i + 1],
        'from_price': first_p, 'to_price': last_p,
    }


@login_required
def item_monthly_sales(request):
    f = _filters(request)
    selected_year, q, store = f['selected_year'], f['q'], f['store']
    brand_filter, salesman_filter = f['brand_filter'], f['salesman_filter']
    inv_items, cm_items = f['inv_items'], f['cm_items']

    view = request.GET.get('view', '').strip().lower()
    view = 'price' if view == 'price' else 'amount'
    # unit-price view only: 'latest' (default, last invoice line of the month) or 'avg' (weighted average)
    # The Latest option is switched off for now (kept in the code): the unit-price view always shows the average.
    pmode = 'avg' if view == 'price' else 'latest'
    try:
        page_size = int(request.GET.get('page_size', 50))
    except ValueError:
        page_size = 50
    page_size = max(10, min(page_size, 200))

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
    export = export if export in ('xlsx', 'pdf') else ''

    def _attach_prices(target_rows):
        """Latest unit price of every month (and of the year) for the given rows."""
        for it in target_rows:
            it['p'] = [None] * 12
            it['p_total'] = None
        by_code = {it['item_code']: it for it in target_rows}
        lines = _price_lines(inv_items)
        if len(by_code) < 500:   # a page worth of items: narrow the query; the full export reads them all
            lines = lines.filter(item_code__in=list(by_code))
        lines = lines.values(
            'item_code', 'invoice__posting_date__month', 'price', 'quantity',
        ).order_by('item_code', 'invoice__posting_date', 'invoice__invoice_number', 'line_no')
        sums = {}   # (item, month; month 0 = whole year) -> [price x qty, qty]   (average mode)
        for ln in lines.iterator(chunk_size=5000):
            it = by_code.get((ln['item_code'] or '').strip())
            m = ln['invoice__posting_date__month']
            if it is None or not m:
                continue
            if pmode == 'avg':
                value = ln['price'] * ln['quantity']
                for key in ((it['item_code'], m), (it['item_code'], 0)):
                    acc = sums.setdefault(key, [ZERO, ZERO])
                    acc[0] += value
                    acc[1] += ln['quantity']
                continue
            price = ln['price'].quantize(CENT)
            it['p'][m - 1] = price      # rows come in date order, so the last write is the latest
            it['p_total'] = price
        for (code, m), (value, qty) in sums.items():
            if qty > 0:   # weighted average = total value / total quantity
                avg = (value / qty).quantize(CENT, rounding=ROUND_HALF_UP)
                if m:
                    by_code[code]['p'][m - 1] = avg
                else:
                    by_code[code]['p_total'] = avg
        for it in target_rows:
            it['p_last_m'] = 0
            if pmode == 'avg':
                # the total column shows the LAST month's average (the figure the Change in Price % is worked out from)
                bought = [i for i, v in enumerate(it['p']) if v]
                it['p_total'] = it['p'][bought[-1]] if bought else None
                it['p_last_m'] = bought[-1] + 1 if bought else 0
            it['price_change'] = _price_change(it['p'])

    if export:
        # export covers every matching item (not just one page); same numbers as the screen
        if view == 'price':
            _attach_prices(rows)
        filter_lines = [
            ('View', ('Average unit price (weighted)' if pmode == 'avg' else 'Latest unit price') if view == 'price' else 'Sales amount'),
            ('Store', store or 'All'),
            ('Brand', ', '.join(brand_filter) or 'All'),
            ('Salesman', ', '.join(salesman_filter) or 'All'),
            ('Search', q or '-'),
        ]
        if export == 'xlsx':
            return xlsx_response(rows, selected_year, filter_lines, view, pmode)
        resp = pdf_response(rows, selected_year, filter_lines, view, pmode)
        if resp is None:
            return HttpResponse(
                f'{len(rows)} items are too many for one PDF (max {PDF_ROW_LIMIT}). '
                'Filter by brand first, or download the Excel file instead.',
                status=400, content_type='text/plain')
        return resp

    paginator = Paginator(rows, page_size)
    page_obj = paginator.get_page(request.GET.get('page'))
    page_rows = list(page_obj.object_list)
    if view == 'price':
        _attach_prices(page_rows)
    for it in page_rows:
        # compact data for the line charts (drawn in the browser, no chart library)
        series = it['p'] if view == 'price' else it['m']
        it['chart_json'] = json.dumps({
            'code': it['item_code'], 'desc': it['description'], 'year': selected_year, 'kind': view, 'pmode': pmode,
            'm': [round(float(v), 2) if v else 0 for v in series],
        })

    brands = list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
                  .values_list('item_firm', flat=True).distinct().order_by('item_firm'))

    params = request.GET.copy()
    params.pop('page', None)
    params_nopmode = params.copy()
    params_nopmode.pop('view', None)
    params_nopmode.pop('pmode', None)
    params_nopmode.pop('export', None)
    params_noview = params.copy()
    params_noview.pop('view', None)
    params_noview.pop('export', None)

    return render(request, 'item_monthly_sales/page.html', {
        'years': f['years'], 'selected_year': selected_year,
        'brands': brands, 'salesmen': f['salesmen'],
        'month_names': MONTH_NAMES_SHORT, 'view': view, 'pmode': pmode,
        'qs_nopmode': params_nopmode.urlencode(),
        'rows': page_rows, 'page_obj': page_obj, 'total_count': paginator.count,
        'month_totals': month_totals, 'grand_total': grand_total,
        'querystring': params.urlencode(),
        'qs_noview': params_noview.urlencode(),
        'filters': {'q': q, 'brand_filter': brand_filter, 'salesman_filter': salesman_filter,
                    'store': store, 'page_size': page_size},
    })


@login_required
def item_monthly_sales_lines(request):
    """Every invoice line behind one unit-price cell (item + month; month=0 means the whole year).
    Same filters and the same user scope as the page. Read-only JSON, loaded when a cell is clicked."""
    f = _filters(request)
    item = request.GET.get('item', '').strip()
    try:
        month = int(request.GET.get('month', '0'))
    except ValueError:
        month = 0
    if not item or not 0 <= month <= 12:
        return JsonResponse({'error': 'bad request'}, status=400)

    qs = _price_lines(f['inv_items']).filter(item_code=item)
    if month:
        qs = qs.filter(invoice__posting_date__month=month)
    qs = qs.values(
        'invoice__posting_date', 'invoice__invoice_number', 'invoice__customer_name',
        'invoice__salesman_name', 'quantity', 'price', 'line_total', 'line_total_after_discount',
    ).order_by('invoice__posting_date', 'invoice__invoice_number', 'line_no')

    lines, total_qty, total_amt, total_val, invoices = [], ZERO, ZERO, ZERO, set()
    lo = hi = None
    for r in qs:
        price = r['price'].quantize(CENT)
        amount = r['line_total_after_discount'] or r['line_total'] or (r['price'] * r['quantity'])
        total_qty += r['quantity']
        total_amt += amount
        total_val += r['price'] * r['quantity']
        invoices.add(r['invoice__invoice_number'])
        lo = price if lo is None or price < lo else lo
        hi = price if hi is None or price > hi else hi
        lines.append({
            'date': r['invoice__posting_date'].strftime('%d %b %Y') if r['invoice__posting_date'] else '',
            'invoice': r['invoice__invoice_number'], 'customer': r['invoice__customer_name'] or '',
            'salesman': r['invoice__salesman_name'] or '',
            'qty': f"{r['quantity']:,.2f}".rstrip('0').rstrip('.'), 'price': f'{price:,.2f}',
            'amount': f'{amount:,.2f}',
        })
    summary = None
    if lines:
        summary = {
            'latest': lines[-1]['price'], 'low': f'{lo:,.2f}', 'high': f'{hi:,.2f}',
            'avg': f'{(total_val / total_qty).quantize(CENT, rounding=ROUND_HALF_UP):,.2f}' if total_qty else '-',
            'invoices': len(invoices), 'lines': len(lines),
        }
    truncated = len(lines) > MAX_POPUP_LINES
    if truncated:
        lines = lines[-MAX_POPUP_LINES:]   # keep the most recent lines
    return JsonResponse({'summary': summary, 'lines': lines, 'truncated': truncated})
