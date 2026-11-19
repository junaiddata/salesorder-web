"""
Excel / PDF export for the Item Monthly Sales page.

Receives the rows the page itself builds (so numbers always match the screen) and writes the same
table: net sales amount per item for Jan-Dec plus the item total. Graphs are intentionally not included.
"""
import calendar
from io import BytesIO
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone

PDF_ROW_LIMIT = 3000   # a PDF with more rows is refused: use Excel or filter first

MONTHS = [calendar.month_abbr[m] for m in range(1, 13)]


def _stamp():
    return timezone.localtime().strftime('%Y%m%d_%H%M')


def xlsx_response(rows, year, filter_lines):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = 'Item Monthly Sales'
    head = ['Item Code', 'Description', 'Brand'] + MONTHS + ['Total']
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='2C5530')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def put_text(cell, value):
        cell.value = str(value)
        cell.data_type = 's'   # always text: a value starting with = + - @ must never run as a formula

    for r in rows:
        ws.append([None] * len(head))
        n = ws.max_row
        put_text(ws.cell(row=n, column=1), r['item_code'])
        put_text(ws.cell(row=n, column=2), r['description'])
        ws.cell(row=n, column=2).alignment = Alignment(wrap_text=True, vertical='top')
        put_text(ws.cell(row=n, column=3), r['brand'])
        for i, v in enumerate(r['m']):
            if v:
                cell = ws.cell(row=n, column=4 + i)
                cell.value, cell.number_format = float(v), '#,##0.00;[Red]-#,##0.00'
        tot = ws.cell(row=n, column=16)
        tot.value, tot.number_format = float(r['total']), '#,##0.00;[Red]-#,##0.00'
        tot.font = Font(bold=True)

    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 46
    ws.column_dimensions['C'].width = 22
    for i in range(12):
        ws.column_dimensions[ws.cell(row=1, column=4 + i).column_letter].width = 13
    ws.column_dimensions['P'].width = 16
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = ws.dimensions

    info = wb.create_sheet('Filter')
    info.append(['Report', 'Item Monthly Sales (net sales amount: invoices less credit memos)'])
    info.append(['Year', year])
    info.append(['Exported', timezone.localtime().strftime('%d %b %Y %H:%M')])
    for label, value in filter_lines:
        info.append([label, value])
    info.append(['Items', len(rows)])
    info.column_dimensions['A'].width = 14
    info.column_dimensions['B'].width = 60

    buf = BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="item_monthly_sales_{year}_{_stamp()}.xlsx"'
    resp['Cache-Control'] = 'private, no-store'
    return resp


def pdf_response(rows, year, filter_lines):
    """Returns an HttpResponse, or None when there are too many rows for one PDF."""
    if len(rows) > PDF_ROW_LIMIT:
        return None
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()['Normal']
    cell = ParagraphStyle('ms', parent=base, fontSize=7, leading=8.6, wordWrap='CJK')
    num = ParagraphStyle('msn', parent=cell, alignment=2, wordWrap=None, fontSize=6.6)   # amounts never split
    brand_style = ParagraphStyle('msb', parent=cell, wordWrap=None)   # wrap brand names at spaces, not mid-word
    hcell = ParagraphStyle('msh', parent=cell, fontName='Helvetica-Bold', textColor=colors.white, fontSize=7.5)
    hcell_r = ParagraphStyle('mshr', parent=hcell, alignment=2)
    title = ParagraphStyle('mst', parent=base, fontName='Helvetica-Bold', fontSize=14, leading=18)

    def money(v):
        if not v:
            return '-'
        s = f'{v:,.2f}'
        return f'<font color="#b91c1c">{s}</font>' if v < 0 else s

    head = ['Item', 'Brand'] + MONTHS + ['Total']
    data = [[Paragraph(h, hcell if i < 2 else hcell_r) for i, h in enumerate(head)]]
    for r in rows:
        item = f"<b>{escape(r['item_code'])}</b>"
        if r['description']:
            item += f"<br/>{escape(r['description'])}"
        line = [Paragraph(item, cell), Paragraph(escape(r['brand'] or '-'), brand_style)]
        line += [Paragraph(money(v), num) for v in r['m']]
        line.append(Paragraph(f"<b>{money(r['total'])}</b>", num))
        data.append(line)
    if not rows:
        data.append([Paragraph('No sales found for the selected filters.', cell)] + [''] * (len(head) - 1))

    page = landscape(A4)
    usable = page[0] - 16 * mm
    month_w, brand_w, total_w = 16.5 * mm, 22 * mm, 20 * mm
    item_w = usable - (brand_w + 12 * month_w + total_w)
    table = Table(data, colWidths=[item_w, brand_w] + [month_w] * 12 + [total_w], repeatRows=1)
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
               Paragraph('Net sales amount (invoices less credit memos)', cell), Paragraph(escape(sub), cell),
               Spacer(1, 3 * mm), table], onFirstPage=footer, onLaterPages=footer)
    resp = HttpResponse(buf.getvalue(), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="item_monthly_sales_{year}_{_stamp()}.pdf"'
    resp['Cache-Control'] = 'private, no-store'
    return resp
