"""
SAP Sales Order PDF — uses the same design as Customer Order PDF (views.py).
Design elements are duplicated here; views.py is NOT modified or imported.
"""
import os
from io import BytesIO
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, Image, KeepTogether,
)

# ─────────────────────────────────────────────────────────────────────────────
# THEME DEFINITIONS — same palette as Customer Order (junaid)
# ─────────────────────────────────────────────────────────────────────────────

SAP_PDF_THEME = {
    'name': 'JUNAID',
    'primary': HexColor('#1B2A4A'),
    'primary_light': HexColor('#2C4A7C'),
    'accent': HexColor('#D4912A'),
    'accent_light': HexColor('#F5E6CC'),
    'header_bg': HexColor('#1B2A4A'),
    'row_alt': HexColor('#F7F9FC'),
    'row_white': colors.white,
    'total_bg': HexColor('#EBF0F7'),
    'grand_total_bg': HexColor('#E8F0E8'),
    'border': HexColor('#D1D5DB'),
    'border_heavy': HexColor('#9CA3AF'),
    'text': HexColor('#1F2937'),
    'text_muted': HexColor('#6B7280'),
    'text_white': colors.white,
    'logo_urls': [
        'https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp',
    ],
    'logo_local': [
        'media/footer-logo1.png',
        'static/images/footer-logo.png',
    ],
    'ramdan_local': 'media/ramdan1.png',
    'terms_prefix': 'Junaid Trading',
}

# ─────────────────────────────────────────────────────────────────────────────
# TYPOGRAPHY & SPACING CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

FONT_TITLE = 16
FONT_SUBTITLE = 8
FONT_SECTION = 10
FONT_BODY = 8.5
FONT_BODY_SM = 7.5
FONT_TABLE_HDR = 8
FONT_TABLE_BODY = 7.5
FONT_FOOTER = 6
FONT_TERMS = 7
FONT_NOTES = 7.5
FONT_GRAND_TOTAL = 11

LOGO_WIDTH = 2.5 * inch
LOGO_HEIGHT = 0.95 * inch
RAMDAN_LOGO_WIDTH = 1.0 * inch
RAMDAN_LOGO_HEIGHT = 0.6 * inch

PAGE_MARGIN_H = 0.5 * inch
PAGE_MARGIN_TOP = 0.5 * inch
PAGE_MARGIN_BOT = 0.5 * inch

SP_SECTION = 6
SP_INNER = 3
SP_AFTER_TABLE = 4


def _build_styles(theme):
    """Build a dict of ParagraphStyles bound to the given theme palette."""
    base = getSampleStyleSheet()['Normal']
    return {
        'title': ParagraphStyle(
            'SAPTitle', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TITLE,
            textColor=theme['primary'], leading=FONT_TITLE + 4,
            alignment=TA_CENTER,
        ),
        'subtitle': ParagraphStyle(
            'SAPSubtitle', parent=base,
            fontName='Helvetica', fontSize=FONT_SUBTITLE,
            textColor=theme['text_muted'], leading=FONT_SUBTITLE + 3,
            alignment=TA_CENTER,
        ),
        'section': ParagraphStyle(
            'SAPSection', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_SECTION,
            textColor=theme['text_white'], leading=FONT_SECTION + 2,
        ),
        'label': ParagraphStyle(
            'SAPLabel', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY_SM,
            textColor=theme['text_muted'], leading=FONT_BODY_SM + 2,
        ),
        'value': ParagraphStyle(
            'SAPValue', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY,
            textColor=theme['text'], leading=FONT_BODY + 2,
        ),
        'value_bold': ParagraphStyle(
            'SAPValueBold', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=theme['text'], leading=FONT_BODY + 2,
        ),
        'th': ParagraphStyle(
            'SAPTH', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TABLE_HDR,
            textColor=theme['text_white'], leading=FONT_TABLE_HDR + 2,
        ),
        'th_r': ParagraphStyle(
            'SAPTHRight', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TABLE_HDR,
            textColor=theme['text_white'], leading=FONT_TABLE_HDR + 2,
            alignment=TA_RIGHT,
        ),
        'th_c': ParagraphStyle(
            'SAPTHCenter', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TABLE_HDR,
            textColor=theme['text_white'], leading=FONT_TABLE_HDR + 2,
            alignment=TA_CENTER,
        ),
        'td': ParagraphStyle(
            'SAPTD', parent=base,
            fontName='Helvetica', fontSize=FONT_TABLE_BODY,
            textColor=theme['text'], leading=FONT_TABLE_BODY + 2,
        ),
        'td_c': ParagraphStyle(
            'SAPTDCenter', parent=base,
            fontName='Helvetica', fontSize=FONT_TABLE_BODY,
            textColor=theme['text'], leading=FONT_TABLE_BODY + 2,
            alignment=TA_CENTER,
        ),
        'td_r': ParagraphStyle(
            'SAPTDRight', parent=base,
            fontName='Helvetica', fontSize=FONT_TABLE_BODY,
            textColor=theme['text'], leading=FONT_TABLE_BODY + 2,
            alignment=TA_RIGHT,
        ),
        'td_bold': ParagraphStyle(
            'SAPTDBold', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TABLE_BODY,
            textColor=theme['text'], leading=FONT_TABLE_BODY + 2,
        ),
        'td_bold_r': ParagraphStyle(
            'SAPTDBoldR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TABLE_BODY,
            textColor=theme['text'], leading=FONT_TABLE_BODY + 2,
            alignment=TA_RIGHT,
        ),
        'summary_label': ParagraphStyle(
            'SAPSumLabel', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=theme['text'], leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'summary_value': ParagraphStyle(
            'SAPSumValue', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=theme['text'], leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'grand_label': ParagraphStyle(
            'SAPGrandLabel', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_GRAND_TOTAL,
            textColor=theme['primary'], leading=FONT_GRAND_TOTAL + 4,
            alignment=TA_RIGHT,
        ),
        'grand_value': ParagraphStyle(
            'SAPGrandValue', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_GRAND_TOTAL,
            textColor=theme['primary'], leading=FONT_GRAND_TOTAL + 4,
            alignment=TA_RIGHT,
        ),
        'notes': ParagraphStyle(
            'SAPNotes', parent=base,
            fontName='Helvetica', fontSize=FONT_NOTES,
            textColor=theme['text_muted'], leading=FONT_NOTES + 3,
        ),
        'terms': ParagraphStyle(
            'SAPTerms', parent=base,
            fontName='Helvetica', fontSize=FONT_TERMS,
            textColor=theme['text_muted'], leading=FONT_TERMS + 3,
        ),
        'footer': ParagraphStyle(
            'SAPFooter', parent=base,
            fontName='Helvetica', fontSize=FONT_FOOTER,
            textColor=theme['text_muted'], leading=FONT_FOOTER + 2,
            alignment=TA_CENTER,
        ),
    }


def _get_ramdan_logo_path(theme=None):
    """Return absolute path to media/ramdan.png."""
    if theme and theme.get('ramdan_local'):
        full_path = os.path.join(settings.BASE_DIR, theme['ramdan_local'])
        full_path = os.path.normpath(os.path.abspath(full_path))
        if os.path.isfile(full_path):
            return full_path
    base = str(getattr(settings, 'BASE_DIR', ''))
    full_path = os.path.abspath(os.path.join(base, 'media', 'ramdan1.png'))
    if os.path.isfile(full_path):
        return full_path
    media_dir = os.path.join(base, 'media')
    if os.path.isdir(media_dir):
        for f in os.listdir(media_dir):
            if f.lower() == 'ramdan1.png':
                return os.path.abspath(os.path.join(media_dir, f))
    return None


def _load_logo(theme):
    """Try local files first, then URL fallback. Returns ReportLab Image or None."""
    for rel_path in theme['logo_local']:
        full_path = os.path.join(settings.BASE_DIR, rel_path)
        if os.path.exists(full_path):
            try:
                return Image(full_path, width=LOGO_WIDTH, height=LOGO_HEIGHT)
            except Exception:
                continue
    for url in theme['logo_urls']:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                return Image(BytesIO(resp.content), width=LOGO_WIDTH, height=LOGO_HEIGHT)
        except Exception:
            continue
    return None


def _build_header(theme, styles, usable_width, title='SALES ORDER'):
    """Build header: logo + Ramdan logo → Title → Accent line → Subtitle."""
    elements = []
    logo = _load_logo(theme)
    ramdan_img = None
    ramdan_path = _get_ramdan_logo_path(theme)
    if ramdan_path:
        try:
            ramdan_img = Image(str(ramdan_path), width=RAMDAN_LOGO_WIDTH, height=RAMDAN_LOGO_HEIGHT)
        except Exception:
            pass

    if logo and ramdan_img:
        side_w = (usable_width - LOGO_WIDTH) / 2
        logo_row = Table(
            [['', logo, ramdan_img]],
            colWidths=[side_w, LOGO_WIDTH, side_w],
        )
        logo_row.setStyle(TableStyle([
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'CENTER'),
            ('ALIGN', (2, 0), (2, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        elements.append(logo_row)
        elements.append(Spacer(1, 2))
    elif logo:
        logo.hAlign = 'CENTER'
        elements.append(logo)
        elements.append(Spacer(1, 4))
    elif ramdan_img:
        ramdan_img.hAlign = 'RIGHT'
        elements.append(ramdan_img)
        elements.append(Spacer(1, 4))

    elements.append(Paragraph(title, styles['title']))
    elements.append(Spacer(1, 1))

    line_data = [['']]
    line_tbl = Table(line_data, colWidths=[usable_width * 0.4])
    line_tbl.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, -1), 2, theme['accent']),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ]))
    wrapper = Table([[line_tbl]], colWidths=[usable_width])
    wrapper.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(wrapper)
    elements.append(Spacer(1, 2))

    elements.append(Paragraph(
        f"Generated on {datetime.now().strftime('%d %B %Y at %H:%M')}",
        styles['subtitle'],
    ))
    elements.append(Spacer(1, SP_SECTION))

    return elements


def _build_section_bar(title, theme, styles, usable_width):
    """Full-width section header bar with accent left-strip."""
    accent_w = 4
    content_w = usable_width - accent_w
    tbl = Table(
        [['', Paragraph(title, styles['section'])]],
        colWidths=[accent_w, content_w],
    )
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), theme['accent']),
        ('BACKGROUND', (1, 0), (1, 0), theme['header_bg']),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (1, 0), (1, 0), 8),
    ]))
    return tbl


def _build_info_table(rows, theme, styles, usable_width):
    """Build two-column key:value information grid."""
    label_w = 1.6 * inch
    value_w = usable_width / 2 - label_w
    col_widths = [label_w, value_w, label_w, value_w]

    table_data = []
    for i in range(0, len(rows), 2):
        left = rows[i]
        right = rows[i + 1] if i + 1 < len(rows) else ('', '')
        table_data.append([
            Paragraph(left[0], styles['label']),
            Paragraph(str(left[1]), styles['value_bold']),
            Paragraph(right[0], styles['label']),
            Paragraph(str(right[1]), styles['value_bold']),
        ])

    tbl = Table(table_data, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -2), 0.25, theme['border']),
        ('BACKGROUND', (0, 0), (0, -1), HexColor('#F9FAFB')),
        ('BACKGROUND', (2, 0), (2, -1), HexColor('#F9FAFB')),
    ]))
    return tbl


def _to_decimal(x):
    if x is None:
        return Decimal('0')
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal('0')


def _build_sap_items_table(items_qs, theme, styles, usable_width):
    """Build SAP line-items table with Rev. Price column and zebra striping."""
    col_widths = [
        0.35 * inch,   # #
        0.95 * inch,   # Item No
        2.20 * inch,   # Description
        0.50 * inch,   # Qty
        0.75 * inch,   # Unit Price
        0.75 * inch,   # Rev. Price
        0.85 * inch,   # Total
    ]
    allocated = sum(col_widths)
    col_widths[2] += max(0, usable_width - allocated)

    hdr = [
        Paragraph('#', styles['th_c']),
        Paragraph('Item No.', styles['th_c']),
        Paragraph('Description', styles['th']),
        Paragraph('Qty', styles['th_c']),
        Paragraph('Unit Price', styles['th_r']),
        Paragraph('Rev. Price', styles['th_r']),
        Paragraph('Rev.Total', styles['th_r']),
    ]
    table_data = [hdr]

    subtotal = Decimal('0')
    for idx, it in enumerate(items_qs, 1):
        qty = _to_decimal(it.quantity)
        price = _to_decimal(it.price)
        orig_row = _to_decimal(it.row_total) if getattr(it, 'row_total', None) is not None else None
        # Unit price: row_total/qty when available (more reliable), else price
        if orig_row is not None and qty:
            unit_price = (orig_row / qty).quantize(Decimal("0.01"))
        else:
            unit_price = price
        rev_price_raw = getattr(it, 'revised_price', None)
        rev_price = _to_decimal(rev_price_raw) if rev_price_raw is not None else None
        if rev_price is not None and rev_price == 0:
            rev_price = None

        # Use revised price for row total when available; otherwise use unit price
        effective_price = rev_price if (rev_price is not None and rev_price > 0) else unit_price
        row_total = (qty * effective_price).quantize(Decimal("0.01"))

        if (price == 0 or price is None) and qty and orig_row is not None:
            try:
                price = (orig_row / qty).quantize(Decimal("0.01"))
            except Exception:
                price = Decimal("0")
        subtotal += row_total

        desc = (it.description or '—')[:55] + ('…' if len(it.description or '') > 55 else '')
        qty_str = f"{qty.normalize():f}".rstrip('0').rstrip('.') if qty else "0"
        rev_price_str = f"{rev_price:,.2f}" if rev_price and rev_price > 0 else "—"

        table_data.append([
            Paragraph(str(idx), styles['td_c']),
            Paragraph(it.item_no or '—', styles['td_c']),
            Paragraph(desc, styles['td_bold']),
            Paragraph(qty_str, styles['td_c']),
            Paragraph(f"{price:,.2f}", styles['td_r']),
            Paragraph(rev_price_str, styles['td_r']),
            Paragraph(f"{row_total:,.2f}", styles['td_r']),
        ])

    num_rows = len(table_data)
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)

    cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), theme['header_bg']),
        ('TEXTCOLOR', (0, 0), (-1, 0), theme['text_white']),
        ('BOX', (0, 0), (-1, -1), 0.75, theme['border_heavy']),
        ('LINEBELOW', (0, 0), (-1, 0), 1, theme['border_heavy']),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 1), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    for i in range(1, num_rows):
        bg = theme['row_alt'] if i % 2 == 0 else theme['row_white']
        cmds.append(('BACKGROUND', (0, i), (-1, i), bg))
        if i < num_rows - 1:
            cmds.append(('LINEBELOW', (0, i), (-1, i), 0.25, theme['border']))

    tbl.setStyle(TableStyle(cmds))
    return tbl, subtotal


def _build_sap_summary_block(salesorder, subtotal, theme, styles, usable_width):
    """Build SAP summary: Document Total, VAT, Grand Total."""
    label_w = 1.6 * inch
    value_w = 1.3 * inch
    spacer_w = usable_width - label_w - value_w

    # Use computed subtotal (includes revised prices when set) for Document Total
    doc_total = subtotal.quantize(Decimal("0.01"))
    vat_rate = Decimal("0.05")
    vat_amount = (doc_total * vat_rate).quantize(Decimal("0.01"))
    grand_total = (doc_total + vat_amount).quantize(Decimal("0.01"))

    rows = [
        ['', '', Paragraph('Document Total:', styles['summary_label']),
         Paragraph(f"{doc_total:,.2f} AED", styles['summary_value'])],
        ['', '', Paragraph('VAT (5%):', styles['summary_label']),
         Paragraph(f"{vat_amount:,.2f} AED", styles['summary_value'])],
        ['', '', Paragraph('Grand Total:', styles['grand_label']),
         Paragraph(f"{grand_total:,.2f} AED", styles['grand_value'])],
    ]

    half_spacer = spacer_w / 2
    col_widths = [half_spacer, half_spacer, label_w, value_w]
    tbl = Table(rows, colWidths=col_widths)

    cmds = [
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -2), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -2), 2),
        ('TOPPADDING', (0, -1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 6),
        ('LINEABOVE', (2, -1), (3, -1), 1.5, theme['primary']),
        ('BACKGROUND', (2, -1), (3, -1), theme['grand_total_bg']),
        ('LINEABOVE', (2, 0), (3, 0), 0.5, theme['border']),
    ]
    tbl.setStyle(TableStyle(cmds))
    return tbl


def _build_terms_block(theme, styles):
    """Build terms & conditions section."""
    elements = []
    elements.append(Spacer(1, SP_SECTION))
    elements.append(Spacer(1, 1))

    heading_style = ParagraphStyle(
        'TermsHeading', parent=styles['label'],
        fontSize=FONT_BODY_SM, textColor=theme['text_muted'],
        fontName='Helvetica-Bold',
    )
    elements.append(Paragraph('Terms & Conditions', heading_style))
    elements.append(Spacer(1, 2))

    terms = [
        "1. This sales order is valid for 7 days from the date of issue.",
        "2. Prices are subject to change after the validity period.",
        "3. Delivery timelines to be confirmed upon order confirmation.",
        "4. System-generated document by Junaid Trading.",
    ]
    for term in terms:
        elements.append(Paragraph(term, styles['terms']))
        elements.append(Spacer(1, 1))

    return elements


def _page_footer_factory(theme):
    """Return onPage callback for branded footer."""

    def _draw_footer(canvas, doc):
        canvas.saveState()
        page_w, page_h = doc.pagesize
        y_line = PAGE_MARGIN_BOT - 12
        canvas.setStrokeColor(theme['accent'])
        canvas.setLineWidth(0.75)
        canvas.line(PAGE_MARGIN_H, y_line, page_w - PAGE_MARGIN_H, y_line)
        canvas.setFont('Helvetica', 6)
        canvas.setFillColor(theme['text_muted'])
        footer_text = (
            f"Page {doc.page}  ·  {theme['name']}  ·  "
            f"Generated {datetime.now().strftime('%d %b %Y %H:%M')}  ·  "
            f"Confidential"
        )
        canvas.drawCentredString(page_w / 2, y_line - 10, footer_text)
        canvas.restoreState()

    return _draw_footer


def generate_sap_salesorder_pdf_bytes(salesorder):
    """
    Generate SAP Sales Order PDF bytes using the Customer Order design.
    Does NOT import or modify views.py.
    """
    theme = SAP_PDF_THEME
    items_qs = salesorder.items.all().order_by('id')

    page_w, page_h = A4
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=PAGE_MARGIN_H,
        leftMargin=PAGE_MARGIN_H,
        topMargin=PAGE_MARGIN_TOP,
        bottomMargin=PAGE_MARGIN_BOT,
    )
    usable_width = page_w - 2 * PAGE_MARGIN_H
    styles = _build_styles(theme)
    elements = []

    # 1. Header
    elements.extend(_build_header(theme, styles, usable_width, title='SALES ORDER'))

    # 2. Sales Order Information
    elements.append(_build_section_bar('SALES ORDER INFORMATION', theme, styles, usable_width))
    elements.append(Spacer(1, SP_INNER))

    posting_str = salesorder.posting_date.strftime('%d %B %Y') if salesorder.posting_date else '—'
    order_info_rows = [
        ('Number', salesorder.so_number or '—'),
        ('Date', posting_str),
        ('BP Ref', salesorder.bp_reference_no or '—'),
        ('Status', salesorder.status or '—'),
    ]
    elements.append(_build_info_table(order_info_rows, theme, styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    # 3. Customer Information
    elements.append(_build_section_bar('CUSTOMER INFORMATION', theme, styles, usable_width))
    elements.append(Spacer(1, SP_INNER))

    customer_info_rows = [
        ('Customer Name', salesorder.customer_name or '—'),
        ('Customer Code', salesorder.customer_code or '—'),
        ('Salesman', salesorder.salesman_name or '—'),
    ]
    if len(customer_info_rows) % 2 != 0:
        customer_info_rows.append(('', ''))
    elements.append(_build_info_table(customer_info_rows, theme, styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    # 4. Line Items
    elements.append(_build_section_bar('ORDER ITEMS', theme, styles, usable_width))
    elements.append(Spacer(1, SP_INNER))

    items_table, subtotal = _build_sap_items_table(items_qs, theme, styles, usable_width)
    elements.append(items_table)
    elements.append(Spacer(1, SP_AFTER_TABLE))

    # 5. Summary
    summary_block = _build_sap_summary_block(salesorder, subtotal, theme, styles, usable_width)
    elements.append(summary_block)
    elements.append(Spacer(1, SP_SECTION))

    # 6. Terms & Conditions
    elements.extend(_build_terms_block(theme, styles))

    # Build
    footer_fn = _page_footer_factory(theme)
    doc.build(elements, onFirstPage=footer_fn, onLaterPages=footer_fn)

    pdf = buffer.getvalue()
    buffer.close()
    return pdf
