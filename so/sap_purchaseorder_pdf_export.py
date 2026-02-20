"""
Open Purchase Order List PDF Export - Same layout as Finance Statement list.
Uses shared design helpers from finance_statement_pdf_export.
"""
from io import BytesIO
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse

from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Spacer

from django.db.models import DecimalField
from so.sap_purchaseorder_views import (
    _purchaseorder_items_qs,
    _apply_purchaseorder_list_filters,
    _user_can_see_price,
)
from so.finance_statement_pdf_export import (
    _build_styles,
    _build_document_header,
    _build_kpi_bar,
    _standard_data_table_style,
    _build_page_footer,
    _fmt,
)


def _d(x):
    """Convert to Decimal for formatting."""
    if x is None:
        return Decimal("0")
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


@login_required
def export_purchaseorder_list_pdf(request):
    """
    Export Open Purchase Order List to PDF - Same layout as Finance Statement list.
    Document header, KPI bar, filters note, data table, page footer.
    Respects all filters: q, item, firm, purchaser, start/end date, total range.
    Price columns only for admin when show_price=1.
    """
    qs = _purchaseorder_items_qs()
    qs = _apply_purchaseorder_list_filters(qs, request)

    is_admin = _user_can_see_price(request)
    show_price = is_admin and request.GET.get('show_price') == '1'

    # Aggregates for KPI bar
    agg = qs.aggregate(
        pending_total=Coalesce(Sum('pending_amount'), Value(0, output_field=DecimalField())),
        row_total_sum=Coalesce(Sum('row_total'), Value(0, output_field=DecimalField())),
    )
    pending_total = float(agg['pending_total'] or 0)
    row_total_sum = float(agg['row_total_sum'] or 0)
    item_count = qs.count()

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="open_purchase_orders_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    from reportlab.lib.units import inch
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

    # 1. Document Header
    elements.extend(_build_document_header(
        styles,
        title_text='OPEN PURCHASE ORDERS',
        subtitle_text='Item-level open PO lines (DocumentStatus=bost_Open)',
        page_width=usable_width,
    ))

    # 2. KPI Summary Bar
    kpi_items = [('Line Items', str(item_count))]
    if show_price:
        kpi_items.append(('Pending Total (AED)', _fmt(pending_total)))
        kpi_items.append(('Row Total (AED)', _fmt(row_total_sum)))
    elements.append(_build_kpi_bar(kpi_items, styles, usable_width))
    elements.append(Spacer(1, 10))

    # 3. Active Filters note
    q = request.GET.get('q', '').strip()
    item_filter = request.GET.get('item', '').strip()
    firm_filter = request.GET.getlist('firm')
    purchaser_filter = request.GET.getlist('purchaser')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    total_range = request.GET.get('total', '').strip()

    active_filters = []
    if q:
        active_filters.append(f'Search: "{q}"')
    if item_filter:
        active_filters.append(f'Item: "{item_filter}"')
    if firm_filter:
        active_filters.append(f'Brand: {", ".join(firm_filter[:3])}{"…" if len(firm_filter) > 3 else ""}')
    if purchaser_filter:
        active_filters.append(f'Purchaser: {", ".join(purchaser_filter[:3])}{"…" if len(purchaser_filter) > 3 else ""}')
    if start:
        active_filters.append(f'From: {start}')
    if end:
        active_filters.append(f'To: {end}')
    if total_range:
        active_filters.append(f'Amount: {total_range}')

    if active_filters:
        filter_text = '  •  '.join(active_filters)
        elements.append(Paragraph(
            f'<font color="#6B7280">Filters applied: {filter_text}</font>',
            styles['label'],
        ))
        elements.append(Spacer(1, 4))

    # 4. Data Table - Column widths for landscape A4
    if show_price:
        col_widths = [
            0.95 * inch,   # Doc No
            0.35 * inch,   # St
            0.55 * inch,   # Date
            0.70 * inch,   # Supplier Code
            1.80 * inch,   # Supplier Name
            0.60 * inch,   # Item No
            1.40 * inch,   # Description
            0.40 * inch,   # Qty
            0.55 * inch,   # Price
            0.65 * inch,   # Pending
            0.65 * inch,   # Row Total
        ]
    else:
        col_widths = [
            0.95 * inch,   # Doc No
            0.35 * inch,   # St
            0.55 * inch,   # Date
            0.70 * inch,   # Supplier Code
            1.80 * inch,   # Supplier Name
            0.60 * inch,   # Item No
            2.25 * inch,   # Description (more room)
            0.40 * inch,   # Qty
        ]
    allocated = sum(col_widths)
    remainder = max(0, usable_width - allocated)
    col_widths[6] += remainder  # Give remainder to Description

    hdr = [
        Paragraph('Doc No', styles['header_cell']),
        Paragraph('St', styles['header_cell']),
        Paragraph('Date', styles['header_cell']),
        Paragraph('Supplier Code', styles['header_cell']),
        Paragraph('Supplier Name', styles['header_cell']),
        Paragraph('Item No', styles['header_cell']),
        Paragraph('Description', styles['header_cell']),
        Paragraph('Qty', styles['header_cell_r']),
    ]
    if show_price:
        hdr.extend([
            Paragraph('Price', styles['header_cell_r']),
            Paragraph('Pending', styles['header_cell_r']),
            Paragraph('Total', styles['header_cell_r']),
        ])
    table_data = [hdr]

    for item in qs:
        po = item.purchaseorder
        row = [
            Paragraph(po.po_number or '—', styles['cell']),
            Paragraph('O', styles['cell']),
            Paragraph(po.posting_date.strftime('%d/%m/%y') if po.posting_date else '—', styles['cell']),
            Paragraph(po.supplier_code or '—', styles['cell']),
            Paragraph((po.supplier_name or '—')[:45], styles['cell']),
            Paragraph(item.item_no or '—', styles['cell']),
            Paragraph((item.description or '—')[:50], styles['cell']),
            Paragraph(str(int(item.quantity)) if item.quantity else '0', styles['cell_r']),
        ]
        if show_price:
            row.extend([
                Paragraph(_fmt(_d(item.price)), styles['cell_r']),
                Paragraph(_fmt(_d(item.pending_amount)), styles['cell_r']),
                Paragraph(_fmt(_d(item.row_total)), styles['cell_r']),
            ])
        table_data.append(row)

    # Totals row
    totals_row = [
        Paragraph('', styles['cell']),
        Paragraph('', styles['cell']),
        Paragraph('', styles['cell']),
        Paragraph('', styles['cell']),
        Paragraph('<b>TOTAL</b>', styles['cell_bold']),
        Paragraph(f'<i>{item_count} items</i>', styles['label']),
        Paragraph('', styles['cell']),
        Paragraph('', styles['cell']),
    ]
    if show_price:
        totals_row.extend([
            Paragraph('', styles['cell']),
            Paragraph(_fmt(pending_total), styles['cell_bold_r']),
            Paragraph(_fmt(row_total_sum), styles['cell_bold_r']),
        ])
    table_data.append(totals_row)

    data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    ts = _standard_data_table_style(len(table_data), has_total_row=True)
    ts.add('ALIGN', (7, 0), (-1, -1), 'RIGHT')  # Right-align numeric columns
    data_table.setStyle(ts)
    elements.append(data_table)

    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response
