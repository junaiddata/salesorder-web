"""
Customer Analysis Views
Separate views file for Customer Analysis functionality
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Sum, Value, DecimalField, Max, Count
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from datetime import datetime
from decimal import Decimal
import logging
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
import requests
import os
from django.conf import settings

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
            
            # Render filter display HTML
            filter_display_html = ''
            if salesmen_filter or firm_filter or item_filter:
                filter_display_html = render_to_string('salesorders/_customer_analysis_filter_display.html', {
                    'filters': {
                        'salesman': salesmen_filter,
                        'firm': firm_filter,
                        'item': item_filter,
                    },
                    'firms': all_firms,
                    'items': all_items,
                }, request=request)
            
            return JsonResponse({
                'success': True,
                'table_html': table_html,
                'pagination_html': pagination_html,
                'filter_display_html': filter_display_html,
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


@login_required
def export_customer_analysis_pdf(request):
    """
    Export Customer Analysis to PDF with all current filters applied
    Reuses the same logic as customer_analysis view
    """
    # Import PDF libraries
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    import requests
    
    # Call the main customer_analysis view logic to get the data
    # We'll duplicate the key parts here for PDF generation
    
    current_year = datetime.now().year
    years = [2026, 2025, 2024]
    
    # Get all filters (same as customer_analysis)
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    item_filter = request.GET.getlist('item')
    store_filter = request.GET.get('store', '').strip()
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()
    category_filter = request.GET.get('category', 'All').strip()
    
    # Get base querysets
    invoice_qs = SAPARInvoice.objects.filter(salesman_scope_q_salesorder(request.user))
    creditmemo_qs = SAPARCreditMemo.objects.filter(salesman_scope_q_salesorder(request.user))
    
    # Apply filters (simplified - same logic as customer_analysis)
    if category_filter and category_filter != 'All':
        category_salesmen = get_salesmen_by_category(category_filter, invoice_qs)
        if category_salesmen:
            invoice_qs = invoice_qs.filter(salesman_name__in=category_salesmen)
            creditmemo_qs = creditmemo_qs.filter(salesman_name__in=category_salesmen)
        else:
            invoice_qs = invoice_qs.none()
            creditmemo_qs = creditmemo_qs.none()
    
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            invoice_qs = invoice_qs.filter(salesman_name__in=clean_salesmen)
            creditmemo_qs = creditmemo_qs.filter(salesman_name__in=clean_salesmen)
    
    if store_filter:
        invoice_qs = invoice_qs.filter(store=store_filter)
        creditmemo_qs = creditmemo_qs.filter(store=store_filter)
    
    if month_filter:
        try:
            month_nums = [int(m) for m in month_filter if m.strip()]
            if month_nums:
                invoice_qs = invoice_qs.filter(posting_date__month__in=month_nums)
                creditmemo_qs = creditmemo_qs.filter(posting_date__month__in=month_nums)
        except (ValueError, TypeError):
            pass
    
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
    
    # Apply firm and item filters (simplified)
    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_codes = Items.objects.filter(item_firm__in=clean_firms).values_list('item_code', flat=True)
            if firm_item_codes:
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
    
    if item_filter:
        clean_items = [i for i in item_filter if i.strip()]
        if clean_items:
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
    
    # Build customer data (simplified version)
    customer_data = {}
    is_admin = request.user.is_superuser or request.user.is_staff or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    
    for year in years:
        year_invoices = invoice_qs.filter(posting_date__year=year)
        year_creditmemos = creditmemo_qs.filter(posting_date__year=year)
        
        invoice_customers = year_invoices.values('customer_code').annotate(
            total_sales=Coalesce(Sum('doc_total_without_vat'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('total_gross_profit'), Value(0, output_field=DecimalField())),
            latest_posting_date=Max('posting_date')
        )
        
        creditmemo_customers = year_creditmemos.values('customer_code').annotate(
            total_sales=Coalesce(Sum('doc_total_without_vat'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('total_gross_profit'), Value(0, output_field=DecimalField())),
            latest_posting_date=Max('posting_date')
        )
        
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
        
        invoice_customers_list = list(invoice_customers)
        creditmemo_customers_list = list(creditmemo_customers)
        
        invoice_customer_codes = [item['customer_code'] for item in invoice_customers_list if item.get('customer_code')]
        creditmemo_customer_codes = [item['customer_code'] for item in creditmemo_customers_list if item.get('customer_code')]
        all_customer_codes = list(set(invoice_customer_codes + creditmemo_customer_codes))
        
        customer_name_map = {}
        customer_salesman_map = {}
        if all_customer_codes:
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
            
            latest_creditmemo_customers = SAPARCreditMemo.objects.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'customer_name', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for cust in latest_creditmemo_customers:
                code = cust['customer_code']
                if code and code not in customer_name_map:
                    customer_name_map[code] = cust['customer_name'] or 'Unknown'
                    customer_salesman_map[code] = cust.get('salesman_name') or ''
        
        customer_salesman_final_map = {}
        if all_customer_codes:
            latest_invoice_salesmen = year_invoices.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for inv in latest_invoice_salesmen:
                code = inv['customer_code']
                if code and code not in customer_salesman_final_map:
                    customer_salesman_final_map[code] = inv.get('salesman_name') or ''
            
            latest_creditmemo_salesmen = year_creditmemos.filter(
                customer_code__in=all_customer_codes
            ).exclude(customer_code__isnull=True).exclude(customer_code='').values(
                'customer_code', 'salesman_name', 'posting_date'
            ).order_by('customer_code', '-posting_date', '-id')
            
            for cm in latest_creditmemo_salesmen:
                code = cm['customer_code']
                if code and code not in customer_salesman_final_map:
                    customer_salesman_final_map[code] = cm.get('salesman_name') or ''
            
            for code in all_customer_codes:
                if code not in customer_salesman_final_map:
                    customer_salesman_final_map[code] = customer_salesman_map.get(code, '')
        
        for item in invoice_customers_list:
            code = item['customer_code'] or ''
            if not code:
                continue
            name = customer_name_map.get(code, 'Unknown')
            salesman = customer_salesman_final_map.get(code, '')
            latest_date = item.get('latest_posting_date')
            key = code
            
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': name,
                    'salesman_name': salesman,
                    'latest_posting_date': latest_date,
                    'years': {}
                }
            
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                }
            
            customer_data[key]['years'][year]['total_sales'] += item.get('total_sales', Decimal('0'))
            customer_data[key]['years'][year]['total_gp'] += item.get('total_gp', Decimal('0'))
        
        for item in creditmemo_customers_list:
            code = item['customer_code'] or ''
            if not code:
                continue
            name = customer_name_map.get(code, 'Unknown')
            salesman = customer_salesman_final_map.get(code, '')
            latest_date = item.get('latest_posting_date')
            key = code
            
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': name,
                    'salesman_name': salesman,
                    'latest_posting_date': latest_date,
                    'years': {}
                }
            
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                }
            
            customer_data[key]['years'][year]['total_sales'] += item.get('total_sales', Decimal('0'))
            customer_data[key]['years'][year]['total_gp'] += item.get('total_gp', Decimal('0'))
    
    # Convert to sorted list
    customers_list = sorted(
        customer_data.values(),
        key=lambda x: sum(y.get('total_sales', Decimal('0')) for y in x['years'].values()),
        reverse=True
    )
    
    # Create PDF
    response = HttpResponse(content_type='application/pdf')
    filename = f"Customer_Analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=0.75*inch,
        bottomMargin=0.5*inch
    )
    
    elements = []
    styles = getSampleStyleSheet()
    
    # Logo
    logo_url = "https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp"
    try:
        logo = Image(logo_url, width=2*inch, height=0.7*inch)
        elements.append(logo)
        elements.append(Spacer(1, 0.2*inch))
    except Exception:
        pass
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        textColor=colors.HexColor('#2C3E50'),
        spaceAfter=12,
        alignment=TA_CENTER
    )
    elements.append(Paragraph("Customer Performance Analysis", title_style))
    elements.append(Spacer(1, 0.1*inch))
    
    # Filter info
    filter_info = []
    if store_filter:
        filter_info.append(f"Store: {store_filter}")
    if category_filter != 'All':
        filter_info.append(f"Category: {category_filter}")
    if salesmen_filter:
        filter_info.append(f"Salesmen: {', '.join(salesmen_filter)}")
    if firm_filter:
        filter_info.append(f"Firms: {', '.join(firm_filter[:2])}{'...' if len(firm_filter) > 2 else ''}")
    if month_filter:
        filter_info.append(f"Months: {', '.join(month_filter)}")
    if start_date or end_date:
        filter_info.append(f"Date Range: {start_date or 'Start'} to {end_date or 'End'}")
    if search_query:
        filter_info.append(f"Search: {search_query}")
    
    if filter_info:
        filter_style = ParagraphStyle(
            'FilterStyle',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor('#666666'),
            alignment=TA_LEFT
        )
        elements.append(Paragraph("Filters: " + " | ".join(filter_info), filter_style))
        elements.append(Spacer(1, 0.15*inch))
    
    # Table headers with Paragraph for proper rendering
    header_style = ParagraphStyle(
        'HeaderStyle',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.whitesmoke,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    
    header_data = [[
        Paragraph('Customer Name', header_style),
        Paragraph('Salesman', header_style)
    ]]
    for year in years:
        if is_admin:
            header_data[0].extend([
                Paragraph(f'{year} Sales', header_style),
                Paragraph(f'{year} GP', header_style),
                Paragraph(f'{year} GP%', header_style)
            ])
        else:
            header_data[0].append(Paragraph(f'{year} Sales', header_style))
    
    # Table data with wrapped text
    table_data = [header_data[0]]
    
    # Create a style for wrapping text
    wrap_style = ParagraphStyle(
        'WrapStyle',
        parent=styles['Normal'],
        fontSize=7,
        leading=9,
        alignment=TA_LEFT
    )
    
    for customer in customers_list:
        # Use Paragraph for text wrapping
        customer_name_para = Paragraph(customer['customer_name'], wrap_style)
        salesman_name_para = Paragraph(customer['salesman_name'], wrap_style)
        
        row = [customer_name_para, salesman_name_para]
        
        for year in years:
            year_data = customer['years'].get(year, {})
            sales = year_data.get('total_sales', Decimal('0'))
            gp = year_data.get('total_gp', Decimal('0'))
            
            row.append(Paragraph(f"{sales:,.2f}", wrap_style))
            if is_admin:
                row.append(Paragraph(f"{gp:,.2f}", wrap_style))
                gp_percent = (gp / sales * 100) if sales > 0 else Decimal('0')
                row.append(Paragraph(f"{gp_percent:.2f}%", wrap_style))
        
        table_data.append(row)
    
    # Create table with adjusted column widths to fit landscape A4
    # Landscape A4: 11.69 x 8.27 inches, minus margins (0.5*2 = 1 inch each side) = 10.69 inches available
    col_widths = [2.2*inch, 1.2*inch]  # Customer Name, Salesman
    for year in years:
        col_widths.append(0.85*inch)  # Sales
        if is_admin:
            col_widths.append(0.75*inch)  # GP
            col_widths.append(0.65*inch)  # GP%
    
    # Adjust if total width exceeds available space
    total_width = sum(col_widths)
    available_width = 10.69*inch
    if total_width > available_width:
        scale_factor = available_width / total_width
        col_widths = [w * scale_factor for w in col_widths]
    
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    
    # Table style with smaller fonts and proper wrapping
    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
    ])
    
    table.setStyle(table_style)
    elements.append(table)
    
    # Build PDF
    doc.build(elements)
    pdf_value = buffer.getvalue()
    buffer.close()
    response.write(pdf_value)
    
    return response
