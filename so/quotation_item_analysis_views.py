"""
Item Quoted Analysis Views
Firm-wise analysis showing Qty Quoted 2025, Qty Quoted 2026, and Customer Quoted Count per item.
Uses Credit Memo Analysis design pattern with expandable rows for customer drill-down.
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Q, Sum, Count, Max, Value, DecimalField
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from decimal import Decimal
from collections import defaultdict
import logging

from .models import Items, SAPQuotation, SAPQuotationItem, SAPSalesorder
from .views import salesman_scope_q
import re
import requests

logger = logging.getLogger(__name__)

# Purchase system API for import ordered quantities
IMPORT_ORDERED_API_URL = 'https://purchase.junaidworld.com/api/item-totals/'


def _get_import_ordered_lookup(item_codes):
    """
    Fetch totalqty_ordered per item from purchase API.
    Returns dict: item_code -> totalqty_ordered (int). Missing items get 0.
    """
    lookup = {code: 0 for code in item_codes}
    try:
        resp = requests.get(IMPORT_ORDERED_API_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return lookup
        for row in data:
            itemcode = (row.get('itemcode') or row.get('item_code') or '').strip()
            if itemcode and itemcode in lookup:
                try:
                    lookup[itemcode] = int(row.get('totalqty_ordered', 0) or 0)
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"Could not fetch import ordered from {IMPORT_ORDERED_API_URL}: {e}")
    return lookup


def safe_float(x):
    """Safely convert value to float."""
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _get_conversion_metrics(item_codes, quotation_items_qs):
    """
    Calculate conversion metrics for items: SO qty from converted quotations, 
    converted quotation count, and conversion rate - ALL SPLIT BY YEAR (2025/2026).
    
    PERFORMANCE OPTIMIZED:
    - Single bulk query for all SOs with nf_ref (indexed field)
    - In-memory regex extraction of quotation numbers
    - Dictionary lookups for O(1) access
    - Database-level aggregation for SO quantities split by year
    
    Returns dict: {
        item_code: {
            'so_qty_from_converted_2025': float,
            'so_qty_from_converted_2026': float,
            'converted_quotation_count_2025': int,
            'converted_quotation_count_2026': int,
            'conversion_rate_2025': float,  # percentage (0-100)
            'conversion_rate_2026': float,  # percentage (0-100)
        }
    }
    """
    # Bulk fetch all SOs with nf_ref (indexed field for fast filtering) - include posting_date
    sos_with_nfref = SAPSalesorder.objects.filter(
        nf_ref__isnull=False
    ).exclude(nf_ref='').values('id', 'nf_ref', 'posting_date')
    
    # Extract quotation numbers from NFRef in memory (regex)
    # Build map: quotation_number -> [(SO ID, posting_date)]
    quotation_to_so_data = defaultdict(list)
    for so in sos_with_nfref:
        nf_ref = so['nf_ref']
        if not nf_ref:
            continue
        # Extract quotation number using same pattern as model method
        match = re.search(r'(?:Quotations?|Q)\s+(\d+)', nf_ref, re.IGNORECASE)
        if match:
            quotation_number = match.group(1)
            quotation_to_so_data[quotation_number].append((so['id'], so['posting_date']))
    
    if not quotation_to_so_data:
        # No conversions found, return empty metrics
        return {item_code: {
            'so_qty_from_converted_2025': 0.0,
            'so_qty_from_converted_2026': 0.0,
            'converted_quotation_count_2025': 0,
            'converted_quotation_count_2026': 0,
            'conversion_rate_2025': 0.0,
            'conversion_rate_2026': 0.0,
        } for item_code in item_codes}
    
    # Get quotation numbers that were converted to SOs
    converted_quotation_numbers = set(quotation_to_so_data.keys())
    
    # Get all SO IDs that reference converted quotations
    all_so_ids = []
    for so_data_list in quotation_to_so_data.values():
        all_so_ids.extend([so_id for so_id, _ in so_data_list])
    
    # Bulk fetch SO items for converted SOs, filtered by item_codes - include salesorder posting_date
    from .models import SAPSalesorderItem
    so_items = SAPSalesorderItem.objects.filter(
        salesorder_id__in=all_so_ids,
        item_no__in=item_codes
    ).exclude(item_no__isnull=True).exclude(item_no='').select_related('salesorder').values(
        'item_no', 'quantity', 'salesorder_id', 'salesorder__posting_date'
    )
    
    # Build reverse lookup: SO ID -> (quotation_number, posting_date)
    so_id_to_data = {}
    for quotation_number, so_data_list in quotation_to_so_data.items():
        for so_id, posting_date in so_data_list:
            so_id_to_data[so_id] = (quotation_number, posting_date)
    
    # Aggregate SO quantities by item_code and year
    so_qty_by_item_2025 = defaultdict(float)
    so_qty_by_item_2026 = defaultdict(float)
    converted_quotations_by_item_2025 = defaultdict(set)
    converted_quotations_by_item_2026 = defaultdict(set)
    
    for so_item in so_items:
        item_code = so_item['item_no']
        so_id = so_item['salesorder_id']
        posting_date = so_item['salesorder__posting_date']
        data = so_id_to_data.get(so_id)
        
        if item_code and data:
            quotation_number, _ = data
            qty = safe_float(so_item['quantity'])
            
            # Split by SO posting date year
            if posting_date:
                year = posting_date.year if hasattr(posting_date, 'year') else (posting_date.year if hasattr(posting_date, 'year') else None)
                if year == 2025:
                    so_qty_by_item_2025[item_code] += qty
                    converted_quotations_by_item_2025[item_code].add(quotation_number)
                elif year == 2026:
                    so_qty_by_item_2026[item_code] += qty
                    converted_quotations_by_item_2026[item_code].add(quotation_number)
    
    # Get quoted quantities for converted quotations split by year (for conversion rate calculation)
    converted_quotation_items_2025 = quotation_items_qs.filter(
        quotation__q_number__in=converted_quotation_numbers,
        quotation__posting_date__year=2025
    ).exclude(quotation__posting_date__isnull=True)
    
    converted_quotation_items_2026 = quotation_items_qs.filter(
        quotation__q_number__in=converted_quotation_numbers,
        quotation__posting_date__year=2026
    ).exclude(quotation__posting_date__isnull=True)
    
    converted_qty_by_item_2025 = defaultdict(float)
    converted_qty_by_item_2026 = defaultdict(float)
    
    for qi in converted_quotation_items_2025:
        item_code = qi.item_no
        if item_code and item_code in item_codes:
            converted_qty_by_item_2025[item_code] += safe_float(qi.quantity)
    
    for qi in converted_quotation_items_2026:
        item_code = qi.item_no
        if item_code and item_code in item_codes:
            converted_qty_by_item_2026[item_code] += safe_float(qi.quantity)
    
    # Build result
    result = {}
    for item_code in item_codes:
        so_qty_2025 = so_qty_by_item_2025.get(item_code, 0.0)
        so_qty_2026 = so_qty_by_item_2026.get(item_code, 0.0)
        converted_count_2025 = len(converted_quotations_by_item_2025.get(item_code, set()))
        converted_count_2026 = len(converted_quotations_by_item_2026.get(item_code, set()))
        quoted_qty_2025 = converted_qty_by_item_2025.get(item_code, 0.0)
        quoted_qty_2026 = converted_qty_by_item_2026.get(item_code, 0.0)
        
        # Conversion rates split by year
        conversion_rate_2025 = 0.0
        if quoted_qty_2025 > 0:
            conversion_rate_2025 = (so_qty_2025 / quoted_qty_2025) * 100.0
        
        conversion_rate_2026 = 0.0
        if quoted_qty_2026 > 0:
            conversion_rate_2026 = (so_qty_2026 / quoted_qty_2026) * 100.0
        
        result[item_code] = {
            'so_qty_from_converted_2025': so_qty_2025,
            'so_qty_from_converted_2026': so_qty_2026,
            'converted_quotation_count_2025': converted_count_2025,
            'converted_quotation_count_2026': converted_count_2026,
            'conversion_rate_2025': conversion_rate_2025,
            'conversion_rate_2026': conversion_rate_2026,
        }
    
    return result


@login_required
def item_quoted_analysis(request):
    """
    Item Quoted Analysis - Firm-wise report showing Qty Quoted 2025, 2026, and Customer Count.
    """
    selected_firms = request.GET.getlist('firm')
    
    # Get all firms for dropdown
    firms = list(
        Items.objects.exclude(item_firm__isnull=True)
        .exclude(item_firm='')
        .values_list('item_firm', flat=True)
        .distinct()
        .order_by('item_firm')
    )
    
    # If no firms selected, return empty state
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
        return render(request, 'salesorders/item_quoted_analysis.html', context)
    
    # Clean and dedupe firms
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
        return render(request, 'salesorders/item_quoted_analysis.html', context)
    
    # Get item codes for selected firms
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
        return render(request, 'salesorders/item_quoted_analysis.html', context)
    
    # Base quotation queryset with salesman scope
    quotation_qs = SAPQuotation.objects.filter(salesman_scope_q(request.user))
    
    # Filter quotation items by item_no (matching item_codes from Items)
    quotation_items_qs = SAPQuotationItem.objects.filter(
        quotation__in=quotation_qs,
        item_no__in=item_codes,
    ).exclude(item_no__isnull=True).exclude(item_no='').select_related('quotation')
    
    # Aggregate qty quoted by item_no and year
    # Qty Quoted 2025
    quoted_2025_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2025)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(qty_quoted=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    quoted_2025_dict = {row['item_no']: safe_float(row['qty_quoted']) for row in quoted_2025_agg}
    
    # Qty Quoted 2026
    quoted_2026_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2026)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(qty_quoted=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    quoted_2026_dict = {row['item_no']: safe_float(row['qty_quoted']) for row in quoted_2026_agg}
    
    # Total Quotations 2025 (distinct quotation count per item)
    quotations_2025_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2025)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(quotation_count=Count('quotation', distinct=True))
    )
    quotations_2025_dict = {row['item_no']: row['quotation_count'] for row in quotations_2025_agg}
    
    # Total Quotations 2026 (distinct quotation count per item)
    quotations_2026_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2026)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(quotation_count=Count('quotation', distinct=True))
    )
    quotations_2026_dict = {row['item_no']: row['quotation_count'] for row in quotations_2026_agg}
    
    # Customer count per item (distinct customers who quoted this item)
    customer_count_agg = list(
        quotation_items_qs
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('item_no')
        .annotate(customer_count=Count('quotation__customer_code', distinct=True))
    )
    customer_count_dict = {row['item_no']: row['customer_count'] for row in customer_count_agg}
    
    # Get item descriptions, UPC, and total stock from Items model
    items_info = {}
    for item in items_qs:
        if item.item_code not in items_info:
            items_info[item.item_code] = {
                'description': item.item_description or '',
                'upc': item.item_upvc or '',
                'total_stock': safe_float(item.total_available_stock) if hasattr(item, 'total_available_stock') else 0.0,
            }
    
    # Also get descriptions from quotation items as fallback
    for qi in quotation_items_qs[:1000]:  # Limit to avoid too many queries
        if qi.item_no and qi.item_no not in items_info:
            items_info[qi.item_no] = {
                'description': qi.description or '',
                'upc': '',
                'total_stock': 0.0,
            }
    
    # Build items list - include all items from selected firms (even if no quotes)
    items_list = []
    all_item_codes = set(item_codes)
    
    for item_code in all_item_codes:
        qty_2025 = quoted_2025_dict.get(item_code, 0.0)
        qty_2026 = quoted_2026_dict.get(item_code, 0.0)
        quot_count_2025 = quotations_2025_dict.get(item_code, 0)
        quot_count_2026 = quotations_2026_dict.get(item_code, 0)
        cust_count = customer_count_dict.get(item_code, 0)
        
        item_info = items_info.get(item_code, {'description': '', 'upc': '', 'total_stock': 0.0})
        
        items_list.append({
            'item_code': item_code,
            'item_description': item_info['description'],
            'upc_code': item_info['upc'],
            'total_stock': item_info['total_stock'],
            'import_ordered': 0,  # Filled below from purchase API
            'qty_quoted_2025': qty_2025,
            'qty_quoted_2026': qty_2026,
            'total_quotations_2025': quot_count_2025,
            'total_quotations_2026': quot_count_2026,
            'customer_quoted_count': cust_count,
            'customers': [],  # Will be loaded lazily for current page only
        })
    
    # Fetch import ordered from purchase API (single request)
    import_ordered_lookup = _get_import_ordered_lookup(item_codes)
    for item in items_list:
        item['import_ordered'] = import_ordered_lookup.get(item['item_code'], 0)
    
    # Sort by total quoted (2025 + 2026) descending, then by customer count
    items_list.sort(key=lambda x: (x['qty_quoted_2025'] + x['qty_quoted_2026'], x['customer_quoted_count']), reverse=True)
    
    # Calculate grand totals
    grand_total_2025 = sum(item['qty_quoted_2025'] for item in items_list)
    grand_total_2026 = sum(item['qty_quoted_2026'] for item in items_list)
    
    # Get unique customers across all items
    all_customers_set = set()
    customer_items_agg = list(
        quotation_items_qs
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('quotation__customer_code')
        .distinct()
    )
    grand_total_customers = len(customer_items_agg)
    
    # Paginate BEFORE loading customer details (optimization)
    page_size = 200
    paginator = Paginator(items_list, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    # Load customer details ONLY for current page items
    page_item_codes = [item['item_code'] for item in page_obj]
    
    if page_item_codes:
        customer_details = _get_customer_details_for_items(
            quotation_items_qs, page_item_codes
        )
        
        # Attach customer details to page items
        for item in page_obj:
            item['customers'] = customer_details.get(item['item_code'], [])
    
    # Load conversion metrics ONLY if requested (optional/conditional loading)
    include_conversion = request.GET.get('include_conversion', 'false').lower() == 'true'
    conversion_metrics = {}
    if include_conversion:
        conversion_metrics = _get_conversion_metrics(item_codes, quotation_items_qs)
        # Attach conversion metrics to items
        for item in items_list:
            item_code = item['item_code']
            metrics = conversion_metrics.get(item_code, {
                'so_qty_from_converted': 0.0,
                'converted_quotation_count': 0,
                'conversion_rate': 0.0
            })
            item.update(metrics)
    
    # Check if AJAX request
    is_ajax = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        request.GET.get('ajax') == '1'
    )
    
    if is_ajax:
        return _render_ajax_response(request, page_obj, paginator, grand_total_2025, grand_total_2026, grand_total_customers, len(items_list))
    
    context = {
        'firms': firms,
        'selected_firms': firm_list,
        'items': page_obj,
        'page_obj': page_obj,
        'total_items': len(items_list),
        'grand_total_2025': grand_total_2025,
        'grand_total_2026': grand_total_2026,
        'grand_total_customers': grand_total_customers,
        'include_conversion': include_conversion,
    }
    
    return render(request, 'salesorders/item_quoted_analysis.html', context)


def _get_customer_details_for_items(quotation_items_qs, item_codes):
    """
    Get customer details for specific items only.
    Returns dict: {item_code: [customer_details]}
    
    OPTIMIZED: Single query for all customer aggregates
    """
    # Filter to only items on current page
    filtered_items = quotation_items_qs.filter(item_no__in=item_codes)
    
    # Aggregate by item_no and customer_code (qty and quotation count split by year 2025/2026)
    customer_aggs = list(
        filtered_items
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('item_no', 'quotation__customer_code')
        .annotate(
            total_quantity=Sum('quantity'),
            qty_2025=Coalesce(Sum('quantity', filter=Q(quotation__posting_date__year=2025)), Value(0, output_field=DecimalField())),
            qty_2026=Coalesce(Sum('quantity', filter=Q(quotation__posting_date__year=2026)), Value(0, output_field=DecimalField())),
            quotation_count_2025=Count('quotation', distinct=True, filter=Q(quotation__posting_date__year=2025)),
            quotation_count_2026=Count('quotation', distinct=True, filter=Q(quotation__posting_date__year=2026)),
            customer_name=Max('quotation__customer_name'),
        )
    )
    
    # Get quotation numbers for these items/customers
    quotation_numbers_raw = list(
        filtered_items
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('item_no', 'quotation__customer_code', 'quotation__q_number')
        .distinct()
    )
    
    # Build lookup: (item_code, customer_code) -> [quotation_numbers]
    quotation_numbers_lookup = defaultdict(lambda: defaultdict(list))
    for row in quotation_numbers_raw:
        if row['quotation__q_number']:
            quotation_numbers_lookup[row['item_no']][row['quotation__customer_code']].append(
                row['quotation__q_number']
            )
    
    # Build result
    result = defaultdict(list)
    
    for agg in customer_aggs:
        qty = safe_float(agg['total_quantity'] or 0)
        if qty == 0:
            continue
        
        item_code = agg['item_no']
        customer_code = agg['quotation__customer_code'] or ''
        
        quotation_numbers = sorted(quotation_numbers_lookup[item_code][customer_code])
        
        result[item_code].append({
            'customer_code': customer_code,
            'customer_name': agg['customer_name'] or 'Unknown',
            'qty_quoted': qty,
            'qty_quoted_2025': safe_float(agg.get('qty_2025', 0)),
            'qty_quoted_2026': safe_float(agg.get('qty_2026', 0)),
            'quotation_count': len(quotation_numbers_lookup[item_code][customer_code]),
            'quotation_count_2025': agg.get('quotation_count_2025', 0) or 0,
            'quotation_count_2026': agg.get('quotation_count_2026', 0) or 0,
            'quotation_numbers': quotation_numbers[:10],  # Limit to 10 quotes per customer
        })
    
    # Sort each item's customers by qty quoted descending
    for item_code in result:
        result[item_code].sort(key=lambda x: x['qty_quoted'], reverse=True)
    
    return dict(result)


def _render_ajax_response(request, page_obj, paginator, grand_total_2025, grand_total_2026, grand_total_customers, total_count):
    """Render AJAX JSON response."""
    try:
        # include_conversion from request so table partial can show conversion columns
        include_conversion = request.GET.get('include_conversion', 'false').lower() == 'true'
        table_html = render_to_string(
            'salesorders/_item_quoted_analysis_table.html',
            {'items': page_obj, 'include_conversion': include_conversion},
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
