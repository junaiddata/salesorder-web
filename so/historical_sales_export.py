"""Admin-only PDF / Excel export for the Historical Sales Analysis dashboard (historical-sales/analysis/).

Self-contained on purpose: the dashboard view in historical_sales_views.py is not touched. The filter rules below mirror the
dashboard's (store, category, salesman, month, start/end, year/period) so an export matches what is on screen.
"""
import calendar
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, Max, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseForbidden

from .models import HistoricalSalesLine

MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
_ZERO = Value(0, output_field=DecimalField())


def _is_admin(user):
    try:
        role = user.role
    except Exception:
        role = None
    return bool(user.is_authenticated and (user.is_superuser or user.is_staff or (role is not None and getattr(role, 'role', None) == 'Admin')))


def _parse_date(s):
    if not s:
        return None
    try:
        if len(s) == 7:
            return datetime.strptime(s + '-01', '%Y-%m-%d').date()
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def _filtered_lines(request):
    """HistoricalSalesLine queryset with the same filters the dashboard applies. Also returns a list of readable filter labels."""
    qs = HistoricalSalesLine.objects.all()
    labels = []
    today = date.today()
    store = request.GET.get('store', '').strip()
    categories = [c for c in request.GET.getlist('category') if c.strip()]
    salesmen = [s for s in request.GET.getlist('salesman') if s.strip()]
    months = request.GET.getlist('month')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    period = request.GET.get('period', '').strip()
    year = request.GET.get('year', 'Total').strip()

    if store and store != 'Total':
        if store == 'HO':
            qs = qs.exclude(sales_employee__istartswith='R.').exclude(sales_employee__istartswith='E.')
        elif store == 'Others':
            qs = qs.filter(Q(sales_employee__istartswith='R.') | Q(sales_employee__istartswith='E.'))
        labels.append(f'Store: {store}')
    if categories:
        cond = Q()
        for cat in categories:
            if cat == 'Trading':
                cond |= Q(sales_employee__istartswith='A.')
            elif cat == 'Project':
                cond |= Q(sales_employee__istartswith='B.')
            elif cat == 'Retail':
                cond |= Q(sales_employee__istartswith='R.')
            elif cat == 'Export':
                cond |= Q(sales_employee__istartswith='E.')
            elif cat == 'Others':
                cond |= (~Q(sales_employee__istartswith='A.') & ~Q(sales_employee__istartswith='B.')
                         & ~Q(sales_employee__istartswith='R.') & ~Q(sales_employee__istartswith='E.'))
        qs = qs.filter(cond)
        labels.append('Category: ' + ', '.join(categories))
    if salesmen:
        qs = qs.filter(sales_employee__in=salesmen)
        labels.append('Salesman: ' + ', '.join(salesmen))
    try:
        month_nums = [int(m) for m in months if str(m).strip()]
    except (ValueError, TypeError):
        month_nums = []
    if month_nums:
        qs = qs.filter(posting_date__month__in=month_nums)
        labels.append('Month: ' + ', '.join(MONTH_NAMES[m - 1] for m in sorted(set(month_nums)) if 1 <= m <= 12))
    start_date, end_date = _parse_date(start), _parse_date(end)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
        labels.append(f'From: {start_date}')
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)
        labels.append(f'To: {end_date}')

    # period / year (same precedence as the dashboard)
    if period == 'today':
        qs = qs.filter(posting_date=today)
        labels.append('Period: today')
    elif period == 'month':
        qs = qs.filter(posting_date__year=today.year, posting_date__month=today.month)
        labels.append('Period: this month')
    elif period == 'year':
        qs = qs.filter(posting_date__year=today.year)
        labels.append('Period: this year')
    elif period in ('all', '') and year and year != 'Total':
        try:
            qs = qs.filter(posting_date__year=int(year))
            labels.append(f'Year: {year}')
        except (ValueError, TypeError):
            pass
    return qs, labels


def _build_export_data(request):
    qs, labels = _filtered_lines(request)
    agg = qs.aggregate(sales=Coalesce(Sum('net_sales'), _ZERO), gp=Coalesce(Sum('gross_profit'), _ZERO),
                       docs=Count('document_number', distinct=True))
    total_sales, total_gp = agg['sales'] or Decimal('0'), agg['gp'] or Decimal('0')

    customers = [{
        'code': r['customer__customer_code'] or '-', 'name': r['customer__customer_name'] or 'Unknown',
        'salesman': r['salesman'] or '-', 'sales': r['sales'] or Decimal('0'), 'gp': r['gp'] or Decimal('0'),
        'docs': r['docs'] or 0,
    } for r in (qs.values('customer__customer_code', 'customer__customer_name')
                .annotate(sales=Coalesce(Sum('net_sales'), _ZERO), gp=Coalesce(Sum('gross_profit'), _ZERO),
                          docs=Count('document_number', distinct=True), salesman=Max('sales_employee'))
                .order_by('-sales')[:5])]

    items = [{
        'code': r['item__item_code'] or '-', 'name': r['item__item_description'] or 'Unknown',
        'qty': r['qty'] or Decimal('0'), 'sales': r['sales'] or Decimal('0'), 'gp': r['gp'] or Decimal('0'),
    } for r in (qs.values('item__item_code', 'item__item_description')
                .annotate(sales=Coalesce(Sum('net_sales'), _ZERO), gp=Coalesce(Sum('gross_profit'), _ZERO),
                          qty=Coalesce(Sum('quantity'), _ZERO))
                .order_by('-sales')[:5])]

    salesmen = [{
        'name': r['sales_employee'] or '-', 'sales': r['sales'] or Decimal('0'), 'gp': r['gp'] or Decimal('0'),
        'docs': r['docs'] or 0,
    } for r in (qs.exclude(sales_employee__isnull=True).exclude(sales_employee='').values('sales_employee')
                .annotate(sales=Coalesce(Sum('net_sales'), _ZERO), gp=Coalesce(Sum('gross_profit'), _ZERO),
                          docs=Count('document_number', distinct=True))
                .order_by('-sales'))]

    # Calendar (same selection as the dashboard: cal_year + one or more cal_month), grouped in ONE query.
    today = date.today()
    try:
        cal_year = int(request.GET.get('cal_year', '').strip() or today.year)
    except (ValueError, TypeError):
        cal_year = today.year
    cal_months = []
    for raw in request.GET.getlist('cal_month'):
        try:
            m = int(str(raw).strip())
            if 1 <= m <= 12:
                cal_months.append(m)
        except (ValueError, TypeError):
            continue
    cal_months = sorted(set(cal_months)) or [today.month]
    day_rows = (qs.filter(posting_date__year=cal_year, posting_date__month__in=cal_months)
                .values('posting_date')
                .annotate(sales=Coalesce(Sum('net_sales'), _ZERO), gp=Coalesce(Sum('gross_profit'), _ZERO),
                          docs=Count('document_number', distinct=True))
                .order_by('posting_date'))
    days = [{'date': r['posting_date'], 'sales': r['sales'] or Decimal('0'), 'gp': r['gp'] or Decimal('0'),
             'docs': r['docs'] or 0} for r in day_rows if r['posting_date']]
    calendar_label = ', '.join(calendar.month_name[m] for m in cal_months) + f' {cal_year}'

    return {
        'filters': labels, 'total_sales': total_sales, 'total_gp': total_gp, 'docs': agg['docs'] or 0,
        'gp_pct': (float(total_gp) / float(total_sales) * 100) if total_sales else 0.0,
        'customers': customers, 'items': items, 'salesmen': salesmen, 'days': days, 'calendar_label': calendar_label,
    }


def _num(v, places=2):
    return f'{float(v):,.{places}f}'


@login_required
def export_historical_sales_analysis_pdf(request):
    if not _is_admin(request.user):
        return HttpResponseForbidden('Admin only.')
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    d = _build_export_data(request)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=0.5 * inch, leftMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    title = ParagraphStyle('HSTitle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor('#2C3E50'),
                           alignment=TA_CENTER, spaceAfter=8)
    h2 = ParagraphStyle('HSH2', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#2C3E50'), spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle('HSSmall', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#555555'))

    def table(rows, widths, right_cols=()):
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2C3E50')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                 ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, -1), 8),
                 ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#BBBBBB')),
                 ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F4F6F8')]),
                 ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]
        for c in right_cols:
            style.append(('ALIGN', (c, 0), (c, -1), 'RIGHT'))
        t.setStyle(TableStyle(style))
        return t

    els = [Paragraph('Historical Sales Analysis (2020-2023)', title),
           Paragraph('Filters: ' + ('; '.join(d['filters']) if d['filters'] else 'none (all data)'), small),
           Paragraph(f'Generated {datetime.now():%d %b %Y %H:%M}', small), Spacer(1, 0.1 * inch)]

    els.append(table([['Net Sales', 'Gross Profit', 'GP %', 'Documents'],
                      [_num(d['total_sales']), _num(d['total_gp']), f"{d['gp_pct']:.1f}%", str(d['docs'])]],
                     [2.2 * inch] * 4, right_cols=(0, 1, 2, 3)))

    els.append(Paragraph('Top 5 Customers', h2))
    rows = [['Code', 'Customer', 'Salesman', 'Net Sales', 'Gross Profit', 'Docs']] + [
        [c['code'], c['name'][:45], c['salesman'], _num(c['sales']), _num(c['gp']), str(c['docs'])] for c in d['customers']]
    els.append(table(rows, [0.9 * inch, 3.3 * inch, 1.6 * inch, 1.3 * inch, 1.3 * inch, 0.6 * inch], right_cols=(3, 4, 5)))

    els.append(Paragraph('Top 5 Items', h2))
    rows = [['Item Code', 'Description', 'Qty', 'Net Sales', 'Gross Profit']] + [
        [i['code'], i['name'][:55], _num(i['qty'], 0), _num(i['sales']), _num(i['gp'])] for i in d['items']]
    els.append(table(rows, [1.2 * inch, 4.3 * inch, 0.9 * inch, 1.3 * inch, 1.3 * inch], right_cols=(2, 3, 4)))

    els.append(Paragraph('Salesman Summary', h2))
    rows = [['Salesman', 'Net Sales', 'Gross Profit', 'Docs']] + [
        [s['name'], _num(s['sales']), _num(s['gp']), str(s['docs'])] for s in d['salesmen']]
    els.append(table(rows, [3.0 * inch, 1.8 * inch, 1.8 * inch, 0.8 * inch], right_cols=(1, 2, 3)))

    els.append(Paragraph(f"Daily Sales - {d['calendar_label']}", h2))
    rows = [['Date', 'Net Sales', 'Gross Profit', 'GP %', 'Docs']] + [
        [f"{x['date']:%d %b %Y}", _num(x['sales']), _num(x['gp']),
         f"{(float(x['gp']) / float(x['sales']) * 100) if x['sales'] else 0:.1f}%", str(x['docs'])] for x in d['days']]
    if len(rows) == 1:
        rows.append(['No sales in this period', '', '', '', ''])
    els.append(table(rows, [1.6 * inch, 1.8 * inch, 1.8 * inch, 0.9 * inch, 0.8 * inch], right_cols=(1, 2, 3, 4)))

    doc.build(els)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Historical_Sales_Analysis_{datetime.now():%Y%m%d_%H%M%S}.pdf"'
    return response


@login_required
def export_historical_sales_analysis_excel(request):
    if not _is_admin(request.user):
        return HttpResponseForbidden('Admin only.')
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    d = _build_export_data(request)
    wb = Workbook()
    head_fill = PatternFill('solid', fgColor='2C3E50')

    def sheet(ws, headers, rows, money_cols=(), widths=None):
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = head_fill
            c.alignment = Alignment(horizontal='center')
        for r in rows:
            ws.append(r)
        for col in money_cols:
            for cell in ws[get_column_letter(col)][1:]:
                cell.number_format = '#,##0.00'
        for i, w in enumerate(widths or [], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = 'A2'

    ws = wb.active
    ws.title = 'Summary'
    sheet(ws, ['Metric', 'Value'], [
        ['Net Sales', float(d['total_sales'])], ['Gross Profit', float(d['total_gp'])],
        ['GP %', round(d['gp_pct'], 1)], ['Documents', d['docs']],
        ['Filters', '; '.join(d['filters']) if d['filters'] else 'none (all data)'],
        ['Generated', f'{datetime.now():%d %b %Y %H:%M}']], widths=[18, 60])
    for r in (2, 3):
        ws.cell(row=r, column=2).number_format = '#,##0.00'

    sheet(wb.create_sheet('Top Customers'), ['Code', 'Customer', 'Salesman', 'Net Sales', 'Gross Profit', 'Docs'],
          [[c['code'], c['name'], c['salesman'], float(c['sales']), float(c['gp']), c['docs']] for c in d['customers']],
          money_cols=(4, 5), widths=[14, 50, 24, 16, 16, 8])
    sheet(wb.create_sheet('Top Items'), ['Item Code', 'Description', 'Qty', 'Net Sales', 'Gross Profit'],
          [[i['code'], i['name'], float(i['qty']), float(i['sales']), float(i['gp'])] for i in d['items']],
          money_cols=(4, 5), widths=[16, 60, 10, 16, 16])
    sheet(wb.create_sheet('Salesmen'), ['Salesman', 'Net Sales', 'Gross Profit', 'Docs'],
          [[s['name'], float(s['sales']), float(s['gp']), s['docs']] for s in d['salesmen']],
          money_cols=(2, 3), widths=[30, 16, 16, 8])
    sheet(wb.create_sheet('Daily Sales'), ['Date', 'Net Sales', 'Gross Profit', 'GP %', 'Docs'],
          [[x['date'], float(x['sales']), float(x['gp']),
            round((float(x['gp']) / float(x['sales']) * 100) if x['sales'] else 0, 1), x['docs']] for x in d['days']],
          money_cols=(2, 3), widths=[14, 16, 16, 8, 8])
    for row in wb['Daily Sales'].iter_rows(min_row=2, max_col=1):
        row[0].number_format = 'DD-MMM-YYYY'

    buf = BytesIO()
    wb.save(buf)
    response = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Historical_Sales_Analysis_{datetime.now():%Y%m%d_%H%M%S}.xlsx"'
    return response
