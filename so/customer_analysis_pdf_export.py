"""
PDF Export for Customer Analysis
"""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.db.models import Q, Sum, Value, DecimalField, Max, Count
from django.db.models.functions import Coalesce
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
import requests
import os
from django.conf import settings

from .models import SAPARInvoice, SAPARInvoiceItem, SAPARCreditMemo, SAPARCreditMemoItem, Items
from .sap_salesorder_views import (
    salesman_scope_q_salesorder,
    get_salesmen_by_category,
)


@login_required
def export_customer_analysis_pdf(request):
    """
    Export Customer Analysis to PDF with all current filters applied
    """
    # Get current year
    current_year = datetime.now().year
    years = [2026, 2025, 2024]
    
    # Get all filters from request (same as customer_analysis view)
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    item_filter = request.GET.getlist('item')
    store_filter = request.GET.get('store', '').strip()
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()
    category_filter = request.GET.get('category', 'All').strip()
    
    # Get base querysets with salesman scope
    invoice_qs = SAPARInvoice.objects.filter(salesman_scope_q_salesorder(request.user))
    creditmemo_qs = SAPARCreditMemo.objects.filter(salesman_scope_q_salesorder(request.user))
    
    # Apply all filters (same logic as customer_analysis view)
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
    
    # Apply firm filter
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
    
    # Apply item filter
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
    
    # Build customer analysis data (same logic as customer_analysis view)
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
                if code:
                    if code not in customer_name_map:
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
            else:
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
            else:
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
        filter_info.append(f"Salesmen: {', '.join(salesmen_filter[:3])}{'...' if len(salesmen_filter) > 3 else ''}")
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
    
    # Table headers
    header_data = [['Customer Name', 'Customer Code', 'Salesman']]
    for year in years:
        if is_admin:
            header_data[0].extend([f'{year} Sales', f'{year} GP', f'{year} GP%'])
        else:
            header_data[0].append(f'{year} Sales')
    
    # Table data
    table_data = [header_data[0]]
    
    for customer in customers_list:
        row = [
            customer['customer_name'][:30] if len(customer['customer_name']) > 30 else customer['customer_name'],
            customer['customer_code'],
            customer['salesman_name'][:20] if len(customer['salesman_name']) > 20 else customer['salesman_name']
        ]
        
        for year in years:
            year_data = customer['years'].get(year, {})
            sales = year_data.get('total_sales', Decimal('0'))
            gp = year_data.get('total_gp', Decimal('0'))
            
            row.append(f"{sales:,.2f}")
            if is_admin:
                row.append(f"{gp:,.2f}")
                gp_percent = (gp / sales * 100) if sales > 0 else Decimal('0')
                row.append(f"{gp_percent:.2f}%")
        
        table_data.append(row)
    
    # Create table
    col_widths = [2*inch, 1*inch, 1.2*inch]
    for year in years:
        col_widths.append(1*inch)
        if is_admin:
            col_widths.append(0.9*inch)
            col_widths.append(0.8*inch)
    
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    
    # Table style
    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('TOPPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
    ])
    
    table.setStyle(table_style)
    elements.append(table)
    
    # Build PDF
    doc.build(elements)
    pdf_value = buffer.getvalue()
    buffer.close()
    response.write(pdf_value)
    
    return response
