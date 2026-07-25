"""
Alabama Credit Note (Credit Memo) Analysis - itemwise returns analysis.
Mirrors Junaid's credit_memo_analysis_views.itemwise_credit_memo_analysis,
but built on AlabamaSalesLine (a flat uploaded-Excel line table) rather than
SAPARCreditMemo/SAPARCreditMemoItem (SAP-synced header+item tables) -- so
there's no store/category/remarks support here, since that data doesn't
exist for Alabama.
"""
import logging
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q, DecimalField, Value
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.shortcuts import render

from .models import AlabamaSalesLine
from .views import alabama_salesman_scope_q, normalize_alabama_salesman
from .item_analysis_views import _salesman_filter_q, _is_null_item_code

logger = logging.getLogger(__name__)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


@login_required
def credit_note_analysis(request):
    """Itemwise Credit Note Analysis - which items are returned most, Alabama."""
    from so.models import Items

    search_query = request.GET.get('q', '').strip()
    salesmen_filter = [s for s in request.GET.getlist('salesman') if s.strip()]
    start_date = _parse_date(request.GET.get('start', ''))
    end_date = _parse_date(request.GET.get('end', ''))

    qs = AlabamaSalesLine.objects.filter(document_type='Credit Memo').select_related('item', 'customer')
    scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
    qs = qs.filter(scope_q)

    if salesmen_filter:
        qs = qs.filter(_salesman_filter_q(salesmen_filter, field='sales_employee'))
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    if search_query:
        search_item_ids = set(
            Items.objects.filter(
                Q(item_code__icontains=search_query)
                | Q(item_description__icontains=search_query)
                | Q(item_upvc__icontains=search_query)
            ).values_list('pk', flat=True)
        )
        qs = qs.filter(item_id__in=search_item_ids)

    # ── Item-level aggregates ────────────────────────────────────────────
    item_aggs = qs.values('item').annotate(
        total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
        total_value=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
        credit_memo_count=Count('document_number', distinct=True),
        customer_count=Count('customer', distinct=True),
    )

    items_by_id = {row['item']: row for row in item_aggs if row['item']}
    items_info = {
        it.pk: it
        for it in Items.objects.filter(pk__in=items_by_id.keys())
    }

    items_list = []
    for item_id, row in items_by_id.items():
        qty = abs(row['total_quantity'] or 0)
        if not qty:
            continue
        item = items_info.get(item_id)
        if not item:
            continue
        code = item.item_code or ''
        if not code or _is_null_item_code(code):
            continue
        items_list.append({
            'item_id': item_id,
            'item_code': code,
            'item_description': item.item_description or 'Unknown',
            'upc_code': getattr(item, 'item_upvc', '') or '',
            'total_quantity': qty,
            'total_value': abs(row['total_value'] or Decimal('0')),
            'credit_memo_count': row['credit_memo_count'],
            'customer_count': row['customer_count'],
        })

    items_list.sort(key=lambda x: x['total_quantity'], reverse=True)

    grand_total_quantity = sum(i['total_quantity'] for i in items_list)
    grand_total_value = sum(i['total_value'] for i in items_list)
    grand_total_credit_memos = qs.values('document_number').distinct().count()
    grand_total_customers = qs.values('customer').distinct().count()

    total_count = len(items_list)
    page_size = 100
    paginator = Paginator(items_list, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # ── Customer drill-down, current page items only ─────────────────────
    page_item_ids = [i['item_id'] for i in page_obj]
    customers_by_item = {}
    if page_item_ids:
        cust_aggs = (
            qs.filter(item_id__in=page_item_ids)
            .values('item_id', 'customer_id', 'customer__customer_name', 'customer__customer_code')
            .annotate(
                total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
                total_value=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
                credit_memo_count=Count('document_number', distinct=True),
            )
        )
        doc_rows = (
            qs.filter(item_id__in=page_item_ids)
            .values('item_id', 'customer_id', 'document_number')
            .distinct()
        )
        doc_lookup = {}
        for row in doc_rows:
            doc_lookup.setdefault((row['item_id'], row['customer_id']), set()).add(row['document_number'])

        for row in cust_aggs:
            qty = abs(row['total_quantity'] or 0)
            if not qty:
                continue
            key = (row['item_id'], row['customer_id'])
            customers_by_item.setdefault(row['item_id'], []).append({
                'customer_name': row['customer__customer_name'] or 'Unknown',
                'customer_code': row['customer__customer_code'] or '',
                'total_quantity': int(qty),
                'total_value': abs(row['total_value'] or Decimal('0')),
                'credit_memo_count': row['credit_memo_count'],
                'credit_memo_numbers': sorted(doc_lookup.get(key, set())),
            })
        for item_id in customers_by_item:
            customers_by_item[item_id].sort(key=lambda x: x['total_quantity'], reverse=True)

    for item in page_obj:
        item['customers'] = customers_by_item.get(item['item_id'], [])

    salesmen_raw = (
        AlabamaSalesLine.objects.filter(document_type='Credit Memo')
        .filter(scope_q)
        .exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
    )
    all_salesmen = sorted(set(normalize_alabama_salesman(s) or s for s in salesmen_raw if s))

    qd = request.GET.copy()
    if 'page' in qd:
        del qd['page']
    query_string = qd.urlencode()

    context = {
        'query_string': query_string,
        'items': page_obj,
        'page_obj': page_obj,
        'total_count': total_count,
        'grand_total_quantity': grand_total_quantity,
        'grand_total_value': grand_total_value,
        'grand_total_credit_memos': grand_total_credit_memos,
        'grand_total_customers': grand_total_customers,
        'salesmen': all_salesmen,
        'filters': {
            'q': search_query,
            'salesman': salesmen_filter,
            'start': request.GET.get('start', ''),
            'end': request.GET.get('end', ''),
        },
    }
    return render(request, 'alabama/credit_note_analysis.html', context)
