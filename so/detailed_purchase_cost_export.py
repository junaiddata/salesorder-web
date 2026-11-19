"""
Excel / PDF export for the Monthly Purchase Cost page.

Receives the rows the page itself builds (so numbers always match the screen) and writes the same
table: last price of each month, Current Cost and the % change. Graphs are intentionally not included.
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


def _num(v):
    return None if v is None else float(v)


def _change_text(pc):
    """'Jan 2.00 -> Nov 3.07' for the bracket part."""
    if not pc:
        return ''
    return f"{pc['from_month']} {pc['from_price']:,.2f} → {pc['to_month']} {pc['to_price']:,.2f}"


def _pct_text(pc):
    if not pc:
        return '-'
    pct = float(pc['pct'])
    return f"{'+' if pct > 0 else ''}{pct:.1f}%"


def xlsx_response(rows, year, filter_lines):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = 'Monthly Purchase Cost'
    head = ['Item Code', 'Description', 'Brand'] + MONTHS + ['Current Cost', 'Change in Price %', 'Change (from -> to)']
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
        for i, c in enumerate(r['months']):
            if c is not None:
                cell = ws.cell(row=n, column=4 + i)
                cell.value, cell.number_format = _num(c['last']), '#,##0.00'
        cc = ws.cell(row=n, column=16)
        if r['current_cost'] is not None:
            cc.value, cc.number_format = _num(r['current_cost']), '#,##0.00'
        pc = r['price_change']
        if pc:
            pcell = ws.cell(row=n, column=17)
            pcell.value = round(float(pc['pct']), 1)
            pcell.number_format = '+0.0"%";-0.0"%";0.0"%"'
            put_text(ws.cell(row=n, column=18), _change_text(pc))
            # purchase price: up is red, down is green
            if pc['pct'] > 0:
                pcell.font = Font(bold=True, color='B91C1C')
            elif pc['pct'] < 0:
                pcell.font = Font(bold=True, color='15803D')

    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 46
    ws.column_dimensions['C'].width = 22
    for i in range(12):
        ws.column_dimensions[ws.cell(row=1, column=4 + i).column_letter].width = 9
    ws.column_dimensions['P'].width = 13
    ws.column_dimensions['Q'].width = 14
    ws.column_dimensions['R'].width = 26
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = ws.dimensions

    info = wb.create_sheet('Filter')
    info.append(['Report', 'Monthly Purchase Cost (last PO price of each month)'])
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
    resp['Content-Disposition'] = f'attachment; filename="monthly_purchase_cost_{year}_{_stamp()}.xlsx"'
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
    cell = ParagraphStyle('mc', parent=base, fontSize=7, leading=8.6, wordWrap='CJK')
    cell_r = ParagraphStyle('mcr', parent=cell, alignment=2)
    brand_style = ParagraphStyle('mcb', parent=cell, wordWrap=None)   # wrap brand names at spaces, not mid-word
    hcell = ParagraphStyle('mh', parent=cell, fontName='Helvetica-Bold', textColor=colors.white, fontSize=7.5)
    hcell_r = ParagraphStyle('mhr', parent=hcell, alignment=2)
    title = ParagraphStyle('mt', parent=base, fontName='Helvetica-Bold', fontSize=14, leading=18)

    head = ['Item', 'Brand'] + MONTHS + ['Current Cost', 'Change in Price % (from Jan to current date)']
    data = [[Paragraph(h, hcell if i < 2 else hcell_r) for i, h in enumerate(head)]]
    for r in rows:
        item = f"<b>{escape(r['item_code'])}</b>"
        if r['description']:
            item += f"<br/>{escape(r['description'])}"
        line = [Paragraph(item, cell), Paragraph(escape(r['brand'] or '-'), brand_style)]
        for c in r['months']:
            line.append(Paragraph('-' if c is None else f"{c['last']:,.2f}", cell_r))
        line.append(Paragraph('-' if r['current_cost'] is None else f"{r['current_cost']:,.2f}", cell_r))
        pc = r['price_change']
        if pc:
            colour = '#b91c1c' if pc['pct'] > 0 else '#15803d' if pc['pct'] < 0 else '#1e293b'
            line.append(Paragraph(
                # the PDF's standard font has no arrow glyph, so spell it "to"
                f'<font color="{colour}"><b>{escape(_pct_text(pc))}</b></font><br/>'
                f'({escape(_change_text(pc).replace(chr(0x2192), "to"))})', cell_r))
        else:
            line.append(Paragraph('-', cell_r))
        data.append(line)
    if not rows:
        data.append([Paragraph('No purchases found for the selected filters.', cell)] + [''] * (len(head) - 1))

    page = landscape(A4)
    usable = page[0] - 16 * mm
    month_w, brand_w, cost_w, chg_w = 11.5 * mm, 27 * mm, 15 * mm, 34 * mm
    item_w = usable - (brand_w + 12 * month_w + cost_w + chg_w)
    table = Table(data, colWidths=[item_w, brand_w] + [month_w] * 12 + [cost_w, chg_w], repeatRows=1)
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
                            title='Monthly Purchase Cost', author='Junaid')
    doc.build([Paragraph('Monthly Purchase Cost', title),
               Paragraph('Last PO price of each month', cell), Paragraph(escape(sub), cell),
               Spacer(1, 3 * mm), table], onFirstPage=footer, onLaterPages=footer)
    resp = HttpResponse(buf.getvalue(), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="monthly_purchase_cost_{year}_{_stamp()}.pdf"'
    resp['Cache-Control'] = 'private, no-store'
    return resp
