"""
Customer-wise Item Sold — Month-wise Analysis
==============================================
Pivot table:
  - Rows    = customers (collapsed by default; expand to reveal their items)
  - Nested  = items bought by that customer, each with 3 sub-rows: Qty / Avg Rate / GP%
  - Columns = months from Jan 2025 through the current month
  - Filters = Salesman (multi-select), Customer search, Item search

Net qty/sales/GP = AR Invoice line values + AR Credit Memo line values, summed
directly (credit-memo lines are stored negative, so summing nets returns
against sales) — same convention as Brandwise Sales Analysis.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum, Q
from django.shortcuts import render

from .models import SAPARInvoice, SAPARInvoiceItem, SAPARCreditMemo, SAPARCreditMemoItem
from .sap_salesorder_views import salesman_scope_q_salesorder
from .brandwise_sales_analysis_views import (
    _net_value_expr, _gp_value_expr, _pct, _user_is_admin, MONTH_NAMES_SHORT,
)

# Some customers carry hundreds of distinct items — an unbounded item x month x
# metric pivot per customer would balloon page size into the tens of MB. Cap the
# items shown per customer to the highest-value ones; the Item filter can be used
# to search across all of a customer's items.
MAX_ITEMS_PER_CUSTOMER = 40


def _month_columns():
    """Month columns paired by month name across years: Jan-25, Jan-26, Feb-25,
    Feb-26, ... through Dec-25 (2026 months beyond the current month are omitted
    since they have no data yet)."""
    today = date.today()
    valid_year_months = set()
    y, m = 2025, 1
    while (y, m) <= (today.year, today.month):
        valid_year_months.add((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    years = sorted({yr for yr, _ in valid_year_months})

    months = []
    for month_num in range(1, 13):
        for yr in years:
            if (yr, month_num) in valid_year_months:
                months.append({
                    'year': yr, 'month': month_num,
                    'label': f"{MONTH_NAMES_SHORT[month_num - 1]}-{str(yr)[2:]}",
                })
    return months


@login_required
def customer_item_monthwise_analysis(request):
    is_admin = _user_is_admin(request.user)
    months = _month_columns()
    n_months = len(months)
    month_index = {(m['year'], m['month']): i for i, m in enumerate(months)}
    years = sorted({m['year'] for m in months})
    year_col_indices = {yr: [i for i, m in enumerate(months) if m['year'] == yr] for yr in years}

    selected_salesmen = [s.strip() for s in request.GET.getlist('salesman') if s.strip()]
    customer_search = request.GET.get('customer', '').strip()
    item_search = request.GET.get('item', '').strip()

    start_date = date(2025, 1, 1)
    end_date = date.today()

    scope_q = salesman_scope_q_salesorder(request.user)
    inv_headers = (
        SAPARInvoice.objects.filter(scope_q)
        .exclude(salesman_name__iexact='Z.DUTY')
        .filter(posting_date__gte=start_date, posting_date__lte=end_date)
    )
    cm_headers = (
        SAPARCreditMemo.objects.filter(scope_q)
        .exclude(salesman_name__iexact='Z.DUTY')
        .filter(posting_date__gte=start_date, posting_date__lte=end_date)
    )
    if selected_salesmen:
        inv_headers = inv_headers.filter(salesman_name__in=selected_salesmen)
        cm_headers = cm_headers.filter(salesman_name__in=selected_salesmen)

    inv_items = (
        SAPARInvoiceItem.objects.filter(invoice__in=inv_headers)
        .exclude(invoice__customer_code__isnull=True).exclude(invoice__customer_code='')
        .exclude(item_code__isnull=True).exclude(item_code='')
    )
    cm_items = (
        SAPARCreditMemoItem.objects.filter(credit_memo__in=cm_headers)
        .exclude(credit_memo__customer_code__isnull=True).exclude(credit_memo__customer_code='')
        .exclude(item_code__isnull=True).exclude(item_code='')
    )
    if customer_search:
        inv_items = inv_items.filter(
            Q(invoice__customer_code__icontains=customer_search) |
            Q(invoice__customer_name__icontains=customer_search)
        )
        cm_items = cm_items.filter(
            Q(credit_memo__customer_code__icontains=customer_search) |
            Q(credit_memo__customer_name__icontains=customer_search)
        )
    if item_search:
        inv_items = inv_items.filter(
            Q(item_code__icontains=item_search) | Q(item_description__icontains=item_search)
        )
        cm_items = cm_items.filter(
            Q(item_code__icontains=item_search) | Q(item_description__icontains=item_search)
        )

    # customer_code -> {'name': str, 'items': {item_code: {'description', 'qty':[], 'amt':[], 'gp':[]}}}
    customers = defaultdict(lambda: {'name': '', 'items': {}})

    def _item_bucket(cust, item_code, description):
        items = cust['items']
        bucket = items.get(item_code)
        if bucket is None:
            bucket = {
                'description': description or '',
                'qty': [Decimal('0')] * n_months,
                'amt': [Decimal('0')] * n_months,
                'gp': [Decimal('0')] * n_months,
            }
            items[item_code] = bucket
        elif description and not bucket['description']:
            bucket['description'] = description
        return bucket

    def _accumulate(rows, code_key, name_key, year_key, month_key):
        for r in rows:
            code = (r[code_key] or '').strip()
            if not code:
                continue
            idx = month_index.get((r[year_key], r[month_key]))
            if idx is None:
                continue
            cust = customers[code]
            if not cust['name']:
                cust['name'] = r.get(name_key) or code
            bucket = _item_bucket(cust, r['item_code'], r.get('item_description'))
            bucket['qty'][idx] += r['qty'] or Decimal('0')
            bucket['amt'][idx] += r['sales'] or Decimal('0')
            bucket['gp'][idx] += r['gp'] or Decimal('0')

    inv_rows = (
        inv_items
        .values('invoice__customer_code', 'invoice__customer_name', 'item_code', 'item_description',
                 'invoice__posting_date__year', 'invoice__posting_date__month')
        .annotate(qty=Sum('quantity'), sales=_net_value_expr(), gp=_gp_value_expr())
    )
    _accumulate(inv_rows, 'invoice__customer_code', 'invoice__customer_name',
                'invoice__posting_date__year', 'invoice__posting_date__month')

    cm_rows = (
        cm_items
        .values('credit_memo__customer_code', 'credit_memo__customer_name', 'item_code', 'item_description',
                 'credit_memo__posting_date__year', 'credit_memo__posting_date__month')
        .annotate(qty=Sum('quantity'), sales=_net_value_expr(), gp=_gp_value_expr())
    )
    _accumulate(cm_rows, 'credit_memo__customer_code', 'credit_memo__customer_name',
                'credit_memo__posting_date__year', 'credit_memo__posting_date__month')

    # ── Build display rows ───────────────────────────────────────
    customer_rows = []
    for code, cust in customers.items():
        item_rows = []
        cust_total_amt = Decimal('0')
        cust_total_qty = Decimal('0')
        cust_total_gp = Decimal('0')
        for item_code, d in cust['items'].items():
            total_qty = sum(d['qty'], Decimal('0'))
            total_amt = sum(d['amt'], Decimal('0'))
            total_gp = sum(d['gp'], Decimal('0'))
            if not total_qty and not total_amt:
                continue
            cells = []
            for i in range(n_months):
                q, a, g = d['qty'][i], d['amt'][i], d['gp'][i]
                cells.append({
                    'qty': q,
                    'rate': (a / q) if q else Decimal('0'),
                    'gp_pct': _pct(g, a),
                })
            # Per-year totals (2025 and 2026 kept separate, not blended together) —
            # each year's avg rate/GP% is computed from that year's own qty/amt sums.
            year_totals = []
            for yr in years:
                idxs = year_col_indices[yr]
                yr_qty = sum((d['qty'][i] for i in idxs), Decimal('0'))
                yr_amt = sum((d['amt'][i] for i in idxs), Decimal('0'))
                yr_gp = sum((d['gp'][i] for i in idxs), Decimal('0'))
                year_totals.append({
                    'year': yr,
                    'qty': yr_qty,
                    'rate': (yr_amt / yr_qty) if yr_qty else Decimal('0'),
                    'gp_pct': _pct(yr_gp, yr_amt),
                })
            item_rows.append({
                'item_code': item_code,
                'description': d['description'],
                'cells': cells,
                'total_qty': total_qty,
                'total_amt': total_amt,
                'year_totals': year_totals,
            })
            cust_total_amt += total_amt
            cust_total_qty += total_qty
            cust_total_gp += total_gp
        if not item_rows:
            continue
        item_rows.sort(key=lambda r: r['total_amt'], reverse=True)
        full_item_count = len(item_rows)
        shown_items = item_rows[:MAX_ITEMS_PER_CUSTOMER]
        customer_rows.append({
            'customer_code': code,
            'customer_name': cust['name'],
            'items': shown_items,
            'item_count': full_item_count,
            'items_truncated': full_item_count > len(shown_items),
            'hidden_item_count': full_item_count - len(shown_items),
            'total_qty': cust_total_qty,
            'total_amt': cust_total_amt,
            'gp_pct': _pct(cust_total_gp, cust_total_amt),
        })
    customer_rows.sort(key=lambda r: r['total_amt'], reverse=True)

    grand_total_amt = sum((r['total_amt'] for r in customer_rows), Decimal('0'))
    grand_total_qty = sum((r['total_qty'] for r in customer_rows), Decimal('0'))

    # ── Pagination (customers) ────────────────────────────────────
    page_size = 15
    paginator = Paginator(customer_rows, page_size)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    # ── Salesman list for the filter dropdown (scoped) ────────────
    salesmen = list(
        SAPARInvoice.objects.filter(scope_q)
        .exclude(salesman_name__isnull=True).exclude(salesman_name='')
        .exclude(salesman_name__iexact='Z.DUTY')
        .values_list('salesman_name', flat=True).distinct().order_by('salesman_name')
    )

    context = {
        'months': months,
        'years': years,
        'customers': page_obj,
        'page_obj': page_obj,
        'total_customers': len(customer_rows),
        'grand_total_amt': grand_total_amt,
        'grand_total_qty': grand_total_qty,
        'is_admin': is_admin,
        'salesmen': salesmen,
        'selected_salesmen': selected_salesmen,
        'customer_search': customer_search,
        'item_search': item_search,
        'period_label': f"Jan 2025 – {MONTH_NAMES_SHORT[end_date.month - 1]} {end_date.year}",
    }
    return render(request, 'salesorders/customer_item_monthwise_analysis.html', context)
