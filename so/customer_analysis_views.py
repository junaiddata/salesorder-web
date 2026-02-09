"""
Customer Analysis Views
Separate views file for Customer Analysis functionality
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Q, Sum, Value, DecimalField, Max, Count
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from datetime import datetime
from decimal import Decimal
import logging

# Import models
from .models import SAPARInvoice, SAPARInvoiceItem, SAPARCreditMemo, SAPARCreditMemoItem, Items

# Import helper functions from sap_salesorder_views
from .sap_salesorder_views import (
    salesman_scope_q_salesorder,
    get_salesmen_by_category,
    normalize_salesman_name,
    get_business_category
)

logger = logging.getLogger(__name__)


@login_required
def customer_analysis(request):
    """
    Customer Analysis Matrix View
    Shows customers with columns for Total Sales, GP, GP% by year (2024, 2025, 2026)
    Similar to Item Analysis but for customers
    """
    # Get current year
    current_year = datetime.now().year
    
    # Years to analyze (2026 first, then 2025, 2024)
    years = [2026, 2025, 2024]
    
    # Get filters from request
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')  # Multi-select
    firm_filter = request.GET.getlist('firm')  # Multi-select
    item_filter = request.GET.getlist('item')  # Multi-select (item codes)
    store_filter = request.GET.get('store', '').strip()
    month_filter = request.GET.getlist('month')  # Multi-select
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()
    category_filter = request.GET.get('category', 'All').strip()  # Business category filter
    
    # Get base querysets with salesman scope
    invoice_qs = SAPARInvoice.objects.filter(salesman_scope_q_salesorder(request.user))
    creditmemo_qs = SAPARCreditMemo.objects.filter(salesman_scope_q_salesorder(request.user))
    
    # Apply category filter (applies before salesman filter)
    if category_filter and category_filter != 'All':
        category_salesmen = get_salesmen_by_category(category_filter, invoice_qs)
        if category_salesmen:
            invoice_qs = invoice_qs.filter(salesman_name__in=category_salesmen)
            creditmemo_qs = creditmemo_qs.filter(salesman_name__in=category_salesmen)
        else:
            # No salesmen in this category, return empty querysets
            invoice_qs = invoice_qs.none()
            creditmemo_qs = creditmemo_qs.none()
    
    # Apply salesman filter
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            invoice_qs = invoice_qs.filter(salesman_name__in=clean_salesmen)
            creditmemo_qs = creditmemo_qs.filter(salesman_name__in=clean_salesmen)
    
    # Apply store filter
    if store_filter:
        invoice_qs = invoice_qs.filter(store=store_filter)
        creditmemo_qs = creditmemo_qs.filter(store=store_filter)
    
    # Apply month filter
    if month_filter:
        try:
            month_nums = [int(m) for m in month_filter if m.strip()]
            if month_nums:
                invoice_qs = invoice_qs.filter(posting_date__month__in=month_nums)
                creditmemo_qs = creditmemo_qs.filter(posting_date__month__in=month_nums)
        except (ValueError, TypeError):
            pass
    
    # Apply date range filter
    def parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None
    
    start_date_parsed = parse_date(start_date)
    end_date_parsed = parse_date(end_date)
    
    if start_date_parsed:
        invoice_qs = invoice_qs.filter(posting_date__gte=start_date_parsed)
        creditmemo_qs = creditmemo_qs.filter(posting_date__gte=start_date_parsed)
    
    if end_date_parsed:
        invoice_qs = invoice_qs.filter(posting_date__lte=end_date_parsed)
        creditmemo_qs = creditmemo_qs.filter(posting_date__lte=end_date_parsed)
    
    # Apply firm filter - filter customers who have transactions with items from selected firms
    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_codes = Items.objects.filter(item_firm__in=clean_firms).values_list('item_code', flat=True)
            if firm_item_codes:
                # Get customer codes from invoices/credit memos that have these items
                firm_customer_codes_inv = SAPARInvoiceItem.objects.filter(
                    invoice__in=invoice_qs,
                    item_code__in=firm_item_codes
                ).values_list('invoice__customer_code', flat=True).distinct()
                
                firm_customer_codes_cm = SAPARCreditMemoItem.objects.filter(
                    credit_memo__in=creditmemo_qs,
                    item_code__in=firm_item_codes
                ).values_list('credit_memo__customer_code', flat=True).distinct()
                
                all_firm_customer_codes = list(set(list(firm_customer_codes_inv) + list(firm_customer_codes_cm)))
                if all_firm_customer_codes:
                    invoice_qs = invoice_qs.filter(customer_code__in=all_firm_customer_codes)
                    creditmemo_qs = creditmemo_qs.filter(customer_code__in=all_firm_customer_codes)
                else:
                    invoice_qs = invoice_qs.none()
                    creditmemo_qs = creditmemo_qs.none()
    
    # Apply item filter - filter customers who have transactions with selected items
    if item_filter:
        clean_items = [i for i in item_filter if i.strip()]
        if clean_items:
            # Get customer codes from invoices/credit memos that have these items
            item_customer_codes_inv = SAPARInvoiceItem.objects.filter(
                invoice__in=invoice_qs,
                item_code__in=clean_items
            ).values_list('invoice__customer_code', flat=True).distinct()
            
            item_customer_codes_cm = SAPARCreditMemoItem.objects.filter(
                credit_memo__in=creditmemo_qs,
                item_code__in=clean_items
            ).values_list('credit_memo__customer_code', flat=True).distinct()
            
            all_item_customer_codes = list(set(list(item_customer_codes_inv) + list(item_customer_codes_cm)))
            if all_item_customer_codes:
                invoice_qs = invoice_qs.filter(customer_code__in=all_item_customer_codes)
                creditmemo_qs = creditmemo_qs.filter(customer_code__in=all_item_customer_codes)
            else:
                invoice_qs = invoice_qs.none()
                creditmemo_qs = creditmemo_qs.none()
    
    # Check if user is admin
    is_admin = request.user.is_superuser or request.user.is_staff or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    
    # Get distinct salesmen for filter dropdown (with caching)
    from django.core.cache import cache
    
    # Cache key based on user role (salesmen list differs by user)
    cache_key_salesmen = f'customer_analysis_salesmen_{request.user.id}_{request.user.is_superuser}'
    all_salesmen = cache.get(cache_key_salesmen)
    
    if all_salesmen is None:
        invoice_salesmen = (
            SAPARInvoice.objects.filter(salesman_scope_q_salesorder(request.user))
            .exclude(salesman_name__isnull=True)
            .exclude(salesman_name='')
            .values_list('salesman_name', flat=True)
            .distinct()
            .order_by('salesman_name')
        )
        creditmemo_salesmen = (
            SAPARCreditMemo.objects.filter(salesman_scope_q_salesorder(request.user))
            .exclude(salesman_name__isnull=True)
            .exclude(salesman_name='')
            .values_list('salesman_name', flat=True)
            .distinct()
            .order_by('salesman_name')
        )
        all_salesmen = sorted(set(list(invoice_salesmen) + list(creditmemo_salesmen)))
        # Cache for 1 hour (3600 seconds)
        cache.set(cache_key_salesmen, all_salesmen, 3600)
    
    # Get distinct firms for filter dropdown (with caching)
    cache_key_firms = 'customer_analysis_firms_all'
    all_firms = cache.get(cache_key_firms)
    
    if all_firms is None:
        all_firms = list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='').values_list('item_firm', flat=True).distinct().order_by('item_firm'))
        # Cache for 1 hour (3600 seconds)
        cache.set(cache_key_firms, all_firms, 3600)
    
    # Get distinct items for filter dropdown (with caching) - include code, description, and UPC
    cache_key_items = 'customer_analysis_items_all'
    all_items = cache.get(cache_key_items)
    
    if all_items is None:
        # Get items with code, description, and UPC code
        items_qs = SAPARInvoiceItem.objects.exclude(item_code__isnull=True).exclude(item_code='').values(
            'item_code', 'item_description', 'upc_code'
        ).distinct().order_by('item_code')
        
        # Group by item_code and get latest description and UPC
        items_dict = {}
        for item in items_qs:
            code = item['item_code']
            if code not in items_dict:
                items_dict[code] = {
                    'code': code,
                    'description': item.get('item_description') or '',
                    'upc': item.get('upc_code') or ''
                }
            else:
                # Update description and UPC if we have newer data
                if item.get('item_description'):
                    items_dict[code]['description'] = item.get('item_description')
                if item.get('upc_code'):
                    items_dict[code]['upc'] = item.get('upc_code')
        
        all_items = list(items_dict.values())
        # Cache for 1 hour (3600 seconds)
        cache.set(cache_key_items, all_items, 3600)
    
    # Build customer analysis data
    customer_data = {}
    
    # Process each year
    for year in years:
        # Filter invoices and credit memos for this year
        year_invoices = invoice_qs.filter(posting_date__year=year)
        year_creditmemos = creditmemo_qs.filter(posting_date__year=year)
        
        # Aggregate invoices by customer_code only (not name) to combine customers with same code but different names
        invoice_customers = year_invoices.values('customer_code').annotate(
            total_sales=Coalesce(Sum('doc_total_without_vat'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('total_gross_profit'), Value(0, output_field=DecimalField())),
            document_count=Count('id'),
            latest_posting_date=Max('posting_date')
        )
        
        # Aggregate credit memos by customer_code only (not name) to combine customers with same code but different names
        creditmemo_customers = year_creditmemos.values('customer_code').annotate(
            total_sales=Coalesce(Sum('doc_total_without_vat'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('total_gross_profit'), Value(0, output_field=DecimalField())),
            document_count=Count('id'),
            latest_posting_date=Max('posting_date')
        )
        
        # Apply search filter if provided (search by customer_code or customer_name)
        if search_query:
            search_customer_codes = SAPARInvoice.objects.filter(
                Q(customer_code__icontains=search_query) | Q(customer_name__icontains=search_query)
            ).values_list('customer_code', flat=True).distinct()
            
            search_customer_codes_cm = SAPARCreditMemo.objects.filter(
                Q(customer_code__icontains=search_query) | Q(customer_name__icontains=search_query)
            ).values_list('customer_code', flat=True).distinct()
            
            all_search_codes = list(set(list(search_customer_codes) + list(search_customer_codes_cm)))
            if all_search_codes:
                invoice_customers = invoice_customers.filter(customer_code__in=all_search_codes)
                creditmemo_customers = creditmemo_customers.filter(customer_code__in=all_search_codes)
            else:
                invoice_customers = invoice_customers.none()
                creditmemo_customers = creditmemo_customers.none()
        
        # Convert querysets to lists
        invoice_customers_list = list(invoice_customers)
        creditmemo_customers_list = list(creditmemo_customers)
        
        # Get latest customer names and salesman names per customer_code (one query for all)
        invoice_customer_codes = [item['customer_code'] for item in invoice_customers_list if item.get('customer_code')]
        creditmemo_customer_codes = [item['customer_code'] for item in creditmemo_customers_list if item.get('customer_code')]
        all_customer_codes = list(set(invoice_customer_codes + creditmemo_customer_codes))
        
        customer_name_map = {}
        customer_salesman_map = {}
        if all_customer_codes:
            # Get latest customer names and salesman names from invoices
            latest_invoice_customers = SAPARInvoice.objects.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'customer_name', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for cust in latest_invoice_customers:
                code = cust['customer_code']
                if code and code not in customer_name_map:
                    customer_name_map[code] = cust['customer_name'] or 'Unknown'
                    customer_salesman_map[code] = cust.get('salesman_name') or ''
            
            # Get latest customer names and salesman names from credit memos (update if newer)
            latest_creditmemo_customers = SAPARCreditMemo.objects.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'customer_name', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for cust in latest_creditmemo_customers:
                code = cust['customer_code']
                if code:
                    # Update if this is a new customer or if we don't have a name yet
                    if code not in customer_name_map:
                        customer_name_map[code] = cust['customer_name'] or 'Unknown'
                        customer_salesman_map[code] = cust.get('salesman_name') or ''
        
        # Get latest salesman names per customer_code based on latest posting_date for this year
        customer_salesman_final_map = {}
        if all_customer_codes:
            # Get latest salesman from invoices per customer_code
            latest_invoice_salesmen = year_invoices.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for inv in latest_invoice_salesmen:
                code = inv['customer_code']
                if code and code not in customer_salesman_final_map:
                    customer_salesman_final_map[code] = inv.get('salesman_name') or ''
            
            # Get latest salesman from credit memos per customer_code (update if newer)
            latest_creditmemo_salesmen = year_creditmemos.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for cm in latest_creditmemo_salesmen:
                code = cm['customer_code']
                if code and code not in customer_salesman_final_map:
                    customer_salesman_final_map[code] = cm.get('salesman_name') or ''
            
            # Fill in any missing salesman names from the initial map
            for code in all_customer_codes:
                if code not in customer_salesman_final_map:
                    customer_salesman_final_map[code] = customer_salesman_map.get(code, '')
        
        # Combine invoice and credit memo customers
        # Group by customer_code only (not name) to combine customers with same code but different names
        for item in invoice_customers_list:
            code = item['customer_code'] or ''
            if not code:
                continue
            name = customer_name_map.get(code, 'Unknown')
            salesman = customer_salesman_final_map.get(code, '')
            latest_date = item.get('latest_posting_date')
            # Use customer_code as key (not name) to combine customers with same code
            key = code
            
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': name,
                    'salesman_name': salesman,
                    'latest_posting_date': latest_date,
                    'years': {}
                }
            else:
                # Update name and salesman if this posting_date is newer
                if latest_date and customer_data[key].get('latest_posting_date'):
                    if latest_date > customer_data[key]['latest_posting_date']:
                        customer_data[key]['customer_name'] = name
                        customer_data[key]['salesman_name'] = salesman
                        customer_data[key]['latest_posting_date'] = latest_date
                elif latest_date:
                    customer_data[key]['customer_name'] = name
                    customer_data[key]['salesman_name'] = salesman
                    customer_data[key]['latest_posting_date'] = latest_date
            
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'document_count': 0
                }
            
            customer_data[key]['years'][year]['total_sales'] += item['total_sales'] or Decimal('0')
            customer_data[key]['years'][year]['total_gp'] += item['total_gp'] or Decimal('0')
            customer_data[key]['years'][year]['document_count'] += item['document_count']
        
        for item in creditmemo_customers_list:
            code = item['customer_code'] or ''
            if not code:
                continue
            name = customer_name_map.get(code, 'Unknown')
            salesman = customer_salesman_final_map.get(code, '')
            latest_date = item.get('latest_posting_date')
            # Use customer_code as key (not name) to combine customers with same code
            key = code
            
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': name,
                    'salesman_name': salesman,
                    'latest_posting_date': latest_date,
                    'years': {}
                }
            else:
                # Update name and salesman if this posting_date is newer
                if latest_date and customer_data[key].get('latest_posting_date'):
                    if latest_date > customer_data[key]['latest_posting_date']:
                        customer_data[key]['customer_name'] = name
                        customer_data[key]['salesman_name'] = salesman
                        customer_data[key]['latest_posting_date'] = latest_date
                elif latest_date:
                    customer_data[key]['customer_name'] = name
                    customer_data[key]['salesman_name'] = salesman
                    customer_data[key]['latest_posting_date'] = latest_date
            
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'document_count': 0
                }
            
            customer_data[key]['years'][year]['total_sales'] += item['total_sales'] or Decimal('0')
            customer_data[key]['years'][year]['total_gp'] += item['total_gp'] or Decimal('0')
            customer_data[key]['years'][year]['document_count'] += item['document_count']
    
    # Calculate GP% for each year
    customers_list = []
    for key, data in customer_data.items():
        customer_row = {
            'customer_code': data['customer_code'],
            'customer_name': data['customer_name'],
            'salesman_name': data.get('salesman_name', ''),
            'years_data': {}
        }
        
        for year in years:
            if year in data['years']:
                year_data = data['years'][year]
                total_sales = year_data['total_sales']
                total_gp = year_data['total_gp']
                
                # Calculate GP%
                gp_percent = Decimal('0')
                if total_sales and total_sales != 0:
                    gp_percent = (total_gp / total_sales) * 100
                
                customer_row['years_data'][year] = {
                    'total_sales': total_sales,
                    'total_gp': total_gp,
                    'gp_percent': gp_percent,
                    'document_count': year_data['document_count']
                }
            else:
                customer_row['years_data'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'gp_percent': Decimal('0'),
                    'document_count': 0
                }
        
        customers_list.append(customer_row)
    
    # Filter out customers without customer_code (double check)
    customers_list = [cust for cust in customers_list if cust['customer_code'] and cust['customer_code'].strip()]
    
    # Sort by total sales across all years (descending)
    customers_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)
    
    # Calculate totals for each year BEFORE pagination (from all customers)
    year_totals = {}
    for year in years:
        year_totals[year] = {
            'total_sales': Decimal('0'),
            'total_gp': Decimal('0'),
            'total_gp_percent': Decimal('0')
        }
        
        # Sum up all customers for this year
        for customer in customers_list:
            if year in customer['years_data']:
                year_data = customer['years_data'][year]
                year_totals[year]['total_sales'] += year_data['total_sales']
                year_totals[year]['total_gp'] += year_data['total_gp']
        
        # Calculate GP% (total GP / total sales * 100)
        if year_totals[year]['total_sales'] and year_totals[year]['total_sales'] != 0:
            year_totals[year]['total_gp_percent'] = (year_totals[year]['total_gp'] / year_totals[year]['total_sales']) * 100
    
    # Create totals list in year order
    totals_list = []
    for year in years:
        totals_list.append(year_totals[year])
    
    # Restructure data for easier template access - convert years_data to list of tuples
    for customer in customers_list:
        # Create a list with year data in order
        customer['year_list'] = []
        for year in years:
            if year in customer['years_data']:
                customer['year_list'].append(customer['years_data'][year])
            else:
                customer['year_list'].append({
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'gp_percent': Decimal('0'),
                    'document_count': 0
                })
    
    # Paginate customers - show 1000 customers per page
    page_size = 1000
    paginator = Paginator(customers_list, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Total count for display
    total_count = len(customers_list)
    
    # Check if this is an AJAX request
    is_ajax = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        request.GET.get('ajax') == '1'
    )
    
    if is_ajax:
        # Return JSON response for AJAX requests
        try:
            # Render table rows
            table_html = render_to_string('salesorders/_customer_analysis_table.html', {
                'customers': page_obj,
                'years': years,
                'is_admin': is_admin,
                'totals_list': totals_list,
            }, request=request)
            
            # Render pagination HTML if needed
            pagination_html = ''
            if paginator.num_pages > 1:
                try:
                    pagination_html = render_to_string('salesorders/_pagination.html', {
                        'page_obj': page_obj,
                    }, request=request)
                except Exception as e:
                    logger.warning(f"Could not render pagination: {e}")
            
            return JsonResponse({
                'success': True,
                'table_html': table_html,
                'pagination_html': pagination_html,
                'total_count': total_count,
                'page_number': page_obj.number,
                'num_pages': paginator.num_pages,
                'has_previous': page_obj.has_previous(),
                'has_next': page_obj.has_next(),
                'customers_count': len(page_obj),
            })
        except Exception as e:
            logger.error(f"Error rendering AJAX response: {e}")
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=500)
    
    context = {
        'customers': page_obj,  # Pass paginated customers
        'page_obj': page_obj,  # Also pass as page_obj for pagination template
        'total_count': total_count,  # Total count for display
        'years': years,
        'is_admin': is_admin,
        'current_year': current_year,
        'salesmen': all_salesmen,
        'firms': all_firms,
        'items': all_items,
        'totals_list': totals_list,
        'filters': {
            'q': search_query,
            'salesman': salesmen_filter,
            'firm': firm_filter,
            'item': item_filter,
            'store': store_filter,
            'month': month_filter,
            'start': start_date,
            'end': end_date,
            'category': category_filter,
        },
    }
    
    return render(request, 'salesorders/customer_analysis.html', context)
