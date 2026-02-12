"""
Finance Statement PDF Export - Customer Finance Summary
Refactored for enterprise-grade visual quality using ReportLab only.
Logo source: https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp
"""
import os
from io import BytesIO
from decimal import Decimal
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Value, FloatField, Max
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
    PageBreak, KeepTogether,
)

from so.models import Customer, Salesman, FinanceCreditEditLog


# ─────────────────────────────────────────────────────────────────────────────
# DESIGN CONSTANTS — single source of truth for all visual parameters
# ─────────────────────────────────────────────────────────────────────────────

# Brand palette
CLR_PRIMARY = HexColor('#1B2A4A')       # Deep navy — headers, titles
CLR_PRIMARY_LIGHT = HexColor('#2C4A7C') # Lighter navy — subtle accents
CLR_ACCENT = HexColor('#D4912A')        # Warm gold — section markers
CLR_BG_HEADER = HexColor('#1B2A4A')     # Table header background
CLR_BG_TOTAL = HexColor('#EBF0F7')      # Totals row background
CLR_BG_ZEBRA = HexColor('#F8F9FB')      # Alternating row tint
CLR_BG_SECTION = HexColor('#F3F4F6')    # Section header background
CLR_BORDER = HexColor('#D1D5DB')        # Table grid — subtle gray
CLR_BORDER_HEAVY = HexColor('#9CA3AF')  # Outer box — slightly darker
CLR_TEXT = HexColor('#1F2937')          # Body text — near-black
CLR_TEXT_MUTED = HexColor('#6B7280')    # Labels, secondary text
CLR_DANGER = HexColor('#DC2626')        # Over-limit warning
CLR_WHITE = colors.white

# Typography sizes
FONT_TITLE = 13
FONT_SUBTITLE = 8
FONT_SECTION = 8.5
FONT_BODY = 7.5
FONT_BODY_SM = 7
FONT_FOOTER = 6.5
FONT_KPI = 9

# Spacing constants (in points)
SP_SECTION = 10        # Space before a new section
SP_AFTER_HEADER = 4    # Space after section header bar
SP_ROW_PAD_V = 3.5     # Vertical cell padding — data rows
SP_ROW_PAD_H = 5       # Horizontal cell padding
SP_HEADER_PAD_V = 5    # Vertical cell padding — header rows


# ─────────────────────────────────────────────────────────────────────────────
# SHARED STYLES — built once, reused everywhere
# ─────────────────────────────────────────────────────────────────────────────

def _build_styles():
    """Return a dict of ParagraphStyles for consistent typography."""
    base = getSampleStyleSheet()['Normal']
    return {
        'title': ParagraphStyle(
            'PDFTitle', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TITLE,
            textColor=CLR_PRIMARY, leading=FONT_TITLE + 3,
        ),
        'subtitle': ParagraphStyle(
            'PDFSubtitle', parent=base,
            fontName='Helvetica', fontSize=FONT_SUBTITLE,
            textColor=CLR_TEXT_MUTED, leading=FONT_SUBTITLE + 3,
        ),
        'section': ParagraphStyle(
            'PDFSection', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_SECTION,
            textColor=CLR_PRIMARY, leading=FONT_SECTION + 3,
        ),
        'label': ParagraphStyle(
            'PDFLabel', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY_SM,
            textColor=CLR_TEXT_MUTED, leading=FONT_BODY_SM + 3,
        ),
        'cell': ParagraphStyle(
            'PDFCell', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
        ),
        'cell_r': ParagraphStyle(
            'PDFCellRight', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'cell_bold': ParagraphStyle(
            'PDFCellBold', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
        ),
        'cell_bold_r': ParagraphStyle(
            'PDFCellBoldR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'header_cell': ParagraphStyle(
            'PDFHeaderCell', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_SECTION,
            textColor=CLR_WHITE, leading=FONT_SECTION + 3,
        ),
        'header_cell_r': ParagraphStyle(
            'PDFHeaderCellR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_SECTION,
            textColor=CLR_WHITE, leading=FONT_SECTION + 3,
            alignment=TA_RIGHT,
        ),
        'kpi_value': ParagraphStyle(
            'PDFKpiValue', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_KPI,
            textColor=CLR_PRIMARY, leading=FONT_KPI + 3,
        ),
        'kpi_label': ParagraphStyle(
            'PDFKpiLabel', parent=base,
            fontName='Helvetica', fontSize=FONT_FOOTER,
            textColor=CLR_TEXT_MUTED, leading=FONT_FOOTER + 2,
        ),
        'footer': ParagraphStyle(
            'PDFFooter', parent=base,
            fontName='Helvetica', fontSize=FONT_FOOTER,
            textColor=CLR_TEXT_MUTED, leading=FONT_FOOTER + 2,
            alignment=TA_CENTER,
        ),
        'danger_bold': ParagraphStyle(
            'PDFDangerBold', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_DANGER, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'danger_bold_r': ParagraphStyle(
            'PDFDangerBoldR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_DANGER, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'danger_bold_lg': ParagraphStyle(
            'PDFDangerBoldLg', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_KPI,
            textColor=CLR_DANGER, leading=FONT_KPI + 3,
            alignment=TA_RIGHT,
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HELPER UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _get_logo():
    """Load company logo from media directory. Returns Image or None."""
    for name in ['footer-logo.png', 'footer-logo1.png']:
        path = os.path.join(settings.BASE_DIR, 'media', name)
        if os.path.exists(path):
            try:
                return Image(path, width=1.6 * inch, height=0.6 * inch)
            except Exception:
                pass
    return None


def _fmt(num):
    """Format number: integers without decimals, floats with 2 decimals."""
    if num is None:
        return "0"
    try:
        v = float(num)
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    except (TypeError, ValueError):
        return "0"


def _build_document_header(styles, title_text, subtitle_text, page_width):
    """
    Build the top-of-page header: logo on left, title block on right,
    separated by a thin accent line.
    """
    logo_img = _get_logo()

    title_block = Paragraph(
        f"<b>{title_text}</b>", styles['title']
    )
    subtitle_block = Paragraph(subtitle_text, styles['subtitle'])
    date_block = Paragraph(
        f"Generated: {datetime.now().strftime('%d %b %Y, %H:%M')}",
        styles['label'],
    )

    # Right-side stack: title, subtitle, date
    right_content = Table(
        [[title_block], [subtitle_block], [date_block]],
        colWidths=[page_width - 2.2 * inch],
    )
    right_content.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))

    if logo_img:
        row = [[logo_img, right_content]]
        widths = [2.0 * inch, page_width - 2.0 * inch]
    else:
        # Fallback: text-only brand
        brand = Paragraph(
            "<b>JUNAID</b>", ParagraphStyle(
                'Brand', fontName='Helvetica-Bold',
                fontSize=16, textColor=CLR_PRIMARY,
            )
        )
        row = [[brand, right_content]]
        widths = [2.0 * inch, page_width - 2.0 * inch]

    header_table = Table(row, colWidths=widths)
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))

    # Accent line under header
    line_data = [['']]
    line_table = Table(line_data, colWidths=[page_width])
    line_table.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, CLR_ACCENT),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))

    return [header_table, Spacer(1, 4), line_table, Spacer(1, SP_SECTION)]


def _build_kpi_bar(kpi_items, styles, page_width):
    """
    Build a horizontal KPI summary bar.
    kpi_items: list of (label, value_str) tuples.
    Returns a Table with card-like KPI cells.
    """
    num_items = len(kpi_items)
    cell_width = page_width / num_items

    # Each KPI is a mini-table: value on top, label below
    kpi_cells = []
    for label, value in kpi_items:
        mini = Table(
            [
                [Paragraph(value, styles['kpi_value'])],
                [Paragraph(label, styles['kpi_label'])],
            ],
            colWidths=[cell_width - 8],
        )
        mini.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        kpi_cells.append(mini)

    bar = Table([kpi_cells], colWidths=[cell_width] * num_items)
    bar.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CLR_BG_SECTION),
        ('BOX', (0, 0), (-1, -1), 0.5, CLR_BORDER),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, CLR_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    return bar


def _build_section_header(text, styles, width):
    """
    Build a section header bar: gold accent strip + bold label on gray background.
    """
    t = Table(
        [['', Paragraph(f"<b>{text}</b>", styles['section'])]],
        colWidths=[3, width - 3],
    )
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), CLR_ACCENT),
        ('BACKGROUND', (1, 0), (1, 0), CLR_BG_SECTION),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (1, 0), (1, 0), 8),
    ]))
    return t


def _standard_data_table_style(num_rows, has_total_row=True):
    """
    Build a clean, professional TableStyle for data grids.
    Applies: header styling, zebra striping, border treatment, total row emphasis.
    num_rows: total rows including header and optional total row.
    """
    cmds = [
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), CLR_BG_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, 0), CLR_WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), FONT_BODY),

        # Global grid: light inner lines, slightly heavier outer box
        ('BOX', (0, 0), (-1, -1), 0.75, CLR_BORDER_HEAVY),
        ('LINEBELOW', (0, 0), (-1, 0), 0.75, CLR_BORDER_HEAVY),

        # Cell padding
        ('TOPPADDING', (0, 0), (-1, -1), SP_ROW_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, -1), SP_ROW_PAD_V),
        ('LEFTPADDING', (0, 0), (-1, -1), SP_ROW_PAD_H),
        ('RIGHTPADDING', (0, 0), (-1, -1), SP_ROW_PAD_H),
        ('TOPPADDING', (0, 0), (-1, 0), SP_HEADER_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, 0), SP_HEADER_PAD_V),

        # Vertical alignment
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]

    # Zebra striping on data rows (skip header row 0, skip total row if present)
    last_data_row = (num_rows - 2) if has_total_row else (num_rows - 1)
    for i in range(1, last_data_row + 1):
        if i % 2 == 0:
            cmds.append(('BACKGROUND', (0, i), (-1, i), CLR_BG_ZEBRA))

    # Subtle horizontal lines between data rows (instead of full grid)
    for i in range(1, num_rows - 1):
        cmds.append(('LINEBELOW', (0, i), (-1, i), 0.25, CLR_BORDER))

    # Total row emphasis
    if has_total_row:
        cmds.extend([
            ('BACKGROUND', (0, -1), (-1, -1), CLR_BG_TOTAL),
            ('LINEABOVE', (0, -1), (-1, -1), 1.2, CLR_PRIMARY),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ])

    return TableStyle(cmds)


def _build_page_footer(canvas, doc, styles_dict=None):
    """
    Draw a consistent footer on every page: thin line + page number + timestamp.
    Used as an onPage callback.
    """
    canvas.saveState()
    page_w, page_h = doc.pagesize

    # Thin line above footer
    y = 18
    canvas.setStrokeColor(CLR_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, y, page_w - doc.rightMargin, y)

    # Page number centered
    canvas.setFont('Helvetica', 6)
    canvas.setFillColor(CLR_TEXT_MUTED)
    canvas.drawCentredString(
        page_w / 2, 8,
        f"Page {doc.page}  •  Generated {datetime.now().strftime('%d %b %Y %H:%M')}  •  Confidential"
    )
    canvas.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# VIEW 1: FINANCE STATEMENT LIST (landscape, multi-customer)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def export_finance_statement_list_pdf(request):
    """
    Export Finance Statement List to PDF - Compact layout, more data per page.
    Respects same filters as list view (q, salesman, store).
    """
    # ── Get filter parameters (same as list view) ──
    search_query = request.GET.get('q', '').strip()
    salesman_filter = request.GET.get('salesman', '').strip()
    store_filter = request.GET.get('store', '').strip()

    customers = Customer.objects.filter(
        Q(total_outstanding__gt=0) | Q(pdc_received__gt=0)
    ).select_related('salesman')

    if search_query:
        customers = customers.filter(
            Q(customer_code__icontains=search_query) |
            Q(customer_name__icontains=search_query)
        )
    if salesman_filter:
        customers = customers.filter(salesman__id=salesman_filter)
    if store_filter == 'HO':
        customers = customers.filter(customer_code__startswith='HO')
    elif store_filter == 'Others':
        customers = customers.exclude(customer_code__startswith='HO')

    customers = customers.order_by('-total_outstanding_with_pdc', 'customer_name')

    totals = customers.aggregate(
        total_outstanding=Coalesce(Sum('total_outstanding'), Value(0.0, output_field=FloatField())),
        total_pdc=Coalesce(Sum('pdc_received'), Value(0.0, output_field=FloatField())),
        total_with_pdc=Coalesce(Sum('total_outstanding_with_pdc'), Value(0.0, output_field=FloatField())),
    )

    # ── Build PDF ──
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="finance_statement_list_'
        f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    margin_h, margin_v = 24, 24

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 6,  # Extra room for footer
    )

    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    # ── 1. Document Header ──
    elements.extend(_build_document_header(
        styles,
        title_text='FINANCE STATEMENT',
        subtitle_text='Customer Finance Summary & Outstanding Balances',
        page_width=usable_width,
    ))

    # ── 2. KPI Summary Bar ──
    kpi_items = [
        ('Total Outstanding', _fmt(totals['total_outstanding']) + ' AED'),
        ('PDC Received in Hand', _fmt(totals['total_pdc']) + ' AED'),
        ('Net Balance', _fmt(totals['total_with_pdc']) + ' AED'),
        ('Customers', str(customers.count())),
    ]
    elements.append(_build_kpi_bar(kpi_items, styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    # ── 3. Active Filters note (if any) ──
    active_filters = []
    if search_query:
        active_filters.append(f'Search: "{search_query}"')
    if salesman_filter:
        active_filters.append(f'Salesman ID: {salesman_filter}')
    if store_filter:
        active_filters.append(f'Store: {store_filter}')
    if active_filters:
        filter_text = '  •  '.join(active_filters)
        elements.append(Paragraph(
            f'<font color="#6B7280">Filters applied: {filter_text}</font>',
            styles['label'],
        ))
        elements.append(Spacer(1, 4))

    # ── 4. Data Table ──
    # Column widths tuned for landscape A4 - Customer Name wider for full names
    col_widths = [
        0.38 * inch,    # #
        0.70 * inch,    # Code
        3.40 * inch,    # Customer Name (lengthened for full names)
        0.95 * inch,    # Salesman
        1.00 * inch,    # Balance
        0.85 * inch,    # PDC
        1.00 * inch,    # Total
        0.85 * inch,    # Limit
        0.50 * inch,    # Terms (right-aligned)
    ]
    # Give remainder to Customer Name column for full names
    allocated = sum(col_widths)
    remainder = max(0, usable_width - allocated)
    col_widths[2] += remainder  # Customer Name

    # Header row - use white text styles for visibility on dark navy background
    hdr = [
        Paragraph('#', styles['header_cell']),
        Paragraph('Code', styles['header_cell']),
        Paragraph('Customer Name', styles['header_cell']),
        Paragraph('Salesman', styles['header_cell']),
        Paragraph('Balance (AED)', styles['header_cell_r']),
        Paragraph('PDC in Hand (AED)', styles['header_cell_r']),
        Paragraph('Total (AED)', styles['header_cell_r']),
        Paragraph('Limit (AED)', styles['header_cell_r']),
        Paragraph('Terms', styles['header_cell_r']),
    ]
    table_data = [hdr]

    # Data rows
    for idx, c in enumerate(customers, start=1):
        salesman_name = c.salesman.salesman_name if c.salesman else '—'
        over_limit = (
            (c.total_outstanding_with_pdc or 0) > (c.credit_limit or 0)
            and (c.credit_limit or 0) > 0
        )
        total_val_style = styles['danger_bold_r'] if over_limit else styles['cell_bold_r']

        table_data.append([
            Paragraph(str(idx), styles['cell']),
            Paragraph(c.customer_code or '—', styles['cell']),
            Paragraph((c.customer_name or '—'), styles['cell']),
            Paragraph(str(salesman_name)[:22], styles['cell']),
            Paragraph(_fmt(c.total_outstanding), styles['cell_r']),
            Paragraph(_fmt(c.pdc_received), styles['cell_r']),
            Paragraph(_fmt(c.total_outstanding_with_pdc), total_val_style),
            Paragraph(_fmt(c.credit_limit), styles['cell_r']),
            Paragraph(str(c.credit_days or '—'), styles['cell_r']),
        ])

    # Totals row
    table_data.append([
        Paragraph('', styles['cell']),
        Paragraph('<b>TOTAL</b>', styles['cell_bold']),
        Paragraph(f'<i>{customers.count()} customers</i>', styles['label']),
        Paragraph('', styles['cell']),
        Paragraph(_fmt(totals['total_outstanding']), styles['cell_bold_r']),
        Paragraph(_fmt(totals['total_pdc']), styles['cell_bold_r']),
        Paragraph(_fmt(totals['total_with_pdc']), styles['cell_bold_r']),
        Paragraph('', styles['cell']),
        Paragraph('', styles['cell']),
    ])

    data_table = Table(table_data, colWidths=col_widths, repeatRows=1)

    # Apply base style
    ts = _standard_data_table_style(len(table_data), has_total_row=True)

    # Right-align numeric columns (4–7) and Terms (8)
    ts.add('ALIGN', (4, 0), (8, -1), 'RIGHT')

    data_table.setStyle(ts)
    elements.append(data_table)

    # ── Build and return ──
    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response


# ─────────────────────────────────────────────────────────────────────────────
# VIEW 2: FINANCE STATEMENT DETAIL (portrait, single customer)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def export_finance_statement_detail_pdf(request, customer_id):
    """
    Export Finance Statement Detail (single customer) to PDF.
    Structure: logo, title, customer info, tables, summary.
    """
    customer = get_object_or_404(
        Customer.objects.select_related('salesman'), id=customer_id
    )

    # ── Compute data (business logic unchanged) ──
    today = datetime.now().date()
    month_names = [
        'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
    ]
    monthly_data = []
    month_amounts = [
        customer.month_pending_1, customer.month_pending_2,
        customer.month_pending_3, customer.month_pending_4,
        customer.month_pending_5, customer.month_pending_6,
    ]
    for i in range(6):
        months_ago = 5 - i
        month_date = today - timedelta(days=30 * months_ago)
        monthly_data.append({
            'month': f"{month_names[month_date.month - 1]} {month_date.year}",
            'amount': month_amounts[i] or 0,
        })

    total_monthly = sum(m['amount'] for m in monthly_data)
    total_outstanding = customer.total_outstanding or 0
    pdc_received = customer.pdc_received or 0
    total_with_pdc = customer.total_outstanding_with_pdc or 0
    old_months = customer.old_months_pending or 0
    very_old_months = getattr(customer, 'very_old_months_pending', 0) or 0
    credit_limit = customer.credit_limit or 0
    has_over_limit = total_with_pdc > credit_limit and credit_limit > 0

    # ── Build PDF ──
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="finance_statement_'
        f'{customer.customer_code}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = A4
    margin_h, margin_v = 32, 30

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 6,
    )

    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    # ── 1. Document Header ──
    elements.extend(_build_document_header(
        styles,
        title_text='FINANCE STATEMENT',
        subtitle_text='Customer Finance Details',
        page_width=usable_width,
    ))

    # ── 2. Customer Information Section ──
    elements.append(_build_section_header('Customer Information', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    # Two-column info grid
    info_col_widths = [1.3 * inch, 2.0 * inch, 1.3 * inch, 2.0 * inch]
    # Adjust to fill usable width
    info_allocated = sum(info_col_widths)
    if usable_width > info_allocated:
        extra = (usable_width - info_allocated) / 2
        info_col_widths[1] += extra
        info_col_widths[3] += extra

    info_data = [
        [
            Paragraph('Customer Code', styles['label']),
            Paragraph(customer.customer_code or '—', styles['cell_bold']),
            Paragraph('Customer Name', styles['label']),
            Paragraph((customer.customer_name or '—')[:40], styles['cell_bold']),
        ],
        [
            Paragraph('Salesman', styles['label']),
            Paragraph(
                (customer.salesman.salesman_name if customer.salesman else '—')[:28],
                styles['cell'],
            ),
            Paragraph('Credit Limit', styles['label']),
            Paragraph(_fmt(credit_limit) + ' AED', styles['cell_bold']),
        ],
        [
            Paragraph('Payment Terms', styles['label']),
            Paragraph(str(customer.credit_days or '—') + ' days', styles['cell']),
            Paragraph('Status', styles['label']),
            Paragraph(
                '<font color="#D97706"><b>Limit not set</b></font>' if credit_limit <= 0
                else (
                    '<font color="#DC2626"><b>OVER LIMIT</b></font>' if has_over_limit
                    else '<font color="#059669"><b>Within Limit</b></font>'
                ),
                styles['cell'],
            ),
        ],
    ]
    info_table = Table(info_data, colWidths=info_col_widths)
    info_table.setStyle(TableStyle([
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -2), 0.25, CLR_BORDER),  # Subtle row separators
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, SP_SECTION))

    # ── 3. Outstanding Summary KPI Bar ──
    elements.append(_build_section_header('Outstanding Summary', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    outstanding_kpis = [
        ('Balance Due', _fmt(total_outstanding) + ' AED'),
        ('PDC Received in Hand', _fmt(pdc_received) + ' AED'),
        ('Net Outstanding', _fmt(total_with_pdc) + ' AED'),
    ]
    elements.append(_build_kpi_bar(outstanding_kpis, styles, usable_width * 0.75))
    elements.append(Spacer(1, SP_SECTION))

    # ── 4. Monthly Pending Table ──
    elements.append(_build_section_header('Monthly Pending (Last 6 Months)', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    month_col_widths = [2.2 * inch, 1.6 * inch]
    month_rows = [
        [
            Paragraph('Month', styles['header_cell']),
            Paragraph('Amount (AED)', styles['header_cell_r']),
        ]
    ]
    for m in monthly_data:
        amt = m['amount']
        # Highlight non-zero amounts
        amt_style = styles['cell_bold_r'] if amt > 0 else styles['cell_r']
        month_rows.append([
            Paragraph(m['month'], styles['cell']),
            Paragraph(_fmt(amt), amt_style),
        ])
    month_rows.append([
        Paragraph('<b>Subtotal (6 months)</b>', styles['cell_bold']),
        Paragraph(_fmt(total_monthly), styles['cell_bold_r']),
    ])

    month_table = Table(month_rows, colWidths=month_col_widths, repeatRows=1)
    ts_month = _standard_data_table_style(len(month_rows), has_total_row=True)
    ts_month.add('ALIGN', (1, 0), (1, -1), 'RIGHT')
    month_table.setStyle(ts_month)
    elements.append(month_table)
    elements.append(Spacer(1, SP_SECTION))

    # ── 5. Aged Pending Section ──
    elements.append(_build_section_header('Aged Pending', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    aged_col_widths = [2.2 * inch, 1.6 * inch]
    aged_rows = [
        [
            Paragraph('Aging Bucket', styles['header_cell']),
            Paragraph('Amount (AED)', styles['header_cell_r']),
        ],
        [
            Paragraph('180+ Days (6+ months)', styles['cell']),
            Paragraph(
                _fmt(old_months),
                styles['cell_bold_r'] if old_months > 0 else styles['cell_r'],
            ),
        ],
        [
            Paragraph('360+ Days (12+ months)', styles['cell']),
            Paragraph(
                _fmt(very_old_months),
                styles['cell_bold_r'] if very_old_months > 0 else styles['cell_r'],
            ),
        ],
    ]
    aged_table = Table(aged_rows, colWidths=aged_col_widths)
    ts_aged = _standard_data_table_style(len(aged_rows), has_total_row=False)
    ts_aged.add('ALIGN', (1, 0), (1, -1), 'RIGHT')
    aged_table.setStyle(ts_aged)
    elements.append(aged_table)
    elements.append(Spacer(1, SP_SECTION + 4))

    # ── 6. Grand Total Summary ──
    elements.append(_build_section_header('Total Summary', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    # Determine the style for the final total
    if has_over_limit:
        final_total_style = ParagraphStyle(
            'FinalTotalDanger', parent=styles['cell_bold_r'],
            fontSize=FONT_KPI, textColor=CLR_DANGER,
        )
    else:
        final_total_style = ParagraphStyle(
            'FinalTotal', parent=styles['cell_bold_r'],
            fontSize=FONT_KPI, textColor=CLR_PRIMARY,
        )

    summary_col_widths = [2.8 * inch, 2.0 * inch]
    summary_rows = [
        [
            Paragraph('Description', styles['header_cell']),
            Paragraph('Amount (AED)', styles['header_cell_r']),
        ],
        [
            Paragraph('Last 6 Months Total', styles['cell']),
            Paragraph(_fmt(total_monthly), styles['cell_bold_r']),
        ],
        [
            Paragraph('180+ Days Pending', styles['cell']),
            Paragraph(_fmt(old_months), styles['cell_r']),
        ],
        [
            Paragraph('360+ Days Pending', styles['cell']),
            Paragraph(_fmt(very_old_months), styles['cell_r']),
        ],
        [
            Paragraph('<b>Total Outstanding</b>', styles['cell_bold']),
            Paragraph(_fmt(total_outstanding), styles['cell_bold_r']),
        ],
        [
            Paragraph('<b>PDC Received in Hand</b>', styles['cell_bold']),
            Paragraph(
                _fmt(pdc_received),
                ParagraphStyle(
                    'PDCDeduct', parent=styles['cell_bold_r'],
                    textColor=HexColor('#059669'),
                ),
            ),
        ],
        [
            Paragraph('<b>Net Outstanding (with PDC)</b>', styles['cell_bold']),
            Paragraph(_fmt(total_with_pdc), final_total_style),
        ],
    ]

    summary_table = Table(summary_rows, colWidths=summary_col_widths, repeatRows=1)

    # Custom styling for the summary table
    ts_summary = _standard_data_table_style(len(summary_rows), has_total_row=True)
    ts_summary.add('ALIGN', (1, 0), (1, -1), 'RIGHT')
    # Extra emphasis on the penultimate separator (before Total Outstanding)
    ts_summary.add('LINEABOVE', (0, -3), (-1, -3), 0.75, CLR_PRIMARY_LIGHT)
    # Double line above final row
    ts_summary.add('LINEABOVE', (0, -1), (-1, -1), 1.5, CLR_PRIMARY)
    # Larger padding on final row
    ts_summary.add('TOPPADDING', (0, -1), (-1, -1), 8)
    ts_summary.add('BOTTOMPADDING', (0, -1), (-1, -1), 8)

    summary_table.setStyle(ts_summary)
    elements.append(summary_table)

    # ── Build and return ──
    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response


@login_required
def export_finance_credit_edit_list_pdf(request):
    """
    Export consolidated manager credit edits to PDF using date range filter.
    """
    if request.user.username != 'manager':
        return HttpResponseForbidden("Only manager can export credit edit list.")

    today = datetime.now().date()
    from_date_str = request.GET.get('from_date', today.strftime('%Y-%m-%d'))
    to_date_str = request.GET.get('to_date', today.strftime('%Y-%m-%d'))

    try:
        from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
    except ValueError:
        from_date = today
        from_date_str = today.strftime('%Y-%m-%d')

    try:
        to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
    except ValueError:
        to_date = today
        to_date_str = today.strftime('%Y-%m-%d')

    if from_date > to_date:
        from_date, to_date = to_date, from_date
        from_date_str, to_date_str = to_date_str, from_date_str

    filtered_edits = FinanceCreditEditLog.objects.filter(
        created_at__date__gte=from_date,
        created_at__date__lte=to_date
    )
    latest_edit_ids = (
        filtered_edits
        .values('customer_id')
        .annotate(latest_id=Max('id'))
        .values_list('latest_id', flat=True)
    )
    edits = (
        FinanceCreditEditLog.objects
        .filter(id__in=latest_edit_ids)
        .select_related('customer__salesman', 'edited_by')
        .order_by('-created_at')
    )

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="finance_credit_edits_'
        f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    margin_h, margin_v = 24, 24
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 6,
    )

    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    subtitle = f"Manager credit edits from {from_date_str} to {to_date_str}"
    elements.extend(_build_document_header(
        styles,
        title_text='CREDIT EDIT CONSOLIDATED REPORT',
        subtitle_text=subtitle,
        page_width=usable_width,
    ))

    kpi_items = [
        ('Total Edits', str(edits.count())),
        ('From Date', from_date_str),
        ('To Date', to_date_str),
        ('Prepared By', request.user.username),
    ]
    elements.append(_build_kpi_bar(kpi_items, styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    col_widths = [
        0.35 * inch,
        1.00 * inch,
        2.30 * inch,
        1.00 * inch,
        1.20 * inch,
        1.00 * inch,
        0.90 * inch,
        2.20 * inch,
    ]
    allocated = sum(col_widths)
    remainder = max(0, usable_width - allocated)
    col_widths[7] += remainder

    table_data = [[
        Paragraph('#', styles['header_cell']),
        Paragraph('Code', styles['header_cell']),
        Paragraph('Customer', styles['header_cell']),
        Paragraph('Salesman', styles['header_cell']),
        Paragraph('Edited Limit', styles['header_cell_r']),
        Paragraph('Terms', styles['header_cell_r']),
        Paragraph('Edited By', styles['header_cell']),
        Paragraph('Edited At / Remarks', styles['header_cell']),
    ]]

    for idx, edit in enumerate(edits, start=1):
        salesman_name = (
            edit.customer.salesman.salesman_name
            if edit.customer and edit.customer.salesman else '—'
        )
        edit_by = edit.edited_by.username if edit.edited_by else '—'
        notes = edit.created_at.strftime('%d %b %Y %H:%M')
        if edit.remarks:
            notes = f"{notes} | {edit.remarks}"

        table_data.append([
            Paragraph(str(idx), styles['cell']),
            Paragraph(edit.customer.customer_code or '—', styles['cell']),
            Paragraph(edit.customer.customer_name or '—', styles['cell']),
            Paragraph(str(salesman_name)[:20], styles['cell']),
            Paragraph(_fmt(edit.edited_credit_limit), styles['cell_r']),
            Paragraph(str(edit.edited_credit_days or '—'), styles['cell_r']),
            Paragraph(edit_by, styles['cell']),
            Paragraph(notes, styles['cell']),
        ])

    if not edits.exists():
        table_data.append([
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('No edits found for selected date range.', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
        ])

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = _standard_data_table_style(len(table_data), has_total_row=False)
    table_style.add('ALIGN', (4, 0), (5, -1), 'RIGHT')
    table.setStyle(table_style)
    elements.append(table)

    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response