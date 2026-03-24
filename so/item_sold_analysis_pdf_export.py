"""
Item Sold Analysis PDF Export
Firm-wise analysis: Qty Sold 2025/2026, Customer count per item.
Same structure as Item Quoted Analysis PDF but for sold data from invoices/credit memos.
"""
from io import BytesIO
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

from . import item_sold_analysis_views


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
    if not firm_list:
        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="item_sold_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
        )
        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=36, rightMargin=36)
        styles = getSampleStyleSheet()
        doc.build([Paragraph('No firm selected. Use the report page to select firms, then export.', styles['Normal'])])
        response.write(buf.getvalue())
        return response

    # Build full items list (no pagination) - reuse view logic via internal helper
    items_list, grand_total_2025, grand_total_2026, grand_total_customers = (
        item_sold_analysis_views._build_items_list_for_pdf(request, firm_list, search_term, include_customers)
    )

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="item_sold_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.5 * inch,
    )

    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle(
        'Title', parent=styles['Heading1'],
        fontSize=16, textColor=colors.HexColor('#1e293b'),
        spaceAfter=8, alignment=TA_CENTER,
    )
    elements.append(Paragraph("Item Sold Analysis", title_style))
    elements.append(Paragraph(
        f"Firms: {', '.join(firm_list[:3])}{'...' if len(firm_list) > 3 else ''} — Qty Sold 2025 & 2026",
        ParagraphStyle('Sub', parent=styles['Normal'], fontSize=9, alignment=TA_CENTER, textColor=colors.grey),
    ))
    elements.append(Spacer(1, 0.2 * inch))

    if not items_list:
        elements.append(Paragraph("No items found for the selected firm(s).", styles['Normal']))
        doc.build(elements)
        response.write(buffer.getvalue())
        return response

    # Build table
    hdr = ['#', 'Item Code', 'Description', 'UPC', 'Stock', 'Imp+LPO', 'Qty 25', 'Qty 26', 'Amt 25', 'Amt 26', 'Invs 25', 'Invs 26', 'Cust.']
    data = [hdr]
    for i, item in enumerate(items_list[:500], 1):  # Limit 500 rows
        row = [
            str(i),
            (item['item_code'] or '')[:20],
            (item['item_description'] or '')[:35],
            (item['upc_code'] or '')[:12],
            str(int(item.get('total_stock', 0) or 0)),
            str(int(item.get('import_ordered', 0) or 0)),
            str(int(item.get('qty_sold_2025', 0) or 0)),
            str(int(item.get('qty_sold_2026', 0) or 0)),
            f"{(item.get('total_amount_2025') or 0):,.0f}",
            f"{(item.get('total_amount_2026') or 0):,.0f}",
            str(item.get('total_invoices_2025', 0) or 0),
            str(item.get('total_invoices_2026', 0) or 0),
            str(item.get('customer_sold_count', 0) or 0),
        ]
        data.append(row)

    # Totals row (grand totals may be Decimal)
    gt25 = grand_total_2025
    gt26 = grand_total_2026
    if hasattr(gt25, '__float__'):
        gt25 = float(gt25)
    if hasattr(gt26, '__float__'):
        gt26 = float(gt26)
    data.append([
        '', '', 'TOTAL', '', '-', '-',
        str(int(gt25 or 0)),
        str(int(gt26 or 0)),
        '-', '-', '-', '-',
        str(grand_total_customers or 0),
    ])

    t = Table(data, colWidths=[18, 55, 100, 45, 35, 40, 40, 40, 55, 55, 35, 35, 30])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('ALIGN', (0, 0), (0, -1), TA_CENTER),
        ('ALIGN', (5, 0), (-1, -1), TA_RIGHT),
        ('ALIGN', (1, 0), (4, -1), TA_LEFT),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f1f5f9')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(t)
    if len(items_list) > 500:
        elements.append(Spacer(1, 0.1 * inch))
        elements.append(Paragraph(
            f"(Showing first 500 of {len(items_list)} items)",
            ParagraphStyle('Note', parent=styles['Normal'], fontSize=8, textColor=colors.grey),
        ))

    doc.build(elements)
    response.write(buffer.getvalue())
    return response
