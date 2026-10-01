"""
Brandwise Quotation Matrix
==========================
Item x year matrix of quotation data, laid out like Item Analysis
(saparinvoices/item-analysis/).

The numbers use exactly the same rules as Brandwise Quotation Analysis
(saparinvoices/brandwise-quotation-analysis/):
  * data: SAPQuotation / SAPQuotationItem, limited by salesman_scope_q
  * value: row_total, else qty * price
  * brand: Items.item_firm, else the quotation's brand
  * GP: value - (Items.item_cost * qty)
Only the presentation (item x year grid) is different. This module does not
touch the existing brandwise view.
"""
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from .brandwise_sales_analysis_views import _user_is_admin
from .models import Items, SAPQuotation

FIRST_YEAR = 2024
PAGE_SIZE = 1000


def _parse_date(raw):
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


@login_required
def brandwise_quotation_matrix(request):
    # Imported here: views.py is large and already imports a lot of modules.
    from .views import salesman_scope_q, _sap_quotation_firm_filter_q

    current_year = date.today().year
    years = list(range(current_year, FIRST_YEAR - 1, -1))
    is_admin = _user_is_admin(request.user)

    search_query = request.GET.get('q', '').strip()
    selected_salesmen = [s.strip() for s in request.GET.getlist('salesman') if s.strip()]
    selected_brands = [b.strip() for b in request.GET.getlist('firm') if b.strip()]
    month_filter = [m for m in request.GET.getlist('month') if m.strip()]
    start_raw = request.GET.get('start', '').strip()
    end_raw = request.GET.get('end', '').strip()

    scope_qs = SAPQuotation.objects.filter(salesman_scope_q(request.user))
    qs = scope_qs.filter(posting_date__year__gte=FIRST_YEAR, posting_date__year__lte=current_year)

    month_nums = []
    for m in month_filter:
        try:
            n = int(m)
            if 1 <= n <= 12:
                month_nums.append(n)
        except ValueError:
            continue
    if month_nums:
        qs = qs.filter(posting_date__month__in=month_nums)

    start_date = _parse_date(start_raw)
    end_date = _parse_date(end_raw)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    if selected_salesmen:
        qs = qs.filter(salesman_name__in=selected_salesmen)
    if selected_brands:
        q_brand = _sap_quotation_firm_filter_q(selected_brands)
        if q_brand is not None:
            qs = qs.filter(q_brand).distinct()

    quotes = qs.prefetch_related('items')
    all_item_codes = {
        str(item.item_no).strip()
        for quote in quotes
        for item in quote.items.all()
        if item.item_no and str(item.item_no).strip()
    }
    item_lookup = {
        row['item_code']: row
        for row in Items.objects.filter(item_code__in=all_item_codes).values(
            'item_code', 'item_cost', 'item_firm', 'total_available_stock', 'item_price'
        )
    }

    selected_brand_set = {b.lower() for b in selected_brands}
    search_lower = search_query.lower()

    def blank_year():
        return {
            'total_sales': Decimal('0'),
            'total_gp': Decimal('0'),
            'total_quantity': Decimal('0'),
            'quotation_numbers': set(),
        }

    # TOTAL-row figures follow the brandwise page's calendar totals exactly:
    #  * a quotation counts only when its matching lines add up to a non-zero value
    #  * days after today in the current month are left out (the calendar stops at today)
    # Item rows are not affected by either rule (same as the brandwise item table).
    today = date.today()
    total_acc = {
        year: {'value': Decimal('0'), 'gp': Decimal('0'), 'quotes': 0} for year in years
    }

    # item_code -> {description, latest date, years{year: totals}}
    item_data = {}
    for quote in quotes:
        if not quote.posting_date:
            continue
        year = quote.posting_date.year
        quote_value = Decimal('0')
        quote_gp = Decimal('0')
        for item in quote.items.all():
            code = str(item.item_no).strip() if item.item_no else ''
            if not code:
                continue
            master = item_lookup.get(code) or {}
            item_brand = str(master.get('item_firm') or quote.brand or '').strip()
            if selected_brand_set and item_brand.lower() not in selected_brand_set:
                continue
            description = item.description or ''
            if search_lower and search_lower not in code.lower() and search_lower not in description.lower():
                continue

            qty = Decimal(str(item.quantity or 0))
            row_total = item.row_total if item.row_total is not None else (qty * Decimal(str(item.price or 0)))
            item_cost = Decimal(str(master.get('item_cost') or 0))

            entry = item_data.get(code)
            if entry is None:
                entry = item_data[code] = {
                    'item_code': code,
                    'item_description': description,
                    'brand': item_brand,
                    'latest_date': quote.posting_date,
                    'years': defaultdict(blank_year),
                }
            elif description and quote.posting_date >= entry['latest_date']:
                entry['item_description'] = description
                entry['latest_date'] = quote.posting_date
            if not entry['brand']:
                entry['brand'] = item_brand

            y = entry['years'][year]
            y['total_sales'] += row_total
            y['total_gp'] += row_total - (item_cost * qty)
            y['total_quantity'] += qty
            y['quotation_numbers'].add(quote.q_number)
            quote_value += row_total
            quote_gp += row_total - (item_cost * qty)

        in_calendar = not (
            quote.posting_date.year == today.year
            and quote.posting_date.month == today.month
            and quote.posting_date.day > today.day
        )
        if quote_value and in_calendar and year in total_acc:
            total_acc[year]['value'] += quote_value
            total_acc[year]['gp'] += quote_gp
            total_acc[year]['quotes'] += 1

    def finish(totals):
        # "total_sales" is the quoted value (name kept for the shared table keys).
        return {
            'total_sales': totals['total_sales'],
            'total_gp': totals['total_gp'],
            'total_quantity': totals['total_quantity'],
            'quotation_count': len(totals['quotation_numbers']),
        }

    items_list = []
    year_sums = {year: blank_year() for year in years}
    for entry in item_data.values():
        year_list = []
        for year in years:
            raw = entry['years'].get(year) or blank_year()
            year_list.append(finish(raw))
            for k in ('total_sales', 'total_gp', 'total_quantity'):
                year_sums[year][k] += raw[k]
            year_sums[year]['quotation_numbers'] |= raw['quotation_numbers']
        master = item_lookup.get(entry['item_code']) or {}
        items_list.append({
            'item_code': entry['item_code'],
            'item_description': entry['item_description'] or 'Unknown',
            'brand': entry['brand'],
            'year_list': year_list,
            'total_available_stock': master.get('total_available_stock') or Decimal('0'),
            'item_price': Decimal(str(master['item_price'])) if master.get('item_price') is not None else None,
        })

    items_list.sort(key=lambda r: sum(y['total_sales'] for y in r['year_list']), reverse=True)
    totals_list = []
    for year in years:
        totals = finish(year_sums[year])
        totals['total_sales'] = total_acc[year]['value']
        totals['total_gp'] = total_acc[year]['gp']
        totals['quotation_count'] = total_acc[year]['quotes']
        totals_list.append(totals)

    stock_total = sum((r['total_available_stock'] for r in items_list), Decimal('0'))
    item_price_total = sum((r['item_price'] for r in items_list if r['item_price'] is not None), Decimal('0'))

    paginator = Paginator(items_list, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))

    salesmen = (
        SAPQuotation.objects.filter(salesman_scope_q(request.user))
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
        .order_by('salesman_name')
    )
    # Same brand list as the brandwise page: quotation brands + item-master firms of quoted items.
    brand_chunks = list(
        scope_qs.exclude(brand__isnull=True).exclude(brand='').values_list('brand', flat=True)
    )
    brand_chunks.extend(
        Items.objects.filter(
            item_code__in=scope_qs.exclude(items__item_no__isnull=True)
            .exclude(items__item_no='')
            .values_list('items__item_no', flat=True)
        )
        .exclude(item_firm__isnull=True)
        .exclude(item_firm='')
        .values_list('item_firm', flat=True)
        .distinct()
    )
    firms = sorted({str(x).strip() for x in brand_chunks if x and str(x).strip()}, key=str.lower)

    context = {
        'items': page_obj,
        'page_obj': page_obj,
        'total_count': len(items_list),
        'years': years,
        'is_admin': is_admin,
        'salesmen': salesmen,
        'firms': firms,
        'totals_list': totals_list,
        'stock_total': stock_total,
        'item_price_total': item_price_total,
        'filters': {
            'q': search_query,
            'salesman': selected_salesmen,
            'firm': selected_brands,
            'month': month_filter,
            'start': start_raw,
            'end': end_raw,
        },
    }
    return render(request, 'salesorders/brandwise_quotation_matrix.html', context)
