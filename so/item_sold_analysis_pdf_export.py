"""
Item Sold Analysis PDF Export
Firm-wise analysis: Qty Sold 2025/2026, Customer count per item.
Same layout approach as finance_statement_list / item_quoted PDFs: Paragraph cells + fixed
column widths so text wraps inside cells (no drawString overflow / overlap).
"""
import html
from io import BytesIO
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Spacer

from . import item_sold_analysis_views
from .finance_statement_pdf_export import (
    _build_document_header,
    _build_kpi_bar,
    _build_page_footer,
    _build_styles,
    _fmt,
    SP_SECTION,
)
from .item_quoted_analysis_pdf_export import (
    _build_analysis_styles,
    _build_analysis_table_style,
)


def _safe_float_pdf(x):
    if x is None:
        return 0.0
    try:
        v = float(x)
        if v != v:  # NaN
            return 0.0
        return v
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _safe_int_pdf(x):
    """Integer for PDF cells; avoids ValueError from NaN or non-numeric values."""
    v = _safe_float_pdf(x)
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _fmt_amount_pdf(x):
    return f"{_safe_float_pdf(x):,.0f}"


def _p(text, style):
    """Paragraph cell with XML-safe text (descriptions may contain &, <, >)."""
    if text is None:
        text = ''
    return Paragraph(html.escape(str(text)), style)


@login_required
def export_item_sold_analysis_pdf(request):
    """
    Export Item Sold Analysis to PDF.
    Query params: firm (multi), include_customers (1/true/yes/on), search.
    """
    include_customers = request.GET.get('include_customers', '').strip().lower() in ('1', 'true', 'yes', 'on')
    selected_firms = request.GET.getlist('firm')
    search_term = request.GET.get('search', '').strip()

    firm_list = list(dict.fromkeys([f.strip() for f in selected_firms if f and str(f).strip()]))

    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    margin_h = 22
    margin_v = 22

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 4,
    )
    usable_width = page_w - 2 * margin_h
    page_styles = _build_styles()
    ts = _build_analysis_styles()
    elements = []

    def _finish(response):
        doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
        response.write(buffer.getvalue())
        return response

    if not firm_list:
        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="item_sold_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
        )
        elements.extend(_build_document_header(
            page_styles,
            title_text='ITEM SOLD ANALYSIS',
            subtitle_text='No firm selected — use the report page to select firms, then export.',
            page_width=usable_width,
        ))
        elements.append(Paragraph(
            '<font color="#6B7280">No firm selected.</font>',
            page_styles['label'],
        ))
        return _finish(response)

    items_list, grand_total_2025, grand_total_2026, grand_total_customers = (
        item_sold_analysis_views._build_items_list_for_pdf(request, firm_list, search_term, include_customers)
    )

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="item_sold_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    firm_label = ', '.join(firm_list[:3])
    if len(firm_list) > 3:
        firm_label += f' (+{len(firm_list) - 3} more)'
    subtitle_parts = [firm_label, 'Qty Sold 2025 & 2026']
    if include_customers:
        subtitle_parts.append('Customer details loaded (summary table)')

    elements.extend(_build_document_header(
        page_styles,
        title_text='ITEM SOLD ANALYSIS',
        subtitle_text=' — '.join(subtitle_parts),
        page_width=usable_width,
    ))

    total_qty = _safe_float_pdf(grand_total_2025) + _safe_float_pdf(grand_total_2026)
    kpi_items = [
        ('Qty Sold 2025', _fmt(grand_total_2025)),
        ('Qty Sold 2026', _fmt(grand_total_2026)),
        ('Combined Qty', _fmt(total_qty)),
        ('Unique Items', str(len(items_list))),
        ('Unique Customers', str(grand_total_customers)),
    ]
    elements.append(_build_kpi_bar(kpi_items, page_styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    if not items_list:
        elements.append(Paragraph(
            '<font color="#6B7280">No items found for the selected firm(s).</font>',
            page_styles['label'],
        ))
        return _finish(response)

    # Column layout — match item quoted PDF: fixed widths + remainder for Description (wraps via Paragraph)
    W_NUM = 18
    W_CODE = 52
    W_UPC = 48
    W_STOCK = 42
    W_IMPORT = 42
    W_QTY = 46
    W_AMT = 52
    W_INV = 36
    W_CUST = 36

    fixed_total = (
        W_NUM + W_CODE + W_UPC + W_STOCK + W_IMPORT
        + (2 * W_QTY) + (2 * W_AMT) + (2 * W_INV) + W_CUST
    )
    W_DESC = max(100, usable_width - fixed_total)

    col_widths = [
        W_NUM, W_CODE, W_DESC, W_UPC, W_STOCK, W_IMPORT,
        W_QTY, W_QTY, W_AMT, W_AMT, W_INV, W_INV, W_CUST,
    ]

    hdr = [
        Paragraph('#', ts['th_c']),
        Paragraph('Item<br/>Code', ts['th_c']),
        Paragraph('Description', ts['th']),
        Paragraph('UPC', ts['th']),
        Paragraph('Stock', ts['th_r']),
        Paragraph('Import<br/>+ LPO', ts['th_c']),
        Paragraph('Qty<br/>2025', ts['th_c']),
        Paragraph('Qty<br/>2026', ts['th_c']),
        Paragraph('Amt<br/>2025', ts['th_c']),
        Paragraph('Amt<br/>2026', ts['th_c']),
        Paragraph('Invs<br/>25', ts['th_c']),
        Paragraph('Invs<br/>26', ts['th_c']),
        Paragraph('Cust.', ts['th_c']),
    ]
    table_data = [hdr]

    for idx, item in enumerate(items_list[:500], start=1):
        desc_text = item.get('item_description') or '—'
        row = [
            Paragraph(str(idx), ts['td_c']),
            _p(item.get('item_code') or '—', ts['td_bold']),
            _p(desc_text, ts['td']),
            _p(item.get('upc_code') or '—', ts['td']),
            Paragraph(_fmt_amount_pdf(item.get('total_stock', 0)), ts['td_r']),
            Paragraph(_fmt_amount_pdf(item.get('import_ordered', 0)), ts['td_r']),
            Paragraph(str(_safe_int_pdf(item.get('qty_sold_2025', 0))), ts['td_bold_r']),
            Paragraph(str(_safe_int_pdf(item.get('qty_sold_2026', 0))), ts['td_bold_r']),
            Paragraph(_fmt_amount_pdf(item.get('total_amount_2025', 0)), ts['td_bold_r']),
            Paragraph(_fmt_amount_pdf(item.get('total_amount_2026', 0)), ts['td_bold_r']),
            Paragraph(str(_safe_int_pdf(item.get('total_invoices_2025', 0))), ts['td_r']),
            Paragraph(str(_safe_int_pdf(item.get('total_invoices_2026', 0))), ts['td_r']),
            Paragraph(str(_safe_int_pdf(item.get('customer_sold_count', 0))), ts['td_c']),
        ]
        table_data.append(row)

    total_stock = sum(_safe_float_pdf(i.get('total_stock', 0)) for i in items_list)
    total_import = sum(_safe_float_pdf(i.get('import_ordered', 0)) for i in items_list)
    total_amt_25 = sum(_safe_float_pdf(i.get('total_amount_2025', 0)) for i in items_list)
    total_amt_26 = sum(_safe_float_pdf(i.get('total_amount_2026', 0)) for i in items_list)
    total_inv_25 = sum(_safe_int_pdf(i.get('total_invoices_2025', 0)) for i in items_list)
    total_inv_26 = sum(_safe_int_pdf(i.get('total_invoices_2026', 0)) for i in items_list)

    total_row = [
        Paragraph('', ts['td']),
        Paragraph('TOTAL', ts['td_bold']),
        Paragraph(f'{len(items_list)} items', ts['total_label']),
        Paragraph('', ts['td']),
        Paragraph(_fmt(total_stock), ts['td_bold_r']),
        Paragraph(_fmt(total_import), ts['td_bold_r']),
        Paragraph(_fmt(grand_total_2025), ts['td_bold_r']),
        Paragraph(_fmt(grand_total_2026), ts['td_bold_r']),
        Paragraph(_fmt(total_amt_25), ts['td_bold_r']),
        Paragraph(_fmt(total_amt_26), ts['td_bold_r']),
        Paragraph(str(total_inv_25), ts['td_bold_r']),
        Paragraph(str(total_inv_26), ts['td_bold_r']),
        Paragraph(str(grand_total_customers), ts['td_bold']),
    ]
    table_data.append(total_row)

    data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = _build_analysis_table_style(num_rows=len(table_data), customer_row_indices=set())
    table_style.add('ALIGN', (4, 0), (11, -1), 'RIGHT')
    table_style.add('ALIGN', (12, 0), (12, -1), 'CENTER')
    table_style.add('ALIGN', (0, 0), (0, -1), 'CENTER')
    table_style.add('ALIGN', (1, 0), (3, -1), 'LEFT')
    data_table.setStyle(table_style)
    elements.append(data_table)

    if len(items_list) > 500:
        elements.append(Spacer(1, 0.1 * inch))
        elements.append(Paragraph(
            f'<font color="#6B7280">(Showing first 500 of {len(items_list)} items)</font>',
            page_styles['label'],
        ))

    return _finish(response)
