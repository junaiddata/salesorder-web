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

from .models import Items, SAPQuotation, SAPQuotationItem
from .views import salesman_scope_q

logger = logging.getLogger(__name__)


def safe_float(x):
    """Safely convert value to float."""
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


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
            'qty_quoted_2025': qty_2025,
            'qty_quoted_2026': qty_2026,
            'total_quotations_2025': quot_count_2025,
            'total_quotations_2026': quot_count_2026,
            'customer_quoted_count': cust_count,
            'customers': [],  # Will be loaded lazily for current page only
        })
    
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
        table_html = render_to_string(
            'salesorders/_item_quoted_analysis_table.html',
            {'items': page_obj},
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
