"""Excel/PDF export of the consolidated Stock Shortage Report
(emailagent.models.StockShortageReport) -- same flattened rows as
stock_shortage_detail.html's table (one row per line x lpo_breakdown
entry, or one row for a line with no breakdown). PDF styling reuses the
shared house style from so.finance_statement_pdf_export so this matches
every other export in the app rather than inventing a new look."""
from datetime import datetime
from io import BytesIO

import pandas as pd
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Spacer

from so.finance_statement_pdf_export import (
    _build_styles,
    _build_document_header,
    _build_kpi_bar,
    _standard_data_table_style,
    _build_page_footer,
)

from .models import StockShortageReport


def _flatten_rows(report):
    """One dict per (line, lpo_breakdown entry) pair -- or one dict for a
    line with no breakdown at all -- matching stock_shortage_detail.html's
    row-per-LPO-entry table exactly, so the export always matches what's
    on screen."""
    rows = []
    for line in report.lines:
        breakdown = line.get('lpo_breakdown') or []
        common = {
            'Item Code': line.get('item_code', ''),
            'Brand': line.get('brand') or '—',
            'Description': line.get('description', ''),
            'Total Required Qty': line.get('total_required_qty'),
            'Available Stock': line.get('available_qty'),
            'Final Qty (To Procure)': line.get('final_qty'),
            'LPO Sent to Supplier': line.get('already_ordered_qty'),
            'Final Qty to Purchase': line.get('final_purchase_qty'),
        }
        if not breakdown:
            rows.append({
                **common,
                'Customer': '—', 'LPO': '—', 'Sales Order': '—',
                'LPO Qty': None, 'Payment Terms': '—',
            })
            continue
        for entry in breakdown:
            rows.append({
                **common,
                'Customer': entry.get('customer_name') or '—',
                'LPO': entry.get('lpo_number') or '—',
                'Sales Order': entry.get('sales_order_number') or '—',
                'LPO Qty': entry.get('quantity'),
                'Payment Terms': entry.get('payment_terms') or '—',
            })
    return rows


@login_required
def export_stock_shortage_excel(request):
    report = (StockShortageReport.objects.filter(pk=1).first()) or StockShortageReport.current()
    rows = _flatten_rows(report)

    columns = [
        'Item Code', 'Brand', 'Description', 'Customer', 'LPO', 'Sales Order',
        'LPO Qty', 'Total Required Qty', 'Available Stock', 'Final Qty (To Procure)',
        'LPO Sent to Supplier', 'Final Qty to Purchase', 'Payment Terms',
    ]
    df = pd.DataFrame(rows, columns=columns)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Stock Shortage', index=False)

        worksheet = writer.sheets['Stock Shortage']

        from openpyxl.styles import Font, PatternFill, Alignment
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        worksheet.row_dimensions[1].height = 30

        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except Exception:
                    pass
            worksheet.column_dimensions[column_letter].width = min(max_length + 2, 50)

    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    filename = f"Stock_Shortage_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _num(value):
    return '—' if value is None else f'{value:,.2f}'


@login_required
def export_stock_shortage_pdf(request):
    report = (StockShortageReport.objects.filter(pk=1).first()) or StockShortageReport.current()
    rows = _flatten_rows(report)

    response = HttpResponse(content_type='application/pdf')
    filename = f"Stock_Shortage_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    margin_h, margin_v = 24, 24
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        rightMargin=margin_h, leftMargin=margin_h,
        topMargin=margin_v, bottomMargin=margin_v + 6,
    )
    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    elements.extend(_build_document_header(
        styles,
        title_text='STOCK SHORTAGE REPORT',
        subtitle_text='Consolidated shortfall across every pending LPO-sourced sales order',
        page_width=usable_width,
    ))

    item_count = len(report.lines)
    total_to_purchase = sum(
        (line.get('final_purchase_qty') or 0) for line in report.lines
        if line.get('final_purchase_qty') is not None
    )
    kpi_items = [
        ('Short Items', str(item_count)),
        ('Total Qty to Purchase', f'{total_to_purchase:,.2f}'),
        ('Last Updated', report.updated_at.strftime('%d %b %Y, %H:%M')),
    ]
    elements.append(_build_kpi_bar(kpi_items, styles, usable_width))
    elements.append(Spacer(1, 10))

    if report.stock_api_error:
        elements.append(Paragraph(
            f'<font color="#B91C1C">Some stock figures are unknown -- see "{report.stock_api_error}"</font>',
            styles['label'],
        ))
        elements.append(Spacer(1, 4))

    col_widths = [
        0.80 * inch,   # Item Code
        0.60 * inch,   # Brand
        1.55 * inch,   # Description
        1.05 * inch,   # Customer
        0.85 * inch,   # LPO
        0.75 * inch,   # Sales Order
        0.55 * inch,   # LPO Qty
        0.65 * inch,   # Total Required
        0.65 * inch,   # Available
        0.65 * inch,   # Final Qty
        0.65 * inch,   # LPO Sent
        0.65 * inch,   # Final Purchase
        0.75 * inch,   # Payment Terms
    ]
    allocated = sum(col_widths)
    remainder = max(0, usable_width - allocated)
    col_widths[2] += remainder  # Description gets the slack

    hdr = [
        Paragraph('Item Code', styles['header_cell']),
        Paragraph('Brand', styles['header_cell']),
        Paragraph('Description', styles['header_cell']),
        Paragraph('Customer', styles['header_cell']),
        Paragraph('LPO', styles['header_cell']),
        Paragraph('Sales Order', styles['header_cell']),
        Paragraph('LPO Qty', styles['header_cell_r']),
        Paragraph('Total Req.', styles['header_cell_r']),
        Paragraph('Available', styles['header_cell_r']),
        Paragraph('Final Qty', styles['header_cell_r']),
        Paragraph('LPO Sent', styles['header_cell_r']),
        Paragraph('To Purchase', styles['header_cell_r']),
        Paragraph('Payment Terms', styles['header_cell']),
    ]
    table_data = [hdr]
    for row in rows:
        table_data.append([
            Paragraph(str(row['Item Code']), styles['cell']),
            Paragraph(str(row['Brand']), styles['cell']),
            Paragraph(str(row['Description'])[:60], styles['cell']),
            Paragraph(str(row['Customer'])[:30], styles['cell']),
            Paragraph(str(row['LPO']), styles['cell']),
            Paragraph(str(row['Sales Order']), styles['cell']),
            Paragraph(_num(row['LPO Qty']), styles['cell_r']),
            Paragraph(_num(row['Total Required Qty']), styles['cell_r']),
            Paragraph(_num(row['Available Stock']), styles['cell_r']),
            Paragraph(_num(row['Final Qty (To Procure)']), styles['cell_bold_r']),
            Paragraph(_num(row['LPO Sent to Supplier']), styles['cell_r']),
            Paragraph(_num(row['Final Qty to Purchase']), styles['cell_bold_r']),
            Paragraph(str(row['Payment Terms']), styles['cell']),
        ])

    if not rows:
        table_data.append([Paragraph('No shortages -- every pending order is covered by available stock.', styles['label'])] + [''] * 12)

    data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    data_table.setStyle(_standard_data_table_style(len(table_data), has_total_row=False))
    elements.append(data_table)

    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response
