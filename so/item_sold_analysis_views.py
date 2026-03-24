"""
Item Sold Analysis Views
Firm-wise analysis showing Qty Sold 2025, Qty Sold 2026, and Customer Count per item.
Uses AR Invoices and Credit Memos. Same structure as Item Quoted Analysis but for sold data.
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Q, Sum, Count, Max, Value, DecimalField, F, Case, When
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from decimal import Decimal
from collections import defaultdict
from datetime import date, timedelta
import logging

from .models import Items, ProposedQuantity, SAPPurchaseOrderItem, SAPARInvoice, SAPARInvoiceItem, SAPARCreditMemo, SAPARCreditMemoItem
from .sap_salesorder_views import salesman_scope_q_salesorder

logger = logging.getLogger(__name__)

IMPORT_ORDERED_API_URL = 'https://purchase.junaidworld.com/api/item-totals/'


def _get_import_ordered_lookup(item_codes):
    """Fetch totalqty_ordered per item from purchase API."""
    lookup = {code: 0 for code in item_codes}
    try:
        import requests
        resp = requests.get(IMPORT_ORDERED_API_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return lookup
        for row in data:
            itemcode = (row.get('itemcode') or row.get('item_code') or '').strip()
            if itemcode in lookup:
                try:
                    lookup[itemcode] = int(row.get('totalqty_ordered', 0) or 0)
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"Could not fetch import ordered: {e}")
    return lookup


def _open_po_row_status_q():
    """Q for open line status on SAPPurchaseOrderItem."""
    return Q(row_status__iexact="open") | Q(row_status__iexact="o") | Q(row_status__iexact="OPEN") | Q(row_status__iexact="O")


def _get_open_po_lookup(item_codes):
    """Open PO qty per item (last 6 months)."""
    if not item_codes:
        return {}
    six_months_ago = date.today() - timedelta(days=180)
    qs = (
        SAPPurchaseOrderItem.objects.filter(_open_po_row_status_q())
        .filter(purchaseorder__posting_date__gte=six_months_ago)
        .filter(item_no__in=item_codes)
        .values('item_no')
        .annotate(
            total_qty=Sum(Coalesce(F('remaining_open_quantity'), F('quantity'), Value(0, output_field=DecimalField())))
        )
    )
    return {row['item_no']: _safe_float(row['total_qty']) for row in qs}


def _safe_float(x):
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _get_customer_details_for_sold_items(invoice_items_qs, creditmemo_items_qs, item_codes):
    """
    Get customer details for sold items from invoices and credit memos.
    Returns dict: {item_code: [customer_details]}
    """
    from .models import SAPARInvoice, SAPARCreditMemo

    result = defaultdict(list)

    # Invoice customer aggregates
    inv_aggs = list(
        invoice_items_qs.filter(item_code__in=item_codes)
        .exclude(invoice__customer_code__isnull=True)
        .exclude(invoice__customer_code='')
        .values('item_code', 'invoice__customer_code', 'invoice__customer_name')
        .annotate(
            total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
            qty_2025=Coalesce(Sum('quantity', filter=Q(invoice__posting_date__year=2025)), Value(0, output_field=DecimalField())),
            qty_2026=Coalesce(Sum('quantity', filter=Q(invoice__posting_date__year=2026)), Value(0, output_field=DecimalField())),
            invoice_count_2025=Count('invoice', distinct=True, filter=Q(invoice__posting_date__year=2025)),
            invoice_count_2026=Count('invoice', distinct=True, filter=Q(invoice__posting_date__year=2026)),
            customer_name=Max('invoice__customer_name'),
        )
    )

    # Credit memo quantities (subtract)
    cm_aggs = list(
        creditmemo_items_qs.filter(item_code__in=item_codes)
        .exclude(credit_memo__customer_code__isnull=True)
        .exclude(credit_memo__customer_code='')
        .values('item_code', 'credit_memo__customer_code', 'credit_memo__customer_name')
        .annotate(
            cm_qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
            cm_qty_2025=Coalesce(Sum('quantity', filter=Q(credit_memo__posting_date__year=2025)), Value(0, output_field=DecimalField())),
            cm_qty_2026=Coalesce(Sum('quantity', filter=Q(credit_memo__posting_date__year=2026)), Value(0, output_field=DecimalField())),
            cm_count_2025=Count('credit_memo', distinct=True, filter=Q(credit_memo__posting_date__year=2025)),
            cm_count_2026=Count('credit_memo', distinct=True, filter=Q(credit_memo__posting_date__year=2026)),
        )
    )

    cm_lookup = {}
    for row in cm_aggs:
        key = (row['item_code'], row['credit_memo__customer_code'])
        cm_lookup[key] = {
            'cm_qty': _safe_float(row['cm_qty']),
            'cm_qty_2025': _safe_float(row['cm_qty_2025']),
            'cm_qty_2026': _safe_float(row['cm_qty_2026']),
            'cm_count_2025': row.get('cm_count_2025', 0) or 0,
            'cm_count_2026': row.get('cm_count_2026', 0) or 0,
        }

    # Invoice numbers for invoices
    inv_numbers_raw = list(
        invoice_items_qs.filter(item_code__in=item_codes)
        .exclude(invoice__customer_code__isnull=True)
        .exclude(invoice__customer_code='')
        .values('item_code', 'invoice__customer_code', 'invoice__invoice_number')
        .distinct()
    )
    inv_numbers_lookup = defaultdict(lambda: defaultdict(list))
    for row in inv_numbers_raw:
        if row.get('invoice__invoice_number'):
            inv_numbers_lookup[row['item_code']][row['invoice__customer_code']].append(row['invoice__invoice_number'])

    # Credit memo numbers
    cm_numbers_raw = list(
        creditmemo_items_qs.filter(item_code__in=item_codes)
        .exclude(credit_memo__customer_code__isnull=True)
        .exclude(credit_memo__customer_code='')
        .values('item_code', 'credit_memo__customer_code', 'credit_memo__credit_memo_number')
        .distinct()
    )
    for row in cm_numbers_raw:
        if row.get('credit_memo__credit_memo_number'):
            inv_numbers_lookup[row['item_code']][row['credit_memo__customer_code']].append(
                f"CM:{row['credit_memo__credit_memo_number']}"
            )

    for agg in inv_aggs:
        item_code = agg['item_code']
        customer_code = agg['invoice__customer_code'] or ''
        qty = _safe_float(agg['total_quantity'] or 0)
        qty_2025 = _safe_float(agg.get('qty_2025', 0))
        qty_2026 = _safe_float(agg.get('qty_2026', 0))
        inv_count_2025 = agg.get('invoice_count_2025', 0) or 0
        inv_count_2026 = agg.get('invoice_count_2026', 0) or 0

        cm = cm_lookup.get((item_code, customer_code), {})
        qty -= cm.get('cm_qty', 0)
        qty_2025 -= cm.get('cm_qty_2025', 0)
        qty_2026 -= cm.get('cm_qty_2026', 0)
        inv_count_2025 += cm.get('cm_count_2025', 0)
        inv_count_2026 += cm.get('cm_count_2026', 0)

        if qty <= 0 and qty_2025 <= 0 and qty_2026 <= 0:
            continue

        doc_numbers = inv_numbers_lookup[item_code][customer_code]
        result[item_code].append({
            'customer_code': customer_code,
            'customer_name': agg.get('customer_name') or agg.get('invoice__customer_name') or 'Unknown',
            'qty_sold': qty,
            'qty_sold_2025': qty_2025,
            'qty_sold_2026': qty_2026,
            'invoice_count': inv_count_2025 + inv_count_2026,
            'invoice_count_2025': inv_count_2025,
            'invoice_count_2026': inv_count_2026,
            'invoice_numbers': sorted(set(doc_numbers))[:10],
        })

    for item_code in result:
        result[item_code].sort(key=lambda x: x['qty_sold'], reverse=True)

    return dict(result)


@login_required
def item_sold_analysis(request):
    """
    Item Sold Analysis - Firm-wise report showing Qty Sold 2025, 2026, and Customer Count.
    Data from AR Invoices and Credit Memos (same structure as Item Quoted Analysis).
    """
    selected_firms = request.GET.getlist('firm')
    search_term = request.GET.get('search', '').strip()

    firms = list(
        Items.objects.exclude(item_firm__isnull=True)
        .exclude(item_firm='')
        .values_list('item_firm', flat=True)
        .distinct()
        .order_by('item_firm')
    )

    if not selected_firms:
        context = {
            'firms': firms,
            'selected_firms': [],
            'items': [],
            'total_items': 0,
            'grand_total_2025': Decimal('0'),
            'grand_total_2026': Decimal('0'),
            'grand_total_customers': 0,
        }
        return render(request, 'salesorders/item_sold_analysis.html', context)

    firm_list = list(dict.fromkeys([f.strip() for f in selected_firms if f and str(f).strip()]))
    if not firm_list:
        context = {
            'firms': firms,
            'selected_firms': [],
            'items': [],
            'total_items': 0,
            'grand_total_2025': Decimal('0'),
            'grand_total_2026': Decimal('0'),
            'grand_total_customers': 0,
        }
        return render(request, 'salesorders/item_sold_analysis.html', context)

    items_qs = Items.objects.filter(item_firm__in=firm_list)
    item_codes = list(items_qs.values_list('item_code', flat=True).distinct())

    if not item_codes:
        context = {
            'firms': firms,
            'selected_firms': firm_list,
            'items': [],
            'total_items': 0,
            'grand_total_2025': Decimal('0'),
            'grand_total_2026': Decimal('0'),
            'grand_total_customers': 0,
        }
        return render(request, 'salesorders/item_sold_analysis.html', context)

    scope_q = salesman_scope_q_salesorder(request.user) if request.user.is_authenticated else Q()
    invoice_qs = SAPARInvoice.objects.filter(scope_q)
    creditmemo_qs = SAPARCreditMemo.objects.filter(scope_q)

    invoice_items_qs = SAPARInvoiceItem.objects.filter(
        invoice__in=invoice_qs,
        item_code__in=item_codes,
    ).exclude(item_code__isnull=True).exclude(item_code='').select_related('invoice')

    creditmemo_items_qs = SAPARCreditMemoItem.objects.filter(
        credit_memo__in=creditmemo_qs,
        item_code__in=item_codes,
    ).exclude(item_code__isnull=True).exclude(item_code='').select_related('credit_memo')

    # Qty sold 2025 (invoices - credit memos)
    sold_2025_inv = list(
        invoice_items_qs.filter(invoice__posting_date__year=2025)
        .values('item_code')
        .annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    sold_2025_cm = list(
        creditmemo_items_qs.filter(credit_memo__posting_date__year=2025)
        .values('item_code')
        .annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    sold_2025_dict = {r['item_code']: _safe_float(r['qty']) for r in sold_2025_inv}
    for r in sold_2025_cm:
        sold_2025_dict[r['item_code']] = sold_2025_dict.get(r['item_code'], 0) - _safe_float(r['qty'])

    # Qty sold 2026
    sold_2026_inv = list(
        invoice_items_qs.filter(invoice__posting_date__year=2026)
        .values('item_code')
        .annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    sold_2026_cm = list(
        creditmemo_items_qs.filter(credit_memo__posting_date__year=2026)
        .values('item_code')
        .annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    sold_2026_dict = {r['item_code']: _safe_float(r['qty']) for r in sold_2026_inv}
    for r in sold_2026_cm:
        sold_2026_dict[r['item_code']] = sold_2026_dict.get(r['item_code'], 0) - _safe_float(r['qty'])

    # Total amount 2025 (line_total_after_discount or line_total, invoices - credit memos)
    amt_2025_inv = list(
        invoice_items_qs.filter(invoice__posting_date__year=2025)
        .values('item_code')
        .annotate(amt=Sum(
            Case(
                When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0),
                     then=F('line_total_after_discount')),
                default=F('line_total')
            )
        ))
    )
    amt_2025_cm = list(
        creditmemo_items_qs.filter(credit_memo__posting_date__year=2025)
        .values('item_code')
        .annotate(amt=Sum(
            Case(
                When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0),
                     then=F('line_total_after_discount')),
                default=F('line_total')
            )
        ))
    )
    amt_2025_dict = {r['item_code']: _safe_float(r['amt']) for r in amt_2025_inv}
    for r in amt_2025_cm:
        amt_2025_dict[r['item_code']] = amt_2025_dict.get(r['item_code'], 0) - _safe_float(r.get('amt') or 0)

    amt_2026_inv = list(
        invoice_items_qs.filter(invoice__posting_date__year=2026)
        .values('item_code')
        .annotate(amt=Sum(
            Case(
                When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0),
                     then=F('line_total_after_discount')),
                default=F('line_total')
            )
        ))
    )
    amt_2026_cm = list(
        creditmemo_items_qs.filter(credit_memo__posting_date__year=2026)
        .values('item_code')
        .annotate(amt=Sum(
            Case(
                When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0),
                     then=F('line_total_after_discount')),
                default=F('line_total')
            )
        ))
    )
    amt_2026_dict = {r['item_code']: _safe_float(r['amt']) for r in amt_2026_inv}
    for r in amt_2026_cm:
        amt_2026_dict[r['item_code']] = amt_2026_dict.get(r['item_code'], 0) - _safe_float(r.get('amt') or 0)

    # Invoice count 2025, 2026
    inv_count_2025 = list(
        invoice_items_qs.filter(invoice__posting_date__year=2025)
        .values('item_code')
        .annotate(cnt=Count('invoice', distinct=True))
    )
    inv_count_2026 = list(
        invoice_items_qs.filter(invoice__posting_date__year=2026)
        .values('item_code')
        .annotate(cnt=Count('invoice', distinct=True))
    )
    cm_count_2025 = list(
        creditmemo_items_qs.filter(credit_memo__posting_date__year=2025)
        .values('item_code')
        .annotate(cnt=Count('credit_memo', distinct=True))
    )
    cm_count_2026 = list(
        creditmemo_items_qs.filter(credit_memo__posting_date__year=2026)
        .values('item_code')
        .annotate(cnt=Count('credit_memo', distinct=True))
    )
    inv_count_2025_dict = {r['item_code']: r['cnt'] for r in inv_count_2025}
    inv_count_2026_dict = {r['item_code']: r['cnt'] for r in inv_count_2026}
    for r in cm_count_2025:
        inv_count_2025_dict[r['item_code']] = inv_count_2025_dict.get(r['item_code'], 0) + r['cnt']
    for r in cm_count_2026:
        inv_count_2026_dict[r['item_code']] = inv_count_2026_dict.get(r['item_code'], 0) + r['cnt']

    # Customer count per item (distinct customers from invoices + credit memos)
    inv_cust = invoice_items_qs.exclude(invoice__customer_code__isnull=True).exclude(invoice__customer_code='')
    inv_cust_raw = list(inv_cust.values('item_code', 'invoice__customer_code').distinct())
    cm_cust_raw = list(
        creditmemo_items_qs.exclude(credit_memo__customer_code__isnull=True).exclude(credit_memo__customer_code='')
        .values('item_code', 'credit_memo__customer_code').distinct()
    )
    cust_sets = defaultdict(set)
    for r in inv_cust_raw:
        if r.get('invoice__customer_code'):
            cust_sets[r['item_code']].add(r['invoice__customer_code'])
    for r in cm_cust_raw:
        if r.get('credit_memo__customer_code'):
            cust_sets[r['item_code']].add(r['credit_memo__customer_code'])
    cust_dict = {k: len(v) for k, v in cust_sets.items()}

    items_info = {}
    for item in items_qs:
        if item.item_code not in items_info:
            items_info[item.item_code] = {
                'description': item.item_description or '',
                'upc': item.item_upvc or '',
                'total_stock': _safe_float(item.total_available_stock) if hasattr(item, 'total_available_stock') else 0.0,
            }

    items_list = []
    all_item_codes = set(item_codes)
    for item_code in all_item_codes:
        qty_2025 = sold_2025_dict.get(item_code, 0.0)
        qty_2026 = sold_2026_dict.get(item_code, 0.0)
        amt_2025 = amt_2025_dict.get(item_code, 0.0)
        amt_2026 = amt_2026_dict.get(item_code, 0.0)
        inv_cnt_2025 = inv_count_2025_dict.get(item_code, 0)
        inv_cnt_2026 = inv_count_2026_dict.get(item_code, 0)
        cust_count = cust_dict.get(item_code, 0)
        item_info = items_info.get(item_code, {'description': '', 'upc': '', 'total_stock': 0.0})

        items_list.append({
            'item_code': item_code,
            'item_description': item_info['description'],
            'upc_code': item_info['upc'],
            'total_stock': item_info['total_stock'],
            'import_ordered': 0,
            'qty_sold_2025': qty_2025,
            'qty_sold_2026': qty_2026,
            'total_amount_2025': amt_2025,
            'total_amount_2026': amt_2026,
            'total_invoices_2025': inv_cnt_2025,
            'total_invoices_2026': inv_cnt_2026,
            'customer_sold_count': cust_count,
            'customers': [],
        })

    import_ordered_lookup = _get_import_ordered_lookup(item_codes)
    open_po_lookup = _get_open_po_lookup(item_codes)
    for item in items_list:
        item['import_ordered'] = import_ordered_lookup.get(item['item_code'], 0) + open_po_lookup.get(item['item_code'], 0.0)

    items_list.sort(key=lambda x: (x['total_amount_2025'], x['customer_sold_count']), reverse=True)

    if search_term:
        search_lower = search_term.lower()
        items_list = [
            item for item in items_list
            if (search_lower in item['item_code'].lower() or
                search_lower in (item['item_description'] or '').lower() or
                search_lower in (item['upc_code'] or '').lower())
        ]

    grand_total_2025 = sum(item['qty_sold_2025'] for item in items_list)
    grand_total_2026 = sum(item['qty_sold_2026'] for item in items_list)
    all_custs = set()
    for r in inv_cust_raw:
        if r.get('invoice__customer_code'):
            all_custs.add(r['invoice__customer_code'])
    for r in cm_cust_raw:
        if r.get('credit_memo__customer_code'):
            all_custs.add(r['credit_memo__customer_code'])
    grand_total_customers = len(all_custs)

    page_size = 200
    paginator = Paginator(items_list, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    page_item_codes = [item['item_code'] for item in page_obj]

    if page_item_codes:
        customer_details = _get_customer_details_for_sold_items(
            invoice_items_qs, creditmemo_items_qs, page_item_codes
        )
        for item in page_obj:
            item['customers'] = customer_details.get(item['item_code'], [])

    proposed_qty_dict = {}
    if request.user.is_authenticated and page_item_codes:
        proposed_qty_objs = ProposedQuantity.objects.filter(
            user=request.user,
            item_code__in=page_item_codes
        )
        proposed_qty_dict = {pq.item_code: float(pq.proposed_qty) for pq in proposed_qty_objs}
    for item in page_obj:
        item['proposed_qty'] = proposed_qty_dict.get(item['item_code'], 0.0)

    is_ajax = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        request.GET.get('ajax') == '1'
    )
    if is_ajax:
        return _render_ajax_response(request, page_obj, paginator, grand_total_2025, grand_total_2026,
                                      grand_total_customers, len(items_list), search_term)

    context = {
        'firms': firms,
        'selected_firms': firm_list,
        'items': page_obj,
        'page_obj': page_obj,
        'total_items': len(items_list),
        'grand_total_2025': grand_total_2025,
        'grand_total_2026': grand_total_2026,
        'grand_total_customers': grand_total_customers,
        'search_term': search_term,
    }
    return render(request, 'salesorders/item_sold_analysis.html', context)


def _build_items_list_for_pdf(request, firm_list, search_term, include_customers=False):
    """
    Build full items list for PDF export (no pagination).
    Returns (items_list, grand_total_2025, grand_total_2026, grand_total_customers).
    """
    items_qs = Items.objects.filter(item_firm__in=firm_list)
    item_codes = list(items_qs.values_list('item_code', flat=True).distinct())
    if not item_codes:
        return [], Decimal('0'), Decimal('0'), 0

    scope_q = salesman_scope_q_salesorder(request.user) if request.user.is_authenticated else Q()
    invoice_qs = SAPARInvoice.objects.filter(scope_q)
    creditmemo_qs = SAPARCreditMemo.objects.filter(scope_q)
    invoice_items_qs = SAPARInvoiceItem.objects.filter(
        invoice__in=invoice_qs, item_code__in=item_codes
    ).exclude(item_code__isnull=True).exclude(item_code='').select_related('invoice')
    creditmemo_items_qs = SAPARCreditMemoItem.objects.filter(
        credit_memo__in=creditmemo_qs, item_code__in=item_codes
    ).exclude(item_code__isnull=True).exclude(item_code='').select_related('credit_memo')

    sold_2025_inv = list(invoice_items_qs.filter(invoice__posting_date__year=2025).values('item_code').annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))))
    sold_2025_cm = list(creditmemo_items_qs.filter(credit_memo__posting_date__year=2025).values('item_code').annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))))
    sold_2025_dict = {r['item_code']: _safe_float(r['qty']) for r in sold_2025_inv}
    for r in sold_2025_cm:
        sold_2025_dict[r['item_code']] = sold_2025_dict.get(r['item_code'], 0) - _safe_float(r['qty'])

    sold_2026_inv = list(invoice_items_qs.filter(invoice__posting_date__year=2026).values('item_code').annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))))
    sold_2026_cm = list(creditmemo_items_qs.filter(credit_memo__posting_date__year=2026).values('item_code').annotate(qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))))
    sold_2026_dict = {r['item_code']: _safe_float(r['qty']) for r in sold_2026_inv}
    for r in sold_2026_cm:
        sold_2026_dict[r['item_code']] = sold_2026_dict.get(r['item_code'], 0) - _safe_float(r['qty'])

    amt_2025_inv = list(invoice_items_qs.filter(invoice__posting_date__year=2025).values('item_code').annotate(amt=Sum(Case(When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0), then=F('line_total_after_discount')), default=F('line_total')))))
    amt_2025_cm = list(creditmemo_items_qs.filter(credit_memo__posting_date__year=2025).values('item_code').annotate(amt=Sum(Case(When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0), then=F('line_total_after_discount')), default=F('line_total')))))
    amt_2025_dict = {r['item_code']: _safe_float(r['amt']) for r in amt_2025_inv}
    for r in amt_2025_cm:
        amt_2025_dict[r['item_code']] = amt_2025_dict.get(r['item_code'], 0) - _safe_float(r.get('amt') or 0)

    amt_2026_inv = list(invoice_items_qs.filter(invoice__posting_date__year=2026).values('item_code').annotate(amt=Sum(Case(When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0), then=F('line_total_after_discount')), default=F('line_total')))))
    amt_2026_cm = list(creditmemo_items_qs.filter(credit_memo__posting_date__year=2026).values('item_code').annotate(amt=Sum(Case(When(Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0), then=F('line_total_after_discount')), default=F('line_total')))))
    amt_2026_dict = {r['item_code']: _safe_float(r['amt']) for r in amt_2026_inv}
    for r in amt_2026_cm:
        amt_2026_dict[r['item_code']] = amt_2026_dict.get(r['item_code'], 0) - _safe_float(r.get('amt') or 0)

    inv_count_2025 = list(invoice_items_qs.filter(invoice__posting_date__year=2025).values('item_code').annotate(cnt=Count('invoice', distinct=True)))
    inv_count_2026 = list(invoice_items_qs.filter(invoice__posting_date__year=2026).values('item_code').annotate(cnt=Count('invoice', distinct=True)))
    cm_count_2025 = list(creditmemo_items_qs.filter(credit_memo__posting_date__year=2025).values('item_code').annotate(cnt=Count('credit_memo', distinct=True)))
    cm_count_2026 = list(creditmemo_items_qs.filter(credit_memo__posting_date__year=2026).values('item_code').annotate(cnt=Count('credit_memo', distinct=True)))
    inv_count_2025_dict = {r['item_code']: r['cnt'] for r in inv_count_2025}
    inv_count_2026_dict = {r['item_code']: r['cnt'] for r in inv_count_2026}
    for r in cm_count_2025:
        inv_count_2025_dict[r['item_code']] = inv_count_2025_dict.get(r['item_code'], 0) + r['cnt']
    for r in cm_count_2026:
        inv_count_2026_dict[r['item_code']] = inv_count_2026_dict.get(r['item_code'], 0) + r['cnt']

    inv_cust_raw = list(invoice_items_qs.exclude(invoice__customer_code__isnull=True).exclude(invoice__customer_code='').values('item_code', 'invoice__customer_code').distinct())
    cm_cust_raw = list(creditmemo_items_qs.exclude(credit_memo__customer_code__isnull=True).exclude(credit_memo__customer_code='').values('item_code', 'credit_memo__customer_code').distinct())
    cust_sets = defaultdict(set)
    for r in inv_cust_raw:
        if r.get('invoice__customer_code'):
            cust_sets[r['item_code']].add(r['invoice__customer_code'])
    for r in cm_cust_raw:
        if r.get('credit_memo__customer_code'):
            cust_sets[r['item_code']].add(r['credit_memo__customer_code'])
    cust_dict = {k: len(v) for k, v in cust_sets.items()}

    items_info = {}
    for item in items_qs:
        if item.item_code not in items_info:
            items_info[item.item_code] = {'description': item.item_description or '', 'upc': item.item_upvc or '', 'total_stock': _safe_float(item.total_available_stock) if hasattr(item, 'total_available_stock') else 0.0}

    import_ordered_lookup = _get_import_ordered_lookup(item_codes)
    open_po_lookup = _get_open_po_lookup(item_codes)

    items_list = []
    for item_code in set(item_codes):
        item_info = items_info.get(item_code, {'description': '', 'upc': '', 'total_stock': 0.0})
        items_list.append({
            'item_code': item_code,
            'item_description': item_info['description'],
            'upc_code': item_info['upc'],
            'total_stock': item_info['total_stock'],
            'import_ordered': import_ordered_lookup.get(item_code, 0) + open_po_lookup.get(item_code, 0.0),
            'qty_sold_2025': sold_2025_dict.get(item_code, 0.0),
            'qty_sold_2026': sold_2026_dict.get(item_code, 0.0),
            'total_amount_2025': amt_2025_dict.get(item_code, 0.0),
            'total_amount_2026': amt_2026_dict.get(item_code, 0.0),
            'total_invoices_2025': inv_count_2025_dict.get(item_code, 0),
            'total_invoices_2026': inv_count_2026_dict.get(item_code, 0),
            'customer_sold_count': cust_dict.get(item_code, 0),
            'customers': [],
        })

    items_list.sort(key=lambda x: (x['total_amount_2025'], x['customer_sold_count']), reverse=True)
    if search_term:
        search_lower = search_term.lower()
        items_list = [i for i in items_list if search_lower in (i['item_code'] or '').lower() or search_lower in (i['item_description'] or '').lower() or search_lower in (i['upc_code'] or '').lower()]

    grand_total_2025 = sum(i['qty_sold_2025'] for i in items_list)
    grand_total_2026 = sum(i['qty_sold_2026'] for i in items_list)
    all_custs = set()
    for r in inv_cust_raw:
        if r.get('invoice__customer_code'):
            all_custs.add(r['invoice__customer_code'])
    for r in cm_cust_raw:
        if r.get('credit_memo__customer_code'):
            all_custs.add(r['credit_memo__customer_code'])
    grand_total_customers = len(all_custs)

    if include_customers and items_list:
        all_codes = [i['item_code'] for i in items_list]
        customer_details = _get_customer_details_for_sold_items(invoice_items_qs, creditmemo_items_qs, all_codes)
        for item in items_list:
            item['customers'] = customer_details.get(item['item_code'], [])

    return items_list, grand_total_2025, grand_total_2026, grand_total_customers


def _render_ajax_response(request, page_obj, paginator, grand_total_2025, grand_total_2026,
                          grand_total_customers, total_count, search_term=''):
    """Render AJAX JSON response for item sold analysis."""
    try:
        page_item_codes = [item['item_code'] for item in page_obj]
        proposed_qty_dict = {}
        if request.user.is_authenticated and page_item_codes:
            proposed_qty_objs = ProposedQuantity.objects.filter(
                user=request.user,
                item_code__in=page_item_codes
            )
            proposed_qty_dict = {pq.item_code: float(pq.proposed_qty) for pq in proposed_qty_objs}
        for item in page_obj:
            item['proposed_qty'] = proposed_qty_dict.get(item['item_code'], 0.0)

        table_html = render_to_string(
            'salesorders/_item_sold_analysis_table.html',
            {
                'items': page_obj,
                'grand_total_2025': grand_total_2025,
                'grand_total_2026': grand_total_2026,
            },
            request=request
        )
        pagination_html = ''
        if paginator.num_pages > 1:
            try:
                pagination_html = render_to_string(
                    'salesorders/_pagination.html',
                    {'page_obj': page_obj},
                    request=request
                )
            except Exception as e:
                logger.warning(f"Could not render pagination: {e}")

        return JsonResponse({
            'success': True,
            'table_html': table_html,
            'pagination_html': pagination_html,
            'total_count': total_count,
            'grand_total_2025': float(grand_total_2025),
            'grand_total_2026': float(grand_total_2026),
            'grand_total_customers': grand_total_customers,
            'page_number': page_obj.number,
            'num_pages': paginator.num_pages,
            'has_previous': page_obj.has_previous(),
            'has_next': page_obj.has_next(),
            'items_count': len(page_obj),
        })
    except Exception as e:
        logger.error(f"Error rendering AJAX response: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
