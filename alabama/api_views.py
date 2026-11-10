"""
Alabama JSON APIs -- kept separate from the Junaid (so app) APIs.

Data source is AlabamaSalesLine (Excel-uploaded sales summary), not the SAP
AR Invoice / Credit Memo tables the Junaid equivalent
(so.purchase_stock_requirement_views.api_item_analysis_totals) reads.
"""
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .item_sync import item_fk
from .models import AlabamaSalesLine
from .views import alabama_salesman_scope_q


def _num(value):
    value = float(value or 0)
    return int(value) if value == int(value) else value


@csrf_exempt
@require_GET
def api_item_analysis_totals(request):
    """
    API endpoint: Alabama item_code with total_qty, ho_qty, others_qty, total_2025, total_2026.
    Same response shape as the Junaid /api/item-analysis-totals/, so a caller can use either.

    Differences from the Junaid API:
    - Credit Memo lines are stored with NEGATIVE quantities in AlabamaSalesLine, so a plain
      SUM over all lines is already net (invoice - credit memo); subtracting again would
      double-count returns.
    - Alabama data has no store split, so ho_qty and others_qty are always 0.

    Optional filter: firm (GET param, repeatable, e.g. ?firm=A&firm=B).
    Response: { "results": [ { "item_code": "...", "total_qty": 10, "ho_qty": 0, "others_qty": 0,
                               "total_2025": 8, "total_2026": 2 }, ... ] }
    """
    firm_list = list(dict.fromkeys(f.strip() for f in request.GET.getlist('firm') if f and f.strip()))

    # alabama_salesman_scope_q errors for an anonymous user, so only scope logged-in callers.
    scope_q = alabama_salesman_scope_q(request.user) if request.user.is_authenticated else Q()
    lines = (AlabamaSalesLine.objects.filter(scope_q)
             .exclude(**{f'{item_fk()}__item_code__isnull': True})
             .exclude(**{f'{item_fk()}__item_code': ''}))
    if firm_list:
        lines = lines.filter(**{f'{item_fk()}__item_firm__in': firm_list})

    zero = Value(0, output_field=DecimalField())
    rows = (lines.values(f'{item_fk()}__item_code')
            .annotate(
                total_qty=Coalesce(Sum('quantity'), zero),
                total_2025=Coalesce(Sum('quantity', filter=Q(posting_date__year=2025)), zero),
                total_2026=Coalesce(Sum('quantity', filter=Q(posting_date__year=2026)), zero),
            )
            .order_by(f'{item_fk()}__item_code'))

    results = [
        {
            'item_code': row[f'{item_fk()}__item_code'],
            'total_qty': _num(row['total_qty']),
            'total_2025': _num(row['total_2025']),
            'total_2026': _num(row['total_2026']),
        }
        for row in rows
    ]
    return JsonResponse({'results': results})


# ---------------------------------------------------------------------------------------------------------------------
# Alabama per-item quotation totals (read-only). Same query parameters and response shapes as the Junaid
# /api/item-quotation-totals/, but reads ONLY Alabama tables (AlabamaSAPQuotation / AlabamaSAPQuotationItem /
# AlabamaItem) -- never so.Items or so.SAPQuotation. Auth: the same shared key (PURCHASE_APP_API_KEY).
# ---------------------------------------------------------------------------------------------------------------------
@csrf_exempt
@require_GET
def api_alabama_item_quotation_totals(request):
    """
    GET /alabama/api/item-quotation-totals/

    Optional params: firm (repeatable / comma list), item_code, year, month (repeatable), start, end (YYYY-MM-DD),
    status=OPEN|CLOSED, feed_brands_only=1 (only brands on the active Alabama feed-brand list),
    group_by=date (+ page, page_size; default 5000, max 20000).
    Value = row_total, else quantity * price. Brand = AlabamaItem.item_firm, else the quotation's brand.
    Cost / profit are never returned.
    """
    import hmac
    from collections import defaultdict
    from decimal import Decimal

    from so.api_item_quotation_totals_views import (
        _configured_key, _request_key, _split, _parse_date, _num as _fnum,
    )

    from .item_sync import brand_is_quotable, load_brand_rules, norm_brand
    from .models import AlabamaItem, AlabamaSAPQuotation, AlabamaSAPQuotationItem

    expected = _configured_key()
    if not expected:
        return JsonResponse({'success': False, 'error': 'API key is not configured on the server'}, status=503)
    if not hmac.compare_digest(_request_key(request), expected):
        return JsonResponse({'success': False, 'error': 'Invalid API key'}, status=401)

    firms = _split(request.GET.getlist('firm'))
    firm_norm = {norm_brand(f) for f in firms if norm_brand(f)}
    item_codes = set(_split(request.GET.getlist('item_code')))
    months = [int(m) for m in _split(request.GET.getlist('month')) if m.isdigit() and 1 <= int(m) <= 12]
    year = request.GET.get('year', '').strip()
    start = _parse_date(request.GET.get('start'))
    end = _parse_date(request.GET.get('end'))
    status = request.GET.get('status', '').strip().upper()
    feed_only = request.GET.get('feed_brands_only', '').strip().lower() in ('1', 'true', 'yes')
    rules = load_brand_rules() if feed_only else None

    qs = AlabamaSAPQuotation.objects.all()
    if year.isdigit():
        qs = qs.filter(posting_date__year=int(year))
    if months:
        qs = qs.filter(posting_date__month__in=months)
    if start:
        qs = qs.filter(posting_date__gte=start)
    if end:
        qs = qs.filter(posting_date__lte=end)
    if status == 'OPEN':
        qs = qs.filter(status__in=['O', 'OPEN', 'Open', 'open'])
    elif status == 'CLOSED':
        qs = qs.filter(status__in=['C', 'CLOSED', 'Closed', 'closed'])

    lines = (AlabamaSAPQuotationItem.objects.filter(quotation__in=qs)
             .exclude(item_no__isnull=True).exclude(item_no=''))
    if item_codes:
        lines = lines.filter(item_no__in=item_codes)
    rows = list(lines.values('item_no', 'description', 'quantity', 'price', 'row_total',
                             'quotation__q_number', 'quotation__posting_date', 'quotation__brand'))

    codes = {str(r['item_no']).strip() for r in rows}
    master = dict(AlabamaItem.objects.filter(item_code__in=codes).values_list('item_code', 'item_firm'))

    def blank():
        return {'quotes': set(), 'qty': Decimal('0'), 'value': Decimal('0')}

    def prepared():
        # Yield (code, brand, qty, value, date, row) for the lines that pass the brand filters.
        for r in rows:
            code = str(r['item_no']).strip()
            brand = str(master.get(code) or r['quotation__brand'] or '').strip()
            if firm_norm and norm_brand(brand) not in firm_norm:
                continue
            if feed_only and not brand_is_quotable(brand, rules):
                continue
            qty = Decimal(str(r['quantity'] or 0))
            value = r['row_total'] if r['row_total'] is not None else qty * Decimal(str(r['price'] or 0))
            yield code, brand, qty, value, r['quotation__posting_date'], r

    if request.GET.get('group_by', '').strip().lower() == 'date':
        daily = {}
        for code, brand, qty, value, date_, r in prepared():
            if not date_:
                continue
            e = daily.setdefault((code, date_), {'description': r['description'] or '', 'brand': brand, **blank()})
            e['quotes'].add(r['quotation__q_number'])
            e['qty'] += qty
            e['value'] += value
            if not e['brand']:
                e['brand'] = brand
        keys = sorted(daily, key=lambda k: (k[1], k[0]), reverse=True)
        try:
            page_size = min(max(int(request.GET.get('page_size', 5000)), 1), 20000)
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page_size, page = 5000, 1
        results = []
        for code, date_ in keys[(page - 1) * page_size: page * page_size]:
            e = daily[(code, date_)]
            results.append({
                'item_code': code, 'description': e['description'], 'brand': e['brand'],
                'date': date_.isoformat(), 'year': date_.year, 'month': date_.month,
                'quoted_qty': _fnum(e['qty']), 'quoted_value': _fnum(e['value']),
                'quotation_count': len(e['quotes']),
            })
        return JsonResponse({'success': True, 'group_by': 'date', 'total': len(keys), 'page': page,
                             'page_size': page_size, 'has_more': page * page_size < len(keys), 'results': results})

    items = {}
    for code, brand, qty, value, date_, r in prepared():
        e = items.get(code)
        if e is None:
            e = items[code] = {'brand': brand, 'description': r['description'] or '', 'last': date_,
                               'total': blank(), 'years': defaultdict(blank)}
        elif date_ and (e['last'] is None or date_ >= e['last']):
            e['last'] = date_
            if r['description']:
                e['description'] = r['description']
        if not e['brand']:
            e['brand'] = brand
        buckets = [e['total']] + ([e['years'][date_.year]] if date_ else [])
        for b in buckets:
            b['quotes'].add(r['quotation__q_number'])
            b['qty'] += qty
            b['value'] += value

    results = []
    for code in sorted(items):
        e = items[code]
        results.append({
            'item_code': code, 'description': e['description'], 'brand': e['brand'],
            'quotation_count': len(e['total']['quotes']),
            'quoted_qty': _fnum(e['total']['qty']), 'quoted_value': _fnum(e['total']['value']),
            'last_quoted_date': e['last'].isoformat() if e['last'] else None,
            'years': {str(y): {'quotation_count': len(b['quotes']), 'quoted_qty': _fnum(b['qty']),
                               'quoted_value': _fnum(b['value'])} for y, b in sorted(e['years'].items())},
        })
    return JsonResponse({'success': True, 'count': len(results), 'results': results})
