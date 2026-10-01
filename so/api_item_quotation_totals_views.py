"""
API: per-item quotation totals for other apps (e.g. the Purchase app)
=====================================================================
GET /api/item-quotation-totals/

Same quotation rules as Brandwise Quotation Analysis
(saparinvoices/brandwise-quotation-analysis/):
  * data: SAPQuotation / SAPQuotationItem
  * value: row_total, else qty * price
  * brand: Items.item_firm, else the quotation's brand
  * quotation_count: number of distinct quotation numbers that contain the item
Read-only. Cost / GP are deliberately not exposed.

Auth: a shared key, sent as header `X-API-Key: <key>` (or `Authorization: Bearer <key>`,
or `?api_key=`). The key is read from the PURCHASE_APP_API_KEY setting / environment
variable; if it is not configured the endpoint refuses every request.

Query params (all optional):
  firm=A&firm=B  or  firm=A,B    brand filter (item master brand)
  item_code=1,2  or repeated     only these item codes
  year=2026                      only this year
  month=1&month=2                only these months
  start=YYYY-MM-DD  end=YYYY-MM-DD
  status=OPEN|CLOSED             only quotations with this status
  group_by=date                  one row per item per quotation date (item_code, date, year,
                                 month, quoted_qty, quoted_value, quotation_count), newest
                                 first, paged with page / page_size (default 5000, max 20000).
                                 Without it: one row per item with totals and a per-year split.
"""
import hmac
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .models import Items, SAPQuotation, SAPQuotationItem


def _configured_key():
    return (getattr(settings, 'PURCHASE_APP_API_KEY', '') or os.getenv('PURCHASE_APP_API_KEY', '')).strip()


def _request_key(request):
    key = request.headers.get('X-API-Key', '').strip()
    if not key:
        auth = request.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            key = auth[7:].strip()
    return key or request.GET.get('api_key', '').strip()


def _split(values):
    """Accept repeated params and comma-separated values."""
    out = []
    for raw in values:
        out.extend(p.strip() for p in str(raw).split(',') if p.strip())
    return list(dict.fromkeys(out))


def _parse_date(raw):
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _num(x):
    return float(x) if x is not None else 0.0


@csrf_exempt
@require_GET
def api_item_quotation_totals(request):
    expected = _configured_key()
    if not expected:
        return JsonResponse({'success': False, 'error': 'API key is not configured on the server'}, status=503)
    if not hmac.compare_digest(_request_key(request), expected):
        return JsonResponse({'success': False, 'error': 'Invalid API key'}, status=401)

    # Same helper the brandwise page uses for the quotation-level brand filter.
    from .views import _sap_quotation_firm_filter_q

    firms = _split(request.GET.getlist('firm'))
    item_codes = set(_split(request.GET.getlist('item_code')))
    months = []
    for m in _split(request.GET.getlist('month')):
        if m.isdigit() and 1 <= int(m) <= 12:
            months.append(int(m))
    year = request.GET.get('year', '').strip()
    start = _parse_date(request.GET.get('start'))
    end = _parse_date(request.GET.get('end'))
    status = request.GET.get('status', '').strip().upper()

    qs = SAPQuotation.objects.all()
    if year.isdigit():
        qs = qs.filter(posting_date__year=int(year))
    if months:
        qs = qs.filter(posting_date__month__in=months)
    if start:
        qs = qs.filter(posting_date__gte=start)
    if end:
        qs = qs.filter(posting_date__lte=end)
    if status in ('OPEN', 'CLOSED'):
        qs = qs.filter(status=status)
    if firms:
        q_brand = _sap_quotation_firm_filter_q(firms)
        if q_brand is not None:
            qs = qs.filter(q_brand).distinct()

    lines = SAPQuotationItem.objects.filter(quotation__in=qs).exclude(item_no__isnull=True).exclude(item_no='')
    if item_codes:
        lines = lines.filter(item_no__in=item_codes)
    lines = lines.values(
        'item_no', 'description', 'quantity', 'price', 'row_total',
        'quotation__q_number', 'quotation__posting_date', 'quotation__brand',
    )

    rows = list(lines)
    codes = {str(r['item_no']).strip() for r in rows}
    master = {
        r['item_code']: r['item_firm']
        for r in Items.objects.filter(item_code__in=codes).values('item_code', 'item_firm')
    }
    firm_set = {f.lower() for f in firms}

    def blank():
        return {'quotes': set(), 'qty': Decimal('0'), 'value': Decimal('0')}

    # ── group_by=date: one row per item per quotation date, so the caller can
    #    filter or sum by month / year itself later ──
    if request.GET.get('group_by', '').strip().lower() == 'date':
        daily = {}
        for r in rows:
            date_ = r['quotation__posting_date']
            if not date_:
                continue
            code = str(r['item_no']).strip()
            brand = str(master.get(code) or r['quotation__brand'] or '').strip()
            if firm_set and brand.lower() not in firm_set:
                continue
            qty = Decimal(str(r['quantity'] or 0))
            value = r['row_total'] if r['row_total'] is not None else (qty * Decimal(str(r['price'] or 0)))
            entry = daily.get((code, date_))
            if entry is None:
                entry = daily[(code, date_)] = {
                    'description': r['description'] or '', 'brand': brand, **blank(),
                }
            entry['quotes'].add(r['quotation__q_number'])
            entry['qty'] += qty
            entry['value'] += value
            if not entry['brand']:
                entry['brand'] = brand

        keys = sorted(daily, key=lambda k: (k[1], k[0]), reverse=True)  # newest date first
        try:
            page_size = min(max(int(request.GET.get('page_size', 5000)), 1), 20000)
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page_size, page = 5000, 1
        chunk = keys[(page - 1) * page_size: page * page_size]
        results = []
        for code, date_ in chunk:
            e = daily[(code, date_)]
            results.append({
                'item_code': code,
                'description': e['description'],
                'brand': e['brand'],
                'date': date_.isoformat(),
                'year': date_.year,
                'month': date_.month,
                'quoted_qty': _num(e['qty']),
                'quoted_value': _num(e['value']),
                'quotation_count': len(e['quotes']),
            })
        return JsonResponse({
            'success': True, 'group_by': 'date', 'total': len(keys), 'page': page,
            'page_size': page_size, 'has_more': page * page_size < len(keys), 'results': results,
        })

    items = {}
    for r in rows:
        code = str(r['item_no']).strip()
        brand = str(master.get(code) or r['quotation__brand'] or '').strip()
        if firm_set and brand.lower() not in firm_set:
            continue
        qty = Decimal(str(r['quantity'] or 0))
        value = r['row_total'] if r['row_total'] is not None else (qty * Decimal(str(r['price'] or 0)))
        date_ = r['quotation__posting_date']

        entry = items.get(code)
        if entry is None:
            entry = items[code] = {
                'item_code': code, 'brand': brand, 'description': r['description'] or '',
                'last_quoted_date': date_, 'total': blank(), 'years': defaultdict(blank),
            }
        elif date_ and (entry['last_quoted_date'] is None or date_ >= entry['last_quoted_date']):
            entry['last_quoted_date'] = date_
            if r['description']:
                entry['description'] = r['description']
        if not entry['brand']:
            entry['brand'] = brand

        buckets = [entry['total']]
        if date_:
            buckets.append(entry['years'][date_.year])
        for b in buckets:
            b['quotes'].add(r['quotation__q_number'])
            b['qty'] += qty
            b['value'] += value

    results = []
    for code in sorted(items):
        e = items[code]
        results.append({
            'item_code': code,
            'description': e['description'],
            'brand': e['brand'],
            'quotation_count': len(e['total']['quotes']),
            'quoted_qty': _num(e['total']['qty']),
            'quoted_value': _num(e['total']['value']),
            'last_quoted_date': e['last_quoted_date'].isoformat() if e['last_quoted_date'] else None,
            'years': {
                str(y): {
                    'quotation_count': len(b['quotes']),
                    'quoted_qty': _num(b['qty']),
                    'quoted_value': _num(b['value']),
                }
                for y, b in sorted(e['years'].items())
            },
        })

    return JsonResponse({'success': True, 'count': len(results), 'results': results})
