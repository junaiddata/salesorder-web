"""
Alabama Customer Analysis - separate view file.
Same behavior as SO customer_analysis but:
- Years: dynamic (current and previous)
- Data source: AlabamaSalesLine (not SAP AR Invoice/Credit Memo)
- Filters: search, salesman, firm, item, month, start/end date (no store/category)
"""
import logging
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Q, Count
from django.db.models.functions import Coalesce
from django.db.models import DecimalField, Value
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string

from .models import AlabamaSalesLine
from .views import alabama_salesman_scope_q, normalize_alabama_salesman
from .item_analysis_views import _salesman_filter_q, _get_alabama_logo

logger = logging.getLogger(__name__)


@login_required
def customer_analysis(request):
    """
    Alabama Customer Analysis - dynamic years (current and previous).
    Data from AlabamaSalesLine.
    """
    from so.models import Items, Customer

    current_year = datetime.now().year
    years = [current_year, current_year - 1]

    # Filters
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    item_filter = request.GET.getlist('item')
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()

    # Base queryset with salesman scope
    qs = AlabamaSalesLine.objects.all().select_related('customer', 'item')
    scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
    qs = qs.filter(scope_q)

    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            qs = qs.filter(_salesman_filter_q(clean, field='sales_employee'))

    if month_filter:
        try:
            month_nums = [int(m) for m in month_filter if m.strip()]
            if month_nums:
                qs = qs.filter(posting_date__month__in=month_nums)
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
        qs = qs.filter(posting_date__gte=start_date_parsed)
    if end_date_parsed:
        qs = qs.filter(posting_date__lte=end_date_parsed)

    # Firm filter - customers who have transactions with items from selected firms
    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_ids = set(Items.objects.filter(item_firm__in=clean_firms).values_list('pk', flat=True))
            qs = qs.filter(item_id__in=firm_item_ids)

    # Item filter - customers who have transactions with selected items
    if item_filter:
        clean_items = [i for i in item_filter if i.strip()]
        if clean_items:
            # item_filter can be item codes
            item_ids = set(Items.objects.filter(item_code__in=clean_items).values_list('pk', flat=True))
            if item_ids:
                qs = qs.filter(item_id__in=item_ids)
            else:
                qs = qs.none()

    is_admin = (
        request.user.is_superuser
        or request.user.is_staff
        or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    )

    # Salesmen for dropdown (normalized)
    salesmen_raw = (
        AlabamaSalesLine.objects.filter(scope_q)
        .exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )
    all_salesmen = sorted(set(normalize_alabama_salesman(s) or s for s in salesmen_raw if s))

    # Firms from Items
    all_firms = list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='').values_list('item_firm', flat=True).distinct().order_by('item_firm'))

    # Items for filter - from AlabamaSalesLine
    items_from_lines = (
        AlabamaSalesLine.objects.filter(scope_q)
        .exclude(item__isnull=True)
        .values_list('item__item_code', 'item__item_description', 'item__item_upvc')
        .distinct()
    )
    items_dict = {}
    for code, desc, upc in items_from_lines:
        if code and code not in items_dict:
            items_dict[code] = {'code': code, 'description': desc or '', 'upc': upc or ''}
    all_items = list(items_dict.values())

    # Build customer data - aggregate by document first for correct doc count, then by customer
    customer_data = {}

    for year in years:
        year_qs = qs.filter(posting_date__year=year)

        # Apply search filter
        if search_query:
            search_customer_ids = set(
                Customer.objects.filter(
                    Q(customer_code__icontains=search_query) | Q(customer_name__icontains=search_query)
                ).values_list('pk', flat=True)
            )
            if search_customer_ids:
                year_qs = year_qs.filter(customer_id__in=search_customer_ids)
            else:
                year_qs = year_qs.none()

        # Aggregate at document level first (for distinct doc count)
        doc_agg = year_qs.values('customer', 'document_type', 'document_number').annotate(
            doc_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            doc_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
        )
        doc_agg = list(doc_agg)

        # Aggregate by customer
        customer_totals = {}
        customer_salesman = {}
        for row in doc_agg:
            cid = row['customer']
            if not cid:
                continue
            if cid not in customer_totals:
                customer_totals[cid] = {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'doc_count': 0}
            customer_totals[cid]['total_sales'] += row['doc_sales'] or Decimal('0')
            customer_totals[cid]['total_gp'] += row['doc_gp'] or Decimal('0')
            customer_totals[cid]['doc_count'] += 1

        # Get latest salesman per customer for this year
        for cid in customer_totals:
            latest = year_qs.filter(customer_id=cid).order_by('-posting_date').values('sales_employee').first()
            if latest:
                customer_salesman[cid] = normalize_alabama_salesman(latest.get('sales_employee')) or latest.get('sales_employee') or ''

        # Get customer names
        customer_ids = list(customer_totals.keys())
        cust_map = {c.pk: c for c in Customer.objects.filter(pk__in=customer_ids)}

        for cid, totals in customer_totals.items():
            cust = cust_map.get(cid)
            if not cust:
                continue
            code = cust.customer_code or ''
            if not code:
                continue
            key = code
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': cust.customer_name or 'Unknown',
                    'salesman_name': customer_salesman.get(cid, ''),
                    'years': {},
                }
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'document_count': 0,
                }
            customer_data[key]['years'][year]['total_sales'] += totals['total_sales']
            customer_data[key]['years'][year]['total_gp'] += totals['total_gp']
            customer_data[key]['years'][year]['document_count'] += totals['doc_count'] or 0

    # Build customers_list
    customers_list = []
    for key, data in customer_data.items():
        row = {
            'customer_code': data['customer_code'],
            'customer_name': data['customer_name'],
            'salesman_name': data.get('salesman_name', ''),
            'years_data': {},
        }
        for year in years:
            yd = data['years'].get(year, {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'document_count': 0})
            total_sales = yd['total_sales']
            total_gp = yd['total_gp']
            gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
            row['years_data'][year] = {
                'total_sales': total_sales,
                'total_gp': total_gp,
                'gp_percent': gp_percent,
                'document_count': yd.get('document_count', 0),
            }
        customers_list.append(row)

    customers_list = [c for c in customers_list if c['customer_code'] and c['customer_code'].strip()]
    customers_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

    # Totals
    year_totals = {}
    for year in years:
        year_totals[year] = {
            'total_sales': Decimal('0'),
            'total_gp': Decimal('0'),
            'total_gp_percent': Decimal('0'),
        }
        for c in customers_list:
            yd = c['years_data'][year]
            year_totals[year]['total_sales'] += yd['total_sales']
            year_totals[year]['total_gp'] += yd['total_gp']
        if year_totals[year]['total_sales']:
            year_totals[year]['total_gp_percent'] = (year_totals[year]['total_gp'] / year_totals[year]['total_sales']) * 100

    totals_list = [year_totals[y] for y in years]

    for c in customers_list:
        c['year_list'] = [c['years_data'][y] for y in years]

    page_size = 1000
    paginator = Paginator(customers_list, page_size)
    page_obj = paginator.get_page(request.GET.get('page'))
    total_count = len(customers_list)

    is_ajax = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.GET.get('ajax') == '1'
    )

    if is_ajax:
        try:
            table_html = render_to_string('alabama/_customer_analysis_table.html', {
                'customers': page_obj,
                'years': years,
                'is_admin': is_admin,
                'totals_list': totals_list,
            }, request=request)
            pagination_html = ''
            if paginator.num_pages > 1:
                try:
                    pagination_html = render_to_string('alabama/_item_analysis_pagination.html', {'page_obj': page_obj}, request=request)
                except Exception:
                    pass
            filter_display_html = ''
            if salesmen_filter or firm_filter or item_filter:
                filter_display_html = render_to_string('alabama/_customer_analysis_filter_display.html', {
                    'filters': {'salesman': salesmen_filter, 'firm': firm_filter, 'item': item_filter},
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
            logger.error("Alabama customer analysis AJAX error: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    context = {
        'customers': page_obj,
        'page_obj': page_obj,
        'total_count': total_count,
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
            'month': month_filter,
            'start': start_date,
            'end': end_date,
        },
    }
    return render(request, 'alabama/customer_analysis.html', context)


@login_required
def export_customer_analysis_pdf(request):
    """Export Alabama Customer Analysis to PDF - same design as Junaid."""
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from django.http import HttpResponse
    from so.models import Items, Customer

    current_year = datetime.now().year
    years = [current_year, current_year - 1]

    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    item_filter = request.GET.getlist('item')
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()

    qs = AlabamaSalesLine.objects.all().select_related('customer', 'item')
    scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
    qs = qs.filter(scope_q)

    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            qs = qs.filter(_salesman_filter_q(clean, field='sales_employee'))
    if month_filter:
        try:
            month_nums = [int(m) for m in month_filter if m.strip()]
            if month_nums:
                qs = qs.filter(posting_date__month__in=month_nums)
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
        qs = qs.filter(posting_date__gte=start_date_parsed)
    if end_date_parsed:
        qs = qs.filter(posting_date__lte=end_date_parsed)
    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_ids = set(Items.objects.filter(item_firm__in=clean_firms).values_list('pk', flat=True))
            qs = qs.filter(item_id__in=firm_item_ids)
    if item_filter:
        clean_items = [i for i in item_filter if i.strip()]
        if clean_items:
            item_ids = set(Items.objects.filter(item_code__in=clean_items).values_list('pk', flat=True))
            if item_ids:
                qs = qs.filter(item_id__in=item_ids)
            else:
                qs = qs.none()

    is_admin = (
        request.user.is_superuser
        or request.user.is_staff
        or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    )

    customer_data = {}
    for year in years:
        year_qs = qs.filter(posting_date__year=year)
        if search_query:
            search_customer_ids = set(
                Customer.objects.filter(
                    Q(customer_code__icontains=search_query) | Q(customer_name__icontains=search_query)
                ).values_list('pk', flat=True)
            )
            if search_customer_ids:
                year_qs = year_qs.filter(customer_id__in=search_customer_ids)
            else:
                year_qs = year_qs.none()

        doc_agg = year_qs.values('customer', 'document_type', 'document_number').annotate(
            doc_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            doc_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
        )
        doc_agg = list(doc_agg)
        customer_totals = {}
        for row in doc_agg:
            cid = row['customer']
            if not cid:
                continue
            if cid not in customer_totals:
                customer_totals[cid] = {'total_sales': Decimal('0'), 'total_gp': Decimal('0')}
            customer_totals[cid]['total_sales'] += row['doc_sales'] or Decimal('0')
            customer_totals[cid]['total_gp'] += row['doc_gp'] or Decimal('0')

        # Get latest salesman per customer for this year
        customer_salesman = {}
        for cid in customer_totals:
            latest = year_qs.filter(customer_id=cid).order_by('-posting_date').values('sales_employee').first()
            if latest:
                customer_salesman[cid] = normalize_alabama_salesman(latest.get('sales_employee')) or latest.get('sales_employee') or ''

        cust_map = {c.pk: c for c in Customer.objects.filter(pk__in=customer_totals.keys())}
        for cid, totals in customer_totals.items():
            cust = cust_map.get(cid)
            if not cust:
                continue
            code = cust.customer_code or ''
            if not code:
                continue
            key = code
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': cust.customer_name or 'Unknown',
                    'salesman_name': customer_salesman.get(cid, ''),
                    'years': {}
                }
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {'total_sales': Decimal('0'), 'total_gp': Decimal('0')}
            customer_data[key]['years'][year]['total_sales'] += totals['total_sales']
            customer_data[key]['years'][year]['total_gp'] += totals['total_gp']

    customers_list = []
    for key, data in customer_data.items():
        row = {
            'customer_code': data['customer_code'],
            'customer_name': data['customer_name'],
            'salesman_name': data.get('salesman_name', ''),
            'years_data': {}
        }
        for year in years:
            yd = data['years'].get(year, {'total_sales': Decimal('0'), 'total_gp': Decimal('0')})
            total_sales = yd['total_sales']
            total_gp = yd['total_gp']
            gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
            row['years_data'][year] = {'total_sales': total_sales, 'total_gp': total_gp, 'gp_percent': gp_percent}
        customers_list.append(row)
    customers_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

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

    # Logo from media/alabama.jpeg
    logo = _get_alabama_logo()
    if logo:
        elements.append(logo)
        elements.append(Spacer(1, 0.2*inch))

    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        textColor=colors.HexColor('#2C3E50'),
        spaceAfter=12,
        alignment=TA_CENTER
    )
    elements.append(Paragraph("Alabama Customer Performance Analysis", title_style))
    elements.append(Spacer(1, 0.1*inch))

    # Filter info
    filter_info = []
    if salesmen_filter:
        filter_info.append(f"Salesmen: {', '.join(salesmen_filter[:3])}{'...' if len(salesmen_filter) > 3 else ''}")
    if firm_filter:
        filter_info.append(f"Firms: {', '.join(firm_filter[:2])}{'...' if len(firm_filter) > 2 else ''}")
    if item_filter:
        filter_info.append(f"Items: {', '.join(item_filter[:2])}{'...' if len(item_filter) > 2 else ''}")
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

    # Wrap style for text cells (customer name, code, salesman)
    wrap_style = ParagraphStyle(
        'WrapStyle',
        parent=styles['Normal'],
        fontSize=7,
        leading=8,
        alignment=TA_LEFT
    )

    def _safe_para(text, style=wrap_style):
        if not text:
            return Paragraph('', style)
        from xml.sax.saxutils import escape
        return Paragraph(escape(str(text)), style)

    # Table
    header = ['Customer Name', 'Customer Code', 'Salesman']
    for y in years:
        if is_admin:
            header.extend([f'{y} Sales', f'{y} GP', f'{y} GP%'])
        else:
            header.append(f'{y} Sales')
    table_data = [header]
    for c in customers_list:
        r = [
            _safe_para(c.get('customer_name') or ''),
            _safe_para(c.get('customer_code') or ''),
            _safe_para(c.get('salesman_name') or '')
        ]
        for year in years:
            yd = c['years_data'][year]
            r.append(f"{float(yd['total_sales']):,.2f}")
            if is_admin:
                r.append(f"{float(yd['total_gp']):,.2f}")
                r.append(f"{float(yd['gp_percent']):.2f}%")
        table_data.append(r)

    # Column widths - fit within landscape A4 (11.69" - 1" margins = 10.69")
    col_widths = [2.2*inch, 0.9*inch, 1*inch]  # Customer Name, Code, Salesman (wider for wrapping)
    for _ in years:
        col_widths.append(0.75*inch)  # Sales
        if is_admin:
            col_widths.append(0.65*inch)  # GP
            col_widths.append(0.55*inch)  # GP%

    available_width = 10.69*inch
    total_width = sum(col_widths)
    if total_width > available_width:
        scale_factor = float(available_width / total_width)
        col_widths = [w * scale_factor for w in col_widths]

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7),
        ('FONTSIZE', (0, 1), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
    ]))
    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="alabama_customer_analysis.pdf"'
    return response
