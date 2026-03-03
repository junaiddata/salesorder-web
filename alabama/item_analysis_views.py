"""
Alabama Item Analysis - separate view file.
Same behavior as SO item_analysis but:
- Years: 2025 and 2026 only (no 2024)
- Data source: AlabamaSalesLine (not SAP AR Invoice/Credit Memo)
- Filters: search, salesman, firm, month, start/end date, store, category (store/category not applied to Alabama data)
"""
import logging
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Q, Avg
from django.db.models.functions import Coalesce
from django.db.models import DecimalField, Value
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render

from .models import AlabamaSalesLine, AlabamaSalesmanMapping
from .views import alabama_salesman_scope_q, normalize_alabama_salesman

logger = logging.getLogger(__name__)


def _get_salesman_mapping_dict():
    """Load salesman mappings from DB (cached)."""
    try:
        rows = AlabamaSalesmanMapping.objects.all().values_list('raw_name', 'normalized_name')
        return {r[0].lower(): r[1] for r in rows}
    except Exception:
        return {}


def _salesman_filter_q(selected_salesmen, field='sales_employee'):
    """
    Build Q filter for sales_employee matching any selected salesman.
    Normalizes: if user selects "KADER", matches both "KADER" and "A.KADER" (raw names that map to KADER).
    """
    if not selected_salesmen:
        return Q()
    mapping = _get_salesman_mapping_dict()
    norm_to_raw = {}
    for raw, norm in mapping.items():
        n = (norm or '').strip()
        if n:
            norm_to_raw.setdefault(n.lower(), []).append(raw)
    names_to_match = set()
    for sel in selected_salesmen:
        s = (sel or '').strip()
        if not s:
            continue
        names_to_match.add(s)
        for raw in norm_to_raw.get(s.lower(), []):
            names_to_match.add(raw)
    q = Q()
    for n in names_to_match:
        q |= Q(**{f'{field}__iexact': n})
    return q


@login_required
def item_analysis(request):
    """
    Alabama Item Analysis - 2025 and 2026 only.
    Data from AlabamaSalesLine.
    """
    from so.models import Items

    current_year = datetime.now().year
    years = [current_year, current_year - 1]

    # Filters
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    store_filter = request.GET.get('store', '').strip()
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()
    category_filter = request.GET.get('category', 'All').strip()

    # Base queryset with salesman scope
    qs = AlabamaSalesLine.objects.all().select_related('item')
    scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
    qs = qs.filter(scope_q)

    # Salesman filter (normalized)
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            qs = qs.filter(_salesman_filter_q(clean_salesmen, field='sales_employee'))

    # Month filter
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

    is_admin = (
        request.user.is_superuser
        or request.user.is_staff
        or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    )

    # Salesmen for dropdown (normalized, unique)
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
    all_firms = Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='').values_list('item_firm', flat=True).distinct().order_by('item_firm')

    # Build item data
    item_data = {}

    for year in years:
        year_qs = qs.filter(posting_date__year=year)
        agg = year_qs.values('item').annotate(
            total_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
            total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
        )
        agg = list(agg)

        # Apply firm filter
        if firm_filter:
            clean_firms = [f for f in firm_filter if f.strip()]
            if clean_firms:
                firm_item_ids = set(Items.objects.filter(item_firm__in=clean_firms).values_list('pk', flat=True))
                agg = [a for a in agg if a['item'] in firm_item_ids]

        # Apply search filter
        if search_query:
            search_item_ids = set(
                Items.objects.filter(
                    Q(item_code__icontains=search_query)
                    | Q(item_description__icontains=search_query)
                    | Q(item_upvc__icontains=search_query)
                ).values_list('pk', flat=True)
            )
            agg = [a for a in agg if a['item'] in search_item_ids]

        for row in agg:
            item_id = row['item']
            if not item_id:
                continue
            try:
                item = Items.objects.get(pk=item_id)
            except Items.DoesNotExist:
                continue
            code = item.item_code or ''
            if not code:
                continue
            key = code
            if key not in item_data:
                item_data[key] = {
                    'item_code': code,
                    'item_description': item.item_description or 'Unknown',
                    'upc_code': getattr(item, 'item_upvc', '') or '',
                    'years': {},
                }
            if year not in item_data[key]['years']:
                item_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'total_quantity': Decimal('0'),
                }
            item_data[key]['years'][year]['total_sales'] += row['total_sales'] or Decimal('0')
            item_data[key]['years'][year]['total_gp'] += row['total_gp'] or Decimal('0')
            item_data[key]['years'][year]['total_quantity'] += row['total_quantity'] or Decimal('0')

    # Calculate GP%, avg rate, build items_list
    items_list = []
    for key, data in item_data.items():
        item_row = {
            'item_code': data['item_code'],
            'item_description': data['item_description'],
            'upc_code': data.get('upc_code', ''),
            'years_data': {},
        }
        for year in years:
            if year in data['years']:
                yd = data['years'][year]
                total_sales = yd['total_sales']
                total_gp = yd['total_gp']
                total_quantity = yd['total_quantity']
                gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
                avg_rate = (total_sales / total_quantity) if total_quantity else Decimal('0')
                item_row['years_data'][year] = {
                    'total_sales': total_sales,
                    'total_gp': total_gp,
                    'gp_percent': gp_percent,
                    'avg_rate': avg_rate,
                    'total_quantity': total_quantity,
                }
            else:
                item_row['years_data'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'gp_percent': Decimal('0'),
                    'avg_rate': Decimal('0'),
                    'total_quantity': Decimal('0'),
                }
        items_list.append(item_row)

    items_list = [i for i in items_list if i['item_code'] and i['item_code'].strip()]
    items_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

    # Totals
    year_totals = {}
    for year in years:
        year_totals[year] = {
            'total_sales': Decimal('0'),
            'total_gp': Decimal('0'),
            'total_quantity': Decimal('0'),
            'total_avg_rate': Decimal('0'),
            'total_gp_percent': Decimal('0'),
        }
        for item in items_list:
            yd = item['years_data'][year]
            year_totals[year]['total_sales'] += yd['total_sales']
            year_totals[year]['total_gp'] += yd['total_gp']
            year_totals[year]['total_quantity'] += yd['total_quantity']
        if year_totals[year]['total_quantity']:
            year_totals[year]['total_avg_rate'] = year_totals[year]['total_sales'] / year_totals[year]['total_quantity']
        if year_totals[year]['total_sales']:
            year_totals[year]['total_gp_percent'] = (year_totals[year]['total_gp'] / year_totals[year]['total_sales']) * 100

    totals_list = [year_totals[y] for y in years]

    for item in items_list:
        item['year_list'] = [item['years_data'][y] for y in years]

    page_size = 1000
    paginator = Paginator(items_list, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    total_count = len(items_list)

    is_ajax = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.GET.get('ajax') == '1'
    )

    if is_ajax:
        try:
            from django.template.loader import render_to_string
            table_html = render_to_string('alabama/_item_analysis_table.html', {
                'items': page_obj,
                'years': years,
                'is_admin': is_admin,
                'totals_list': totals_list,
            }, request=request)
            pagination_html = ''
            if paginator.num_pages > 1:
                try:
                    pagination_html = render_to_string('alabama/_item_analysis_pagination.html', {
                        'page_obj': page_obj,
                    }, request=request)
                except Exception:
                    pass
            filter_display_html = ''
            if salesmen_filter or firm_filter:
                filter_display_html = render_to_string('alabama/_item_analysis_filter_display.html', {
                    'filters': {'salesman': salesmen_filter, 'firm': firm_filter},
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
                'items_count': len(page_obj),
            })
        except Exception as e:
            logger.error("Alabama item analysis AJAX error: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    context = {
        'items': page_obj,
        'page_obj': page_obj,
        'total_count': total_count,
        'years': years,
        'is_admin': is_admin,
        'current_year': current_year,
        'salesmen': all_salesmen,
        'firms': all_firms,
        'totals_list': totals_list,
        'filters': {
            'q': search_query,
            'salesman': salesmen_filter,
            'firm': firm_filter,
            'store': store_filter,
            'month': month_filter,
            'start': start_date,
            'end': end_date,
            'category': category_filter,
        },
    }
    return render(request, 'alabama/item_analysis.html', context)


def _get_alabama_logo():
    """Load Alabama logo from media/alabama.jpeg. Returns ReportLab Image or None."""
    import os
    from reportlab.platypus import Image
    from reportlab.lib.units import inch
    from django.conf import settings
    logo_path = os.path.join(settings.BASE_DIR, 'media', 'alabama.jpeg')
    if os.path.exists(logo_path):
        try:
            return Image(logo_path, width=2*inch, height=0.7*inch)
        except Exception:
            pass
    return None


@login_required
def export_item_analysis_pdf(request):
    """Export Alabama Item Analysis to PDF - same design as Junaid."""
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from django.http import HttpResponse
    from so.models import Items

    current_year = datetime.now().year
    years = [current_year, current_year - 1]
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()

    qs = AlabamaSalesLine.objects.all().select_related('item')
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

    is_admin = (
        request.user.is_superuser
        or request.user.is_staff
        or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    )

    item_data = {}
    for year in years:
        year_qs = qs.filter(posting_date__year=year)
        agg = year_qs.values('item').annotate(
            total_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
            total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
        )
        agg = list(agg)
        if firm_filter:
            clean_firms = [f for f in firm_filter if f.strip()]
            if clean_firms:
                firm_ids = set(Items.objects.filter(item_firm__in=clean_firms).values_list('pk', flat=True))
                agg = [a for a in agg if a['item'] in firm_ids]
        if search_query:
            search_ids = set(
                Items.objects.filter(
                    Q(item_code__icontains=search_query)
                    | Q(item_description__icontains=search_query)
                    | Q(item_upvc__icontains=search_query)
                ).values_list('pk', flat=True)
            )
            agg = [a for a in agg if a['item'] in search_ids]
        for row in agg:
            try:
                item = Items.objects.get(pk=row['item'])
            except Items.DoesNotExist:
                continue
            code = item.item_code or ''
            if not code:
                continue
            key = code
            if key not in item_data:
                item_data[key] = {
                    'item_code': code,
                    'item_description': item.item_description or 'Unknown',
                    'years': {},
                }
            if year not in item_data[key]['years']:
                item_data[key]['years'][year] = {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'total_quantity': Decimal('0')}
            item_data[key]['years'][year]['total_sales'] += row['total_sales'] or Decimal('0')
            item_data[key]['years'][year]['total_gp'] += row['total_gp'] or Decimal('0')
            item_data[key]['years'][year]['total_quantity'] += row['total_quantity'] or Decimal('0')

    items_list = []
    for key, data in item_data.items():
        row = {'item_code': data['item_code'], 'item_description': data['item_description'], 'years_data': {}}
        for year in years:
            yd = data['years'].get(year, {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'total_quantity': Decimal('0')})
            total_sales = yd['total_sales']
            total_gp = yd['total_gp']
            total_quantity = yd['total_quantity']
            gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
            avg_rate = (total_sales / total_quantity) if total_quantity else Decimal('0')
            row['years_data'][year] = {
                'total_sales': total_sales,
                'total_gp': total_gp,
                'gp_percent': gp_percent,
                'avg_rate': avg_rate,
                'total_quantity': total_quantity,
            }
        items_list.append(row)
    items_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

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
    elements.append(Paragraph("Alabama Item Performance Analysis", title_style))
    elements.append(Spacer(1, 0.1*inch))

    # Filter info
    filter_info = []
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

    # Wrap style for text cells (item code, description)
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
    header_data = [['Item Code', 'Description']]
    for y in years:
        if is_admin:
            header_data[0].extend([f'{y} Sales', f'{y} GP', f'{y} GP%', f'{y} Qty', f'{y} Avg Rate'])
        else:
            header_data[0].extend([f'{y} Sales', f'{y} Qty', f'{y} Avg Rate'])
    table_data = [header_data[0]]
    for item in items_list:
        r = [
            _safe_para(item.get('item_code') or ''),
            _safe_para(item.get('item_description') or '')
        ]
        for year in years:
            yd = item['years_data'][year]
            r.append(f"{float(yd['total_sales']):,.2f}")
            if is_admin:
                r.append(f"{float(yd['total_gp']):,.2f}")
                r.append(f"{float(yd['gp_percent']):.2f}%")
            r.append(f"{float(yd['total_quantity']):,.0f}")
            r.append(f"{float(yd['avg_rate']):,.2f}")
        table_data.append(r)

    # Column widths - fit within landscape A4 (11.69" - 1" margins = 10.69")
    col_widths = [0.9*inch, 2.2*inch]  # Item Code, Description (wider for wrapping)
    for _ in years:
        col_widths.append(0.7*inch)   # Sales
        if is_admin:
            col_widths.append(0.6*inch)  # GP
            col_widths.append(0.55*inch) # GP%
        col_widths.append(0.5*inch)   # Qty
        col_widths.append(0.6*inch)   # Avg Rate

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
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
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
    response['Content-Disposition'] = 'attachment; filename="alabama_item_analysis.pdf"'
    return response


@login_required
def export_item_analysis_excel(request):
    """Export Alabama Item Analysis to Excel (2025, 2026 only)."""
    from django.http import HttpResponse
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from so.models import Items

    current_year = datetime.now().year
    years = [current_year, current_year - 1]
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    month_filter = request.GET.getlist('month')
    start_date = request.GET.get('start', '').strip()
    end_date = request.GET.get('end', '').strip()

    qs = AlabamaSalesLine.objects.all().select_related('item')
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

    is_admin = (
        request.user.is_superuser
        or request.user.is_staff
        or (hasattr(request.user, 'role') and request.user.role.role == 'Admin')
    )

    item_data = {}
    for year in years:
        year_qs = qs.filter(posting_date__year=year)
        agg = year_qs.values('item').annotate(
            total_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
            total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
        )
        agg = list(agg)
        if firm_filter:
            clean_firms = [f for f in firm_filter if f.strip()]
            if clean_firms:
                firm_ids = set(Items.objects.filter(item_firm__in=clean_firms).values_list('pk', flat=True))
                agg = [a for a in agg if a['item'] in firm_ids]
        if search_query:
            search_ids = set(
                Items.objects.filter(
                    Q(item_code__icontains=search_query)
                    | Q(item_description__icontains=search_query)
                    | Q(item_upvc__icontains=search_query)
                ).values_list('pk', flat=True)
            )
            agg = [a for a in agg if a['item'] in search_ids]
        for row in agg:
            try:
                item = Items.objects.get(pk=row['item'])
            except Items.DoesNotExist:
                continue
            code = item.item_code or ''
            if not code:
                continue
            key = code
            if key not in item_data:
                item_data[key] = {'item_code': code, 'item_description': item.item_description or 'Unknown', 'years': {}}
            if year not in item_data[key]['years']:
                item_data[key]['years'][year] = {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'total_quantity': Decimal('0')}
            item_data[key]['years'][year]['total_sales'] += row['total_sales'] or Decimal('0')
            item_data[key]['years'][year]['total_gp'] += row['total_gp'] or Decimal('0')
            item_data[key]['years'][year]['total_quantity'] += row['total_quantity'] or Decimal('0')

    items_list = []
    for key, data in item_data.items():
        row = {'item_code': data['item_code'], 'item_description': data['item_description'], 'years_data': {}}
        for year in years:
            yd = data['years'].get(year, {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'total_quantity': Decimal('0')})
            total_sales = yd['total_sales']
            total_gp = yd['total_gp']
            total_quantity = yd['total_quantity']
            gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
            avg_rate = (total_sales / total_quantity) if total_quantity else Decimal('0')
            row['years_data'][year] = {
                'total_sales': total_sales,
                'total_gp': total_gp,
                'gp_percent': gp_percent,
                'avg_rate': avg_rate,
                'total_quantity': total_quantity,
            }
        items_list.append(row)
    items_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Item Analysis"
    header = ['Item Code', 'Description']
    for y in years:
        if is_admin:
            header.extend([f'{y} Sales', f'{y} GP', f'{y} GP%', f'{y} Qty', f'{y} Avg Rate'])
        else:
            header.extend([f'{y} Sales', f'{y} Qty', f'{y} Avg Rate'])
    ws.append(header)
    for item in items_list:
        r = [item['item_code'], item['item_description'] or '']
        for year in years:
            yd = item['years_data'][year]
            r.append(float(yd['total_sales']))
            if is_admin:
                r.append(float(yd['total_gp']))
                r.append(float(yd['gp_percent']))
            r.append(float(yd['total_quantity']))
            r.append(float(yd['avg_rate']))
        ws.append(r)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="alabama_item_analysis.xlsx"'
    return response
