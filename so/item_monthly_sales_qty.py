"""
Item Monthly Sales - the "Sold Qty" tab and its "Proposed Budget 2027" column.

Kept apart from the Sales Amount / Unit Price code, so the budget table is only touched by this tab.

  * Sold qty = invoice qty + credit-memo qty (returns and cancellation documents are stored negative).
  * Every item of the Items master is listed, with or without sales. Brand / Search pick ITEMS; the sales of a
    picked item are always its full sales for the year (within the salesman / store filters and user scope).
  * The budget is one company-wide whole number per item. Everyone sees it, only Role = Admin may change it.
  * PDF without a brand filter lists only items that have sales or a budget (a PDF cannot hold the whole master).
"""
import json
import re
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .brandwise_sales_analysis_views import MONTH_NAMES_SHORT
from .item_monthly_sales_export import MONTHS, PDF_ROW_LIMIT
from .models import ItemBudget, Items, SAPARCreditMemoItem, SAPARInvoiceItem

BUDGET_YEAR = 2027
BUDGET_LABEL = f'Proposed Budget {BUDGET_YEAR}'
MAX_BUDGET = 999_999_999
ZERO = Decimal('0')

_WHOLE_NUMBER = re.compile(r'[0-9]+|[0-9]{1,3}(,[0-9]{3})+')


def can_edit_budget(user):
    """Only users whose Role is Admin."""
    try:
        return user.role.role == 'Admin'
    except Exception:   # no Role row
        return False


def _qty_text(v):
    """1,250  /  12.5  - decimals only when the quantity has them."""
    s = f'{v:,.2f}'
    return s[:-3] if s.endswith('.00') else s


def build_rows(f):
    """One row per item matching Brand / Search, items with sales first (highest net qty first)."""
    sold = {}   # item_code -> {'m': [12 qty], 'descs': set, 'brand': str}

    def _add(qs, month_key):
        rows = (qs.values('item_code', 'item_description', 'item__item_firm', month_key)
                .annotate(qty=Sum('quantity')))
        for r in rows:
            code = (r['item_code'] or '').strip()
            month = r[month_key]
            if not code or not month:
                continue
            it = sold.get(code)
            if it is None:
                it = sold[code] = {'m': [ZERO] * 12, 'descs': [], 'brand': ''}
            if r['item_description'] and r['item_description'] not in it['descs']:
                it['descs'].append(r['item_description'])
            if not it['brand'] and r['item__item_firm']:
                it['brand'] = r['item__item_firm']
            it['m'][month - 1] += r['qty'] or ZERO

    _add(f['inv_items_all'], 'invoice__posting_date__month')
    _add(f['cm_items_all'], 'credit_memo__posting_date__month')

    master = {}
    for code, desc, firm in Items.objects.values_list('item_code', 'item_description', 'item_firm').iterator():
        code = (code or '').strip()
        if code:
            master[code] = (desc or '', firm or '')
    budgets = dict(ItemBudget.objects.filter(year=BUDGET_YEAR).values_list('item_code', 'qty'))

    brands = set(f['brand_filter'])
    needle = f['q'].lower()
    rows = []
    for code in set(master) | set(sold) | set(budgets):
        sale = sold.get(code)
        master_desc, master_brand = master.get(code, ('', ''))
        brand = master_brand or (sale['brand'] if sale else '')
        if brands and brand not in brands:
            continue
        line_descs = sale['descs'] if sale else []
        if needle and not any(needle in text.lower() for text in [code, master_desc] + line_descs):
            continue
        months = sale['m'] if sale else [ZERO] * 12
        rows.append({
            'item_code': code,
            'description': line_descs[0] if line_descs else master_desc,
            'brand': brand,
            'm': months,
            'total': sum(months, ZERO),
            'sold': sale is not None,
            'budget': budgets.get(code),
        })
    rows.sort(key=lambda it: (not it['sold'], -it['total'], it['item_code']))
    return rows


def qty_view(request, f, page_size, export):
    """The Sold Qty tab of item_monthly_sales (screen, Excel and PDF)."""
    rows = build_rows(f)
    year = f['selected_year']

    if export:
        filter_lines = [
            ('View', 'Sold quantity'),
            ('Store', f['store'] or 'All'),
            ('Brand', ', '.join(f['brand_filter']) or 'All'),
            ('Salesman', ', '.join(f['salesman_filter']) or 'All'),
            ('Search', f['q'] or '-'),
        ]
        if export == 'xlsx':
            return xlsx_response(rows, year, filter_lines)
        if not f['brand_filter']:
            rows = [r for r in rows if r['sold'] or r['budget'] is not None]
            filter_lines.append(('Items listed', 'with sales or a budget (select a brand to list all its items)'))
        if len(rows) > PDF_ROW_LIMIT:
            return HttpResponse(
                f'{len(rows)} items are too many for one PDF (max {PDF_ROW_LIMIT}). '
                'Filter by brand first, or download the Excel file instead.',
                status=400, content_type='text/plain')
        return pdf_response(rows, year, filter_lines)

    paginator = Paginator(rows, page_size)
    page_obj = paginator.get_page(request.GET.get('page'))
    page_rows = list(page_obj.object_list)
    for it in page_rows:
        it['cells'] = [{'text': _qty_text(v) if v else '', 'neg': v < 0} for v in it['m']]
        it['total_text'] = _qty_text(it['total'])
        it['chart_json'] = json.dumps({
            'code': it['item_code'], 'desc': it['description'], 'year': year, 'kind': 'qty', 'pmode': 'latest',
            'm': [round(float(v), 2) for v in it['m']],
        })

    params = request.GET.copy()
    params.pop('page', None)
    params_noview = params.copy()
    params_noview.pop('view', None)
    params_noview.pop('export', None)

    return render(request, 'item_monthly_sales/page.html', {
        'years': f['years'], 'selected_year': year,
        'brands': list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
                       .values_list('item_firm', flat=True).distinct().order_by('item_firm')),
        'salesmen': f['salesmen'],
        'month_names': MONTH_NAMES_SHORT, 'view': 'qty', 'pmode': 'latest',
        'rows': page_rows, 'page_obj': page_obj, 'total_count': paginator.count,
        'querystring': params.urlencode(),
        'qs_noview': params_noview.urlencode(),
        'filters': {'q': f['q'], 'brand_filter': f['brand_filter'], 'salesman_filter': f['salesman_filter'],
                    'store': f['store'], 'page_size': page_size},
        'budget_label': BUDGET_LABEL,
        'can_edit_budget': can_edit_budget(request.user),
    })


def _item_exists(code):
    return (Items.objects.filter(item_code=code).exists()
            or SAPARInvoiceItem.objects.filter(item_code=code).exists()
            or SAPARCreditMemoItem.objects.filter(item_code=code).exists())


@login_required
@require_POST
def save_item_budget(request):
    """Save one item's proposed budget. An empty value removes it."""
    if not can_edit_budget(request.user):
        return JsonResponse({'ok': False, 'error': 'Only an admin can change the budget.'}, status=403)

    code = request.POST.get('item', '').strip()
    raw = request.POST.get('qty')
    if not code or len(code) > 100 or raw is None:
        return JsonResponse({'ok': False, 'error': 'Bad request.'}, status=400)
    raw = raw.strip()

    qty = None
    if raw:
        digits = raw.replace(',', '')
        if not _WHOLE_NUMBER.fullmatch(raw) or len(digits) > 12 or int(digits) > MAX_BUDGET:
            return JsonResponse({'ok': False, 'error': 'Enter a whole number (0 or more).'}, status=400)
        qty = int(digits)

    if qty is None:
        ItemBudget.objects.filter(item_code=code, year=BUDGET_YEAR).delete()
    else:
        if not _item_exists(code):
            return JsonResponse({'ok': False, 'error': 'Unknown item.'}, status=400)
        ItemBudget.objects.update_or_create(
            item_code=code, year=BUDGET_YEAR, defaults={'qty': qty, 'updated_by': request.user})
    return JsonResponse({'ok': True, 'qty': qty})


# ---------------------------------------------------------------- Excel / PDF

def xlsx_response(rows, year, filter_lines):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = 'Item Monthly Sales'
    head = ['Item Code', 'Description', 'Brand'] + MONTHS + ['Total Qty', BUDGET_LABEL]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='2C5530')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def put_text(cell, value):
        cell.value = str(value)
        cell.data_type = 's'   # always text: a value starting with = + - @ must never run as a formula

    def put_qty(cell, v):
        whole = v == int(v)
        cell.value = int(v) if whole else float(v)
        cell.number_format = '#,##0;[Red]-#,##0' if whole else '#,##0.00;[Red]-#,##0.00'

    # row numbers are counted here: ws.max_row is slow to ask for on every one of thousands of items
    for n, r in enumerate(rows, start=2):
        put_text(ws.cell(row=n, column=1), r['item_code'])
        put_text(ws.cell(row=n, column=2), r['description'])
        ws.cell(row=n, column=2).alignment = Alignment(wrap_text=True, vertical='top')
        put_text(ws.cell(row=n, column=3), r['brand'])
        for i, v in enumerate(r['m']):
            if v:
                put_qty(ws.cell(row=n, column=4 + i), v)
        if r['sold']:
            put_qty(ws.cell(row=n, column=16), r['total'])
            ws.cell(row=n, column=16).font = Font(bold=True)
        if r['budget'] is not None:
            put_qty(ws.cell(row=n, column=17), r['budget'])

    for letter, width in (('A', 14), ('B', 46), ('C', 22), ('P', 14), ('Q', 24)):
        ws.column_dimensions[letter].width = width
    for i in range(12):
        ws.column_dimensions[ws.cell(row=1, column=4 + i).column_letter].width = 11
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = ws.dimensions

    info = wb.create_sheet('Filter')
    info.append(['Report', 'Item Monthly Sales (sold quantity: invoices less returns)'])
    info.append(['Year', year])
    info.append(['Exported', timezone.localtime().strftime('%d %b %Y %H:%M')])
    for label, value in filter_lines:
        info.append([label, value])
    info.append(['Budget', f'{BUDGET_LABEL} is a quantity for the whole company (salesman / store filters do not change it)'])
    info.append(['Items', len(rows)])
    info.column_dimensions['A'].width = 14
    info.column_dimensions['B'].width = 60

    buf = BytesIO()
    wb.save(buf)
    return _download(buf, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', year, 'xlsx')


def pdf_response(rows, year, filter_lines):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()['Normal']
    cell = ParagraphStyle('mq', parent=base, fontSize=7, leading=8.6, wordWrap='CJK')
    num = ParagraphStyle('mqn', parent=cell, alignment=2, wordWrap=None, fontSize=6.6)
    brand_style = ParagraphStyle('mqb', parent=cell, wordWrap=None)
    hcell = ParagraphStyle('mqh', parent=cell, fontName='Helvetica-Bold', textColor=colors.white, fontSize=7.5)
    hcell_r = ParagraphStyle('mqhr', parent=hcell, alignment=2, wordWrap=None)
    title = ParagraphStyle('mqt', parent=base, fontName='Helvetica-Bold', fontSize=14, leading=18)

    def qty(v):
        if not v:
            return '-'
        return f'<font color="#b91c1c">{_qty_text(v)}</font>' if v < 0 else _qty_text(v)

    head = ['Item', 'Brand'] + MONTHS + ['Total Qty', f'Proposed<br/>Budget {BUDGET_YEAR}']
    data = [[Paragraph(h, hcell if i < 2 else hcell_r) for i, h in enumerate(head)]]
    for r in rows:
        item = f"<b>{escape(r['item_code'])}</b>"
        if r['description']:
            item += f"<br/>{escape(r['description'])}"
        line = [Paragraph(item, cell), Paragraph(escape(r['brand'] or '-'), brand_style)]
        line += [Paragraph(qty(v), num) for v in r['m']]
        line.append(Paragraph(f"<b>{_qty_text(r['total']) if r['sold'] else '-'}</b>", num))
        line.append(Paragraph('-' if r['budget'] is None else _qty_text(r['budget']), num))
        data.append(line)
    if not rows:
        data.append([Paragraph('No items found for the selected filters.', cell)] + [''] * (len(head) - 1))

    page = landscape(A4)
    usable = page[0] - 16 * mm
    month_w, brand_w, total_w, budget_w = 14 * mm, 22 * mm, 17 * mm, 20 * mm
    item_w = usable - (brand_w + 12 * month_w + total_w + budget_w)
    table = Table(data, colWidths=[item_w, brand_w] + [month_w] * 12 + [total_w, budget_w], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2C5530')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E1')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#64748B'))
        canvas.drawRightString(page[0] - 8 * mm, 5 * mm, f'Page {doc.page}')
        canvas.restoreState()

    sub = '   |   '.join([f'Year: {year}'] + [f'{k}: {v}' for k, v in filter_lines]
                         + [f'{len(rows)} items', timezone.localtime().strftime('%d %b %Y %H:%M')])
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm, bottomMargin=11 * mm,
                            title='Item Monthly Sales', author='Junaid')
    doc.build([Paragraph('Item Monthly Sales', title),
               Paragraph(f'Sold quantity (invoices less returns). {BUDGET_LABEL} is a company-wide quantity.', cell),
               Paragraph(escape(sub), cell), Spacer(1, 3 * mm), table], onFirstPage=footer, onLaterPages=footer)
    return _download(buf, 'application/pdf', year, 'pdf')


def _download(buf, content_type, year, ext):
    resp = HttpResponse(buf.getvalue(), content_type=content_type)
    stamp = timezone.localtime().strftime('%Y%m%d_%H%M')
    resp['Content-Disposition'] = f'attachment; filename="item_monthly_sold_qty_{year}_{stamp}.{ext}"'
    resp['Cache-Control'] = 'private, no-store'
    return resp
