"""
Supplier / Brandwise Item Analysis
==================================
Compares the brands (Items.item_firm) the user selects SIDE BY SIDE, using the
same data and net-sales definition as Item Analysis (AR Invoice lines + AR
Credit Memo lines, credit memos netting negative):

  - Summary comparison : one column per brand (Sales, GP, GP %, Quantity,
                         Items, Avg Rate, Share of selected brands)
  - Item comparison    : one block of columns per brand, each listing that
                         brand's top items by sales, ranked side by side

Filters: Year, Store, Month (multi), Salesman (multi), Brand (multi), Item search.
All items of the selected brands are listed, paginated.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Max, Q, DecimalField, Value
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.shortcuts import render

from .brandwise_sales_analysis_views import (
    MONTH_NAMES_SHORT,
    _net_value_expr,
    _gp_value_expr,
    _pct,
    _user_is_admin,
)
from .models import (
    Items,
    SAPARInvoice,
    SAPARInvoiceItem,
    SAPARCreditMemo,
    SAPARCreditMemoItem,
)
from .sap_salesorder_views import salesman_scope_q_salesorder

PAGE_SIZE = 50  # ranked rows per page (each row holds one item per brand)


def _qty_expr():
    return Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))


@login_required
def supplier_brandwise_item_analysis(request):
    today = date.today()
    current_year = today.year

    # ── Filters ──────────────────────────────────────────────────
    calendar_years = list(range(2024, current_year + 2))
    try:
        selected_year = int(request.GET.get('year', '').strip() or current_year)
    except (ValueError, TypeError):
        selected_year = current_year
    if selected_year not in calendar_years:
        selected_year = current_year

    store_filter = (request.GET.get('store', 'Total') or 'Total').strip()
    if store_filter not in ('HO', 'Others', 'Total'):
        store_filter = 'Total'

    def _parse_month(val, default):
        try:
            m = int(str(val).strip())
        except (ValueError, TypeError):
            return default
        return m if 1 <= m <= 12 else default

    # Multi-select months; empty = all months of the year
    selected_months = sorted({
        m for m in (_parse_month(v, None) for v in request.GET.getlist('month')) if m
    })

    selected_salesmen = [s.strip() for s in request.GET.getlist('salesman') if s.strip()]
    selected_brands = list(dict.fromkeys(
        b.strip() for b in request.GET.getlist('brand') if b and b.strip()
    ))
    search_query = request.GET.get('q', '').strip()

    is_admin = _user_is_admin(request.user)

    # ── Option lists ─────────────────────────────────────────────
    scope_q = salesman_scope_q_salesorder(request.user)
    salesmen = list(
        SAPARInvoice.objects.filter(scope_q)
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .exclude(salesman_name__iexact='Z.DUTY')
        .values_list('salesman_name', flat=True)
        .distinct()
        .order_by('salesman_name')
    )
    brands = list(
        Items.objects.exclude(item_firm__isnull=True)
        .exclude(item_firm='')
        .values_list('item_firm', flat=True)
        .distinct()
        .order_by('item_firm')
    )

    brand_summaries = []
    rank_rows = []
    grand_sales = Decimal('0')

    if selected_brands:
        inv_headers = (
            SAPARInvoice.objects.filter(scope_q)
            .exclude(salesman_name__iexact='Z.DUTY')
            .filter(posting_date__year=selected_year)
        )
        cm_headers = (
            SAPARCreditMemo.objects.filter(scope_q)
            .exclude(salesman_name__iexact='Z.DUTY')
            .filter(posting_date__year=selected_year)
        )
        if selected_months:
            inv_headers = inv_headers.filter(posting_date__month__in=selected_months)
            cm_headers = cm_headers.filter(posting_date__month__in=selected_months)
        if store_filter in ('HO', 'Others'):
            inv_headers = inv_headers.filter(store=store_filter)
            cm_headers = cm_headers.filter(store=store_filter)
        if selected_salesmen:
            inv_headers = inv_headers.filter(salesman_name__in=selected_salesmen)
            cm_headers = cm_headers.filter(salesman_name__in=selected_salesmen)

        inv_items = SAPARInvoiceItem.objects.filter(
            invoice__in=inv_headers, item__item_firm__in=selected_brands,
        ).exclude(item_code__isnull=True).exclude(item_code='')
        cm_items = SAPARCreditMemoItem.objects.filter(
            credit_memo__in=cm_headers, item__item_firm__in=selected_brands,
        ).exclude(item_code__isnull=True).exclude(item_code='')

        if search_query:
            text_q = (Q(item_code__icontains=search_query)
                      | Q(item_description__icontains=search_query)
                      | Q(upc_code__icontains=search_query))
            inv_items = inv_items.filter(text_q)
            cm_items = cm_items.filter(text_q)

        # brand -> item_code -> aggregates
        data = defaultdict(dict)

        def _accumulate(rows):
            for r in rows:
                brand = r['item__item_firm']
                code = r['item_code']
                d = data[brand].setdefault(code, {
                    'item_code': code,
                    'description': r['description'] or '',
                    'sales': Decimal('0'), 'gp': Decimal('0'), 'qty': Decimal('0'),
                })
                d['sales'] += r['sales'] or Decimal('0')
                d['gp'] += r['gp'] or Decimal('0')
                d['qty'] += r['qty'] or Decimal('0')
                if not d['description'] and r['description']:
                    d['description'] = r['description']

        _accumulate(
            inv_items.values('item__item_firm', 'item_code').annotate(
                sales=_net_value_expr(), gp=_gp_value_expr(), qty=_qty_expr(),
                description=Max('item_description'))
        )
        _accumulate(
            cm_items.values('item__item_firm', 'item_code').annotate(
                sales=_net_value_expr(), gp=_gp_value_expr(), qty=_qty_expr(),
                description=Max('item_description'))
        )

        # Stock on hand (Items.total_available_stock): brand total over ALL of the
        # brand's items, plus a per-item lookup for the ranked lists.
        stock_by_brand = {
            row['item_firm']: row['stock'] or Decimal('0')
            for row in Items.objects.filter(item_firm__in=selected_brands)
            .values('item_firm').annotate(stock=Sum('total_available_stock'))
        }
        ranked_codes = set()
        for brand_items in data.values():
            ranked_codes.update(brand_items.keys())
        item_stock = {
            code: stk
            for code, stk in Items.objects.filter(item_code__in=ranked_codes)
            .values_list('item_code', 'total_available_stock')
        }

        # Per-brand summary + ranked item lists (columns follow selection order)
        ranked = {}
        for brand in selected_brands:
            items = sorted(data.get(brand, {}).values(), key=lambda x: x['sales'], reverse=True)
            ranked[brand] = items
            sales = sum((i['sales'] for i in items), Decimal('0'))
            gp = sum((i['gp'] for i in items), Decimal('0'))
            qty = sum((i['qty'] for i in items), Decimal('0'))
            grand_sales += sales
            brand_summaries.append({
                'brand': brand,
                'sales': sales,
                'gp': gp,
                'gp_pct': _pct(gp, sales),
                'qty': qty,
                'item_count': len(items),
                'stock': stock_by_brand.get(brand, Decimal('0')),
                'avg_rate': (sales / qty) if qty else Decimal('0'),
            })
        for s in brand_summaries:
            s['share'] = _pct(s['sales'], grand_sales)

        # Side-by-side rank table: row i holds each brand's i-th best item
        max_items = max((len(v) for v in ranked.values()), default=0)
        for i in range(max_items):
            cells = []
            for brand in selected_brands:
                items = ranked[brand]
                if i < len(items):
                    it = items[i]
                    cells.append({
                        'item_code': it['item_code'],
                        'description': it['description'],
                        'qty': it['qty'],
                        'stock': item_stock.get(it['item_code']),
                        'sales': it['sales'],
                        'gp': it['gp'],
                        'gp_pct': _pct(it['gp'], it['sales']),
                    })
                else:
                    cells.append(None)
            rank_rows.append({'rank': i + 1, 'cells': cells})

    params = request.GET.copy()
    params.pop('page', None)
    paginator = Paginator(rank_rows, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'calendar_years': calendar_years,
        'selected_year': selected_year,
        'store_filter': store_filter,
        'all_months': [{'num': i + 1, 'name': MONTH_NAMES_SHORT[i]} for i in range(12)],
        'selected_months': selected_months,
        'salesmen': salesmen,
        'selected_salesmen': selected_salesmen,
        'brands': brands,
        'selected_brands': selected_brands,
        'search_query': search_query,
        'page_obj': page_obj,
        'total_ranked_rows': len(rank_rows),
        'page_querystring': params.urlencode(),
        'is_admin': is_admin,
        'brand_summaries': brand_summaries,
        'rank_rows': page_obj.object_list if page_obj else [],
        'grand_sales': grand_sales,
    }
    return render(request, 'salesorders/supplier_brandwise_item_analysis.html', context)
