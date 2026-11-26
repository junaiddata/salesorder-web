"""
PDF export for the combined quotations pages (Junaid /quotations/combined/ and Alabama).

Draws the same rows as the Excel exports (each page builds them once for both), so filters, user scope and
numbers always match. No GP or cost is included.
"""
import re
from io import BytesIO
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone

SUMMARY_ROW_LIMIT = 5000
ITEMS_ROW_LIMIT = 3000   # more lines make hundreds of pages and a very slow request
_TEXT_LIMIT = 300        # a single cell can never be taller than a page

# (row key, heading, width in mm, 'text' | 'num' | 'qty'); widths add up to the 281 mm of landscape A4
_SUMMARY_COLUMNS = [
    ('Date', 'Date', 19, 'text'), ('Source', 'Source', 13, 'text'), ('Quotation Number', 'Quotation', 30, 'text'),
    ('Customer Code', 'Customer code', 24, 'text'), ('Customer Name', 'Customer name', 100, 'text'),
    ('Total Value', 'Total', 27, 'num'), ('Status', 'Status', 24, 'text'), ('Salesman', 'Salesman', 44, 'text'),
]
_ITEM_COLUMNS = [
    ('Date', 'Date', 18, 'text'), ('Source', 'Source', 12, 'text'), ('Quotation Number', 'Quotation', 24, 'text'),
    ('Customer Name', 'Customer', 50, 'text'), ('Salesman', 'Salesman', 31, 'text'), ('Item No', 'Item no', 27, 'text'),
    ('Item Name', 'Item name', 66, 'text'), ('Quantity', 'Qty', 15, 'qty'), ('Price', 'Price', 18, 'num'),
    ('Total Value', 'Total', 20, 'num'),
]

_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_MARKS = str.maketrans({'″': '"', '′': "'"})   # inch / foot marks the standard PDF font lacks


def _clean(value):
    """Safe paragraph text: no control characters, only glyphs the standard font has, markup escaped."""
    text = _CONTROL.sub('', str(value or '')).translate(_MARKS)
    text = text.encode('cp1252', 'replace').decode('cp1252')
    if len(text) > _TEXT_LIMIT:
        text = text[:_TEXT_LIMIT] + '...'
    return escape(text)


def _number(value, kind):
    s = f'{float(value or 0):,.2f}'
    return s[:-3] if kind == 'qty' and s.endswith('.00') else s


def filter_lines(request, keys):
    """The applied filters, for the PDF heading. ``keys`` are the GET filters this page supports."""
    labels = {'q': 'Search', 'start_date': 'From', 'end_date': 'To', 'source': 'Source', 'customer': 'Customer',
              'salesman_filter': 'Salesman', 'division': 'Division', 'status': 'Status', 'total': 'Total amount'}
    is_salesman = getattr(getattr(request.user, 'role', None), 'role', None) == 'Salesman'
    out = []
    for key in keys:
        if key == 'salesman_filter' and is_salesman:
            continue   # ignored for salesmen: they always get their own quotations
        values = [v.strip() for v in request.GET.getlist(key) if v.strip() and v.strip().lower() != 'all']
        if values:
            out.append((labels[key], ', '.join(values)))
    return out


def _too_many(count, limit, what):
    return HttpResponse(
        f'<p>{count:,} {what} are too many for one PDF (maximum {limit:,}). '
        'Narrow the filters (for example the dates or the customer), or use the Excel export.</p>'
        '<p><a href="javascript:history.back()">Go back</a></p>',
        status=400)


def pdf_response(kind, rows, title, filters, filename_prefix, header_color):
    """kind: 'summary' (one row per quotation) or 'items' (one row per line)."""
    summary = kind == 'summary'
    limit = SUMMARY_ROW_LIMIT if summary else ITEMS_ROW_LIMIT
    if len(rows) > limit:
        return _too_many(len(rows), limit, 'quotations' if summary else 'quotation lines')

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    base = getSampleStyleSheet()['Normal']
    cell = ParagraphStyle('cq', parent=base, fontSize=7, leading=8.6, wordWrap='CJK')
    words = ParagraphStyle('cqw', parent=cell, wordWrap=None)       # names wrap at spaces, not inside a word
    num = ParagraphStyle('cqn', parent=cell, alignment=2, wordWrap=None)   # the bold total
    head = ParagraphStyle('cqh', parent=words, fontName='Helvetica-Bold', textColor=colors.white, fontSize=7.5)
    head_num = ParagraphStyle('cqhn', parent=head, alignment=2)
    heading = ParagraphStyle('cqt', parent=base, fontName='Helvetica-Bold', fontSize=14, leading=18)

    columns = _SUMMARY_COLUMNS if summary else _ITEM_COLUMNS
    data = [[Paragraph(label, head if kind_ == 'text' else head_num) for _, label, _, kind_ in columns]]
    total = 0.0
    for r in rows:
        total += float(r.get('Total Value') or 0)
        line = []
        for key, _, _, kind_ in columns:
            # short fixed cells are plain strings: far faster than a Paragraph on thousands of rows
            if key in ('Date', 'Source'):
                line.append(str(r.get(key) or ''))
            elif kind_ == 'text':
                style = words if key in ('Customer Name', 'Item Name', 'Salesman') else cell
                line.append(Paragraph(_clean(r.get(key)), style))
            else:
                line.append(_number(r.get(key), kind_))
        data.append(line)

    total_col = [c[0] for c in columns].index('Total Value')
    if rows:
        total_row = [''] * len(columns)
        total_row[0] = Paragraph('<b>Total of the rows listed</b>', cell)
        total_row[total_col] = Paragraph(f'<b>{_number(total, "num")}</b>', num)
        data.append(total_row)
    else:
        data.append([Paragraph('No quotations match the filters.', cell)] + [''] * (len(columns) - 1))

    style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(header_color)),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E1')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('SPAN', (0, -1), ((total_col - 1) if rows else -1, -1)),
        # the plain-string cells, matched to the paragraph cells
        ('FONTSIZE', (0, 1), (-1, -1), 7), ('LEADING', (0, 1), (-1, -1), 8.6),
    ]
    style += [('ALIGN', (i, 1), (i, -1), 'RIGHT') for i, c in enumerate(columns) if c[3] != 'text']
    table = LongTable(data, colWidths=[c[2] * mm for c in columns], repeatRows=1)
    table.setStyle(TableStyle(style))

    page = landscape(A4)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#64748B'))
        canvas.drawRightString(page[0] - 8 * mm, 5 * mm, f'Page {doc.page}')
        canvas.restoreState()

    what = 'summary' if summary else 'line items'
    info = [f'{k}: {v}' for k, v in filters] or ['No filters']
    info += [f'{len(rows):,} {"quotations" if summary else "lines"}',
             'Exported ' + timezone.localtime().strftime('%d %b %Y %H:%M')]
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm,
                            bottomMargin=11 * mm, title=f'{title} - {what}')
    doc.build([Paragraph(f'{title} - {what}', heading),
               Paragraph('SAP quotations use the posting date, app quotations the quotation date.', cell),
               Paragraph(_clean('   |   '.join(info)), cell), Spacer(1, 3 * mm), table],
              onFirstPage=footer, onLaterPages=footer)

    stamp = timezone.localtime().strftime('%Y%m%d_%H%M')
    suffix = '' if summary else '_lines'
    response = HttpResponse(buf.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename_prefix}{suffix}_{stamp}.pdf"'
    response['Cache-Control'] = 'private, no-store'
    return response
