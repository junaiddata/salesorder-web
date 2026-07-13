"""
Customer Item Year-wise Analysis
==================================
Same item x year data as Item Analysis (saparinvoices/item-analysis/) — Sales
/ GP / GP% / Qty per year (2026, 2025, 2024) — but pivoted customer-first:
rows = customers (collapsed by default; expand reveals the items that
customer bought, broken out the same year-wise way), matching the hierarchy
of Customer Item Month-wise Analysis but with years instead of months.

Net sales/GP/qty = AR Invoice line values + AR Credit Memo line values,
summed directly (credit-memo lines are stored negative, so summing nets
returns against sales) — same convention as Item Analysis, Brandwise Sales
Analysis, and Customer Item Month-wise Analysis.

Performance note: building the year-wise breakdown for every item of every
customer — when only a page of ~15-20 customers is ever displayed — is the
expensive part (same lesson as Customer Item Month-wise Analysis). So this
view computes lightweight customer totals for all customers first (for
sorting/pagination), then lazily builds the item breakdown only for the
customers on the current page (see _build_item_breakdown).
"""
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum, Q, F, Case, When, Value, DecimalField
from django.db.models.functions import Coalesce
from django.shortcuts import render

from .models import SAPARInvoice, SAPARInvoiceItem, SAPARCreditMemo, SAPARCreditMemoItem, Items
from .sap_salesorder_views import salesman_scope_q_salesorder
from .brandwise_sales_analysis_views import _pct, _user_is_admin

YEARS = [2026, 2025, 2024]

# Items shown per customer on-screen / in the PDF — some customers buy
# hundreds of distinct items; cap to the highest-value ones. Item filter/
# search can narrow further.
MAX_ITEMS_PER_CUSTOMER = 40
PDF_MAX_ITEMS_PER_CUSTOMER = 10
PDF_MAX_CUSTOMERS = 150


def _net_value_expr():
    """Per-line net sales value — line_total_after_discount when populated and
    non-zero, else line_total. Matches Item Analysis / Brandwise convention."""
    return Sum(
        Case(
            When(
                Q(line_total_after_discount__isnull=False) & ~Q(line_total_after_discount=0),
                then=F('line_total_after_discount'),
            ),
            default=F('line_total'),
            output_field=DecimalField(),
        )
    )


def _gp_value_expr():
    return Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField()))


def _qty_expr():
    return Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))


def _compute_customer_item_yearwise(request):
    """Shared computation for the HTML page and the PDF export.

    Returns filters, full unpaginated customer_rows (lightweight totals only
    — no item breakdown yet), and the scoped querysets needed to build that
    breakdown lazily.
    """
    is_admin = _user_is_admin(request.user)

    item_search = request.GET.get('q', '').strip()
    selected_salesmen = [s.strip() for s in request.GET.getlist('salesman') if s.strip()]
    selected_firms = list(dict.fromkeys(f.strip() for f in request.GET.getlist('firm') if f and f.strip()))
    customer_search = request.GET.get('customer', '').strip()

    scope_q = salesman_scope_q_salesorder(request.user)
    invoice_qs = SAPARInvoice.objects.filter(scope_q).exclude(salesman_name__iexact='Z.DUTY')
    creditmemo_qs = SAPARCreditMemo.objects.filter(scope_q).exclude(salesman_name__iexact='Z.DUTY')
    if selected_salesmen:
        invoice_qs = invoice_qs.filter(salesman_name__in=selected_salesmen)
        creditmemo_qs = creditmemo_qs.filter(salesman_name__in=selected_salesmen)

    firm_item_codes = None
    if selected_firms:
        firm_item_codes = list(Items.objects.filter(item_firm__in=selected_firms).values_list('item_code', flat=True))

    # customer_code -> {'name','years': {year: {'sales','gp','qty'}}}
    cust_data = defaultdict(lambda: {'name': '', 'years': {y: {
        'sales': Decimal('0'), 'gp': Decimal('0'), 'qty': Decimal('0')} for y in YEARS}})

    for year in YEARS:
        year_inv = invoice_qs.filter(posting_date__year=year)
        year_cm = creditmemo_qs.filter(posting_date__year=year)

        inv_items = (
            SAPARInvoiceItem.objects.filter(invoice__in=year_inv)
            .exclude(invoice__customer_code__isnull=True).exclude(invoice__customer_code='')
        )
        cm_items = (
            SAPARCreditMemoItem.objects.filter(credit_memo__in=year_cm)
            .exclude(credit_memo__customer_code__isnull=True).exclude(credit_memo__customer_code='')
        )
        if firm_item_codes is not None:
            inv_items = inv_items.filter(item_code__in=firm_item_codes)
            cm_items = cm_items.filter(item_code__in=firm_item_codes)
        if item_search:
            search_q = (
                Q(item_code__icontains=item_search) | Q(item_description__icontains=item_search) |
                Q(upc_code__icontains=item_search)
            )
            inv_items = inv_items.filter(search_q)
            cm_items = cm_items.filter(search_q)
        if customer_search:
            inv_items = inv_items.filter(
                Q(invoice__customer_code__icontains=customer_search) |
                Q(invoice__customer_name__icontains=customer_search)
            )
            cm_items = cm_items.filter(
                Q(credit_memo__customer_code__icontains=customer_search) |
                Q(credit_memo__customer_name__icontains=customer_search)
            )

        for r in inv_items.values('invoice__customer_code', 'invoice__customer_name').annotate(
            sales=_net_value_expr(), gp=_gp_value_expr(), qty=_qty_expr()
        ):
            code = (r['invoice__customer_code'] or '').strip()
            if not code:
                continue
            d = cust_data[code]
            if not d['name']:
                d['name'] = r.get('invoice__customer_name') or code
            yd = d['years'][year]
            yd['sales'] += r['sales'] or Decimal('0')
            yd['gp'] += r['gp'] or Decimal('0')
            yd['qty'] += r['qty'] or Decimal('0')

        for r in cm_items.values('credit_memo__customer_code', 'credit_memo__customer_name').annotate(
            sales=_net_value_expr(), gp=_gp_value_expr(), qty=_qty_expr()
        ):
            code = (r['credit_memo__customer_code'] or '').strip()
            if not code:
                continue
            d = cust_data[code]
            if not d['name']:
                d['name'] = r.get('credit_memo__customer_name') or code
            yd = d['years'][year]
            yd['sales'] += r['sales'] or Decimal('0')
            yd['gp'] += r['gp'] or Decimal('0')
            yd['qty'] += r['qty'] or Decimal('0')

    def _year_metrics(yd):
        return {
            'sales': yd['sales'], 'gp': yd['gp'], 'qty': yd['qty'],
            'gp_pct': _pct(yd['gp'], yd['sales']),
        }

    customer_rows = []
    for code, d in cust_data.items():
        year_list = [_year_metrics(d['years'][y]) for y in YEARS]
        total_sales = sum((yd['sales'] for yd in year_list), Decimal('0'))
        total_qty = sum((yd['qty'] for yd in year_list), Decimal('0'))
        # Skip only if there's truly no activity in any year — NOT if the
        # combined total happens to net to zero (e.g. real 2026 sales fully
        # offset by an unrelated 2025 return would otherwise silently vanish
        # from the table and from the grand year totals).
        if all(not yd['sales'] and not yd['qty'] for yd in year_list):
            continue
        customer_rows.append({
            'customer_code': code,
            'customer_name': d['name'],
            'year_list': year_list,
            'total_sales': total_sales,
        })
    customer_rows.sort(key=lambda r: r['total_sales'], reverse=True)

    grand_year_totals = []
    for i, y in enumerate(YEARS):
        s = sum((r['year_list'][i]['sales'] for r in customer_rows), Decimal('0'))
        g = sum((r['year_list'][i]['gp'] for r in customer_rows), Decimal('0'))
        q = sum((r['year_list'][i]['qty'] for r in customer_rows), Decimal('0'))
        grand_year_totals.append({'year': y, 'sales': s, 'gp': g, 'qty': q, 'gp_pct': _pct(g, s)})

    salesmen = list(
        SAPARInvoice.objects.filter(scope_q)
        .exclude(salesman_name__isnull=True).exclude(salesman_name='')
        .exclude(salesman_name__iexact='Z.DUTY')
        .values_list('salesman_name', flat=True).distinct().order_by('salesman_name')
    )
    firms = list(
        Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
        .values_list('item_firm', flat=True).distinct().order_by('item_firm')
    )

    return {
        'years': YEARS,
        'nested_colspan': len(YEARS) * 3 + 2,   # Item + (Sales/Qty/GP%) per year + Total
        'customer_rows': customer_rows,
        'total_customers': len(customer_rows),
        'grand_year_totals': grand_year_totals,
        'is_admin': is_admin,
        'salesmen': salesmen,
        'selected_salesmen': selected_salesmen,
        'firms': firms,
        'selected_firms': selected_firms,
        'item_search': item_search,
        'customer_search': customer_search,
        'invoice_qs': invoice_qs,
        'creditmemo_qs': creditmemo_qs,
    }


def _build_item_breakdown(customer_codes, invoice_qs, creditmemo_qs, item_search,
                           max_items_per_customer=MAX_ITEMS_PER_CUSTOMER):
    """Build the per-item year-wise breakdown for just the given customer
    codes — deferred out of _compute_customer_item_yearwise so a paginated
    HTML page or a capped PDF slice only pays for what it shows.
    Returns {customer_code: {'items': [...], 'item_count', 'truncated', 'hidden_count'}}.
    """
    if not customer_codes:
        return {}

    # customer_code -> item_code -> {'description', 'years': {year: {'sales','gp','qty'}}}
    raw = defaultdict(lambda: defaultdict(lambda: {'description': '', 'years': {y: {
        'sales': Decimal('0'), 'gp': Decimal('0'), 'qty': Decimal('0')} for y in YEARS}}))

    for year in YEARS:
        year_inv = invoice_qs.filter(posting_date__year=year)
        year_cm = creditmemo_qs.filter(posting_date__year=year)

        inv_items = (
            SAPARInvoiceItem.objects.filter(invoice__in=year_inv, invoice__customer_code__in=customer_codes)
            .exclude(item_code__isnull=True).exclude(item_code='')
        )
        cm_items = (
            SAPARCreditMemoItem.objects.filter(credit_memo__in=year_cm, credit_memo__customer_code__in=customer_codes)
            .exclude(item_code__isnull=True).exclude(item_code='')
        )
        if item_search:
            search_q = (
                Q(item_code__icontains=item_search) | Q(item_description__icontains=item_search) |
                Q(upc_code__icontains=item_search)
            )
            inv_items = inv_items.filter(search_q)
            cm_items = cm_items.filter(search_q)

        for r in inv_items.values('invoice__customer_code', 'item_code', 'item_description').annotate(
            sales=_net_value_expr(), gp=_gp_value_expr(), qty=_qty_expr()
        ):
            cust_code = r['invoice__customer_code']
            bucket = raw[cust_code][r['item_code']]
            if not bucket['description']:
                bucket['description'] = r.get('item_description') or ''
            yd = bucket['years'][year]
            yd['sales'] += r['sales'] or Decimal('0')
            yd['gp'] += r['gp'] or Decimal('0')
            yd['qty'] += r['qty'] or Decimal('0')

        for r in cm_items.values('credit_memo__customer_code', 'item_code', 'item_description').annotate(
            sales=_net_value_expr(), gp=_gp_value_expr(), qty=_qty_expr()
        ):
            cust_code = r['credit_memo__customer_code']
            bucket = raw[cust_code][r['item_code']]
            if not bucket['description']:
                bucket['description'] = r.get('item_description') or ''
            yd = bucket['years'][year]
            yd['sales'] += r['sales'] or Decimal('0')
            yd['gp'] += r['gp'] or Decimal('0')
            yd['qty'] += r['qty'] or Decimal('0')

    def _year_metrics(yd):
        return {
            'sales': yd['sales'], 'gp': yd['gp'], 'qty': yd['qty'],
            'gp_pct': _pct(yd['gp'], yd['sales']),
        }

    details = {}
    for cust_code in customer_codes:
        item_bucket = raw.get(cust_code, {})
        rows = []
        for item_code, d in item_bucket.items():
            year_list = [_year_metrics(d['years'][y]) for y in YEARS]
            total_sales = sum((yd['sales'] for yd in year_list), Decimal('0'))
            total_qty = sum((yd['qty'] for yd in year_list), Decimal('0'))
            # Skip only if there's truly no activity in any year (see the
            # matching comment in _compute_customer_item_yearwise).
            if all(not yd['sales'] and not yd['qty'] for yd in year_list):
                continue
            rows.append({
                'item_code': item_code,
                'description': d['description'],
                'year_list': year_list,
                'total_sales': total_sales,
            })
        rows.sort(key=lambda r: r['total_sales'], reverse=True)
        full_count = len(rows)
        shown = rows[:max_items_per_customer]
        details[cust_code] = {
            'items': shown,
            'item_count': full_count,
            'truncated': full_count > len(shown),
            'hidden_count': full_count - len(shown),
        }
    return details


@login_required
def customer_item_yearwise_analysis(request):
    ctx = _compute_customer_item_yearwise(request)

    page_size = 15
    paginator = Paginator(ctx['customer_rows'], page_size)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    page_codes = [r['customer_code'] for r in page_obj]
    details = _build_item_breakdown(
        page_codes, ctx['invoice_qs'], ctx['creditmemo_qs'], ctx['item_search'],
    )
    for cust in page_obj:
        cust.update(details.get(cust['customer_code'], {'items': [], 'item_count': 0,
                                                          'truncated': False, 'hidden_count': 0}))

    ctx['customers'] = page_obj
    ctx['page_obj'] = page_obj
    del ctx['invoice_qs']
    del ctx['creditmemo_qs']

    return render(request, 'salesorders/customer_item_yearwise_analysis.html', ctx)


@login_required
def export_customer_item_yearwise_analysis_pdf(request):
    """
    Export the customer x year matrix, with each customer's item breakdown
    nested beneath it, to PDF — same design system as Customer Item
    Month-wise Analysis' PDF export (navy document header, KPI bar,
    zebra-striped bordered table, thick divider between years, dark header
    text).
    """
    from datetime import datetime
    from io import BytesIO

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.lib.colors import HexColor

    from django.http import HttpResponse

    from .finance_statement_pdf_export import (
        _build_document_header, _build_kpi_bar, _build_styles, _fmt,
        CLR_TEXT_MUTED, CLR_BORDER, CLR_PRIMARY, CLR_TEXT,
    )
    from .item_quoted_analysis_pdf_export import _build_analysis_styles, _build_analysis_table_style

    ctx = _compute_customer_item_yearwise(request)
    customer_rows = ctx['customer_rows']
    is_admin = ctx['is_admin']

    page_w, page_h = landscape(A4)
    margin_h, margin_v = 22, 22
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        rightMargin=margin_h, leftMargin=margin_h,
        topMargin=margin_v, bottomMargin=margin_v + 4,
    )
    usable_width = page_w - 2 * margin_h
    page_styles = _build_styles()
    ts = _build_analysis_styles()
    elements = []

    def _name_list_label(names, noun):
        shown = ', '.join(names[:3])
        if len(names) > 3:
            shown += f' (+{len(names) - 3} more)'
        return f"{noun}: {shown}"

    filter_parts = []
    if ctx['selected_salesmen']:
        filter_parts.append(_name_list_label(ctx['selected_salesmen'], 'Salesman'))
    if ctx['selected_firms']:
        filter_parts.append(_name_list_label(ctx['selected_firms'], 'Firm'))
    if ctx['customer_search']:
        filter_parts.append(f"Customer: “{ctx['customer_search']}”")
    if ctx['item_search']:
        filter_parts.append(f"Item: “{ctx['item_search']}”")
    subtitle = f"{YEARS[-1]}–{YEARS[0]}" + (' — ' + ' · '.join(filter_parts) if filter_parts else ' — All customers')

    elements.extend(_build_document_header(
        page_styles,
        title_text='CUSTOMER ITEM YEAR-WISE ANALYSIS',
        subtitle_text=subtitle,
        page_width=usable_width,
    ))

    grand = ctx['grand_year_totals']
    total_sales = sum((g['sales'] for g in grand), Decimal('0'))
    total_qty = sum((g['qty'] for g in grand), Decimal('0'))
    total_gp = sum((g['gp'] for g in grand), Decimal('0'))
    kpi_items = [
        ('Total Sales', _fmt(total_sales)),
        ('Total Qty', _fmt(total_qty)),
        ('Customers', str(ctx['total_customers'])),
    ]
    if is_admin:
        kpi_items.append(('GP %', f"{_pct(total_gp, total_sales):.1f}%"))
    kpi_items.append(('Years', f"{YEARS[-1]}–{YEARS[0]}"))

    elements.append(_build_kpi_bar(kpi_items, page_styles, usable_width))
    elements.append(Spacer(1, 10))

    response = HttpResponse(content_type='application/pdf')
    fname = f"customer_item_yearwise_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{fname}"'

    def _finish():
        def _footer(canvas, doc_):
            canvas.saveState()
            canvas.setStrokeColor(CLR_BORDER)
            canvas.setLineWidth(0.5)
            canvas.line(doc_.leftMargin, 16, page_w - doc_.rightMargin, 16)
            canvas.setFont('Helvetica', 6)
            canvas.setFillColor(CLR_TEXT_MUTED)
            canvas.drawCentredString(
                page_w / 2, 7,
                f"Page {doc_.page}  •  Customer Item Year-wise Analysis  •  "
                f"Generated {datetime.now().strftime('%d %b %Y %H:%M')}  •  Confidential"
            )
            canvas.restoreState()
        doc.build(elements, onFirstPage=_footer, onLaterPages=_footer)
        response.write(buffer.getvalue())
        return response

    if not customer_rows:
        elements.append(Paragraph(
            '<font color="#6B7280">No customers found for the selected filters.</font>',
            page_styles['label'],
        ))
        return _finish()

    base = getSampleStyleSheet()['Normal']
    th_dark = ParagraphStyle('ThDark', parent=base, fontName='Helvetica-Bold', fontSize=6.5,
                              textColor=CLR_TEXT, leading=8)
    th_dark_c = ParagraphStyle('ThDarkC', parent=base, fontName='Helvetica-Bold', fontSize=6.5,
                                textColor=CLR_TEXT, leading=8, alignment=TA_CENTER)
    yr_td = ParagraphStyle('YrTd', parent=base, fontName='Helvetica', fontSize=6.2,
                            textColor=CLR_TEXT, leading=7.4, alignment=TA_RIGHT)
    yr_td_faint = ParagraphStyle('YrTdFaint', parent=base, fontName='Helvetica', fontSize=6.2,
                                  textColor=HexColor('#B0B7C0'), leading=7.4, alignment=TA_RIGHT)
    metric_lbl = ParagraphStyle('MetricLbl', parent=base, fontName='Helvetica-Oblique', fontSize=5.8,
                                 textColor=CLR_TEXT_MUTED, leading=7, alignment=TA_LEFT)
    total_cell = ParagraphStyle('TotalCell', parent=base, fontName='Helvetica-Bold', fontSize=6.2,
                                 textColor=CLR_PRIMARY, leading=7.4, alignment=TA_RIGHT)

    # Columns: # | Code | Metric | Name | [Sales, Qty, (GP%) per year] | Total Sales
    metrics_per_year = 3 if is_admin else 2   # Sales+Qty always; GP% admin-only
    W_NUM, W_CODE, W_METRIC, W_TOTAL = 16, 46, 30, 55
    fixed_total = W_NUM + W_CODE + W_METRIC + W_TOTAL
    n_year_cols = len(YEARS) * metrics_per_year
    year_col_w = max(30, (usable_width - fixed_total - 160) / n_year_cols)
    W_NAME = usable_width - fixed_total - (year_col_w * n_year_cols)

    col_widths = [W_NUM, W_CODE, W_METRIC, W_NAME] + [year_col_w] * n_year_cols + [W_TOTAL]

    hdr = [
        Paragraph('#', th_dark_c), Paragraph('Code', th_dark), Paragraph('', th_dark),
        Paragraph('Customer / Item', th_dark),
    ]
    for yr in YEARS:
        for _ in range(metrics_per_year):
            hdr.append(Paragraph(str(yr), th_dark_c))
    hdr.append(Paragraph('Total Sales', th_dark_c))
    table_data = [hdr]
    group_end_cols = set()
    col = 0
    for i in range(len(YEARS)):
        col += metrics_per_year
        group_end_cols.add(col - 1)

    def _row_cells(year_list):
        cells = []
        for yd in year_list:
            cells.append(Paragraph(f"{yd['sales']:,.0f}" if yd['sales'] else '–', yr_td if yd['sales'] else yr_td_faint))
            cells.append(Paragraph(f"{yd['qty']:,.0f}" if yd['qty'] else '–', yr_td if yd['qty'] else yr_td_faint))
            if is_admin:
                cells.append(Paragraph(f"{yd['gp_pct']:.1f}%" if yd['sales'] else '–', yr_td if yd['sales'] else yr_td_faint))
        return cells

    sub_hdr = [Paragraph('', th_dark), Paragraph('', th_dark), Paragraph('', th_dark), Paragraph('', th_dark)]
    for _ in YEARS:
        sub_hdr.append(Paragraph('Sales', metric_lbl))
        sub_hdr.append(Paragraph('Qty', metric_lbl))
        if is_admin:
            sub_hdr.append(Paragraph('GP%', metric_lbl))
    sub_hdr.append(Paragraph('', th_dark))
    table_data.append(sub_hdr)
    item_row_indices = set()

    # Only build the expensive per-item breakdown for customers that actually
    # make it into the (capped) PDF — not the full filtered set.
    pdf_customers = customer_rows[:PDF_MAX_CUSTOMERS]
    pdf_details = _build_item_breakdown(
        [r['customer_code'] for r in pdf_customers], ctx['invoice_qs'], ctx['creditmemo_qs'], ctx['item_search'],
        max_items_per_customer=PDF_MAX_ITEMS_PER_CUSTOMER,
    )

    for idx, cust in enumerate(pdf_customers, start=1):
        detail = pdf_details.get(cust['customer_code'], {'items': [], 'hidden_count': 0})
        name = cust['customer_name'] or '—'
        row = [
            Paragraph(str(idx), ts['td_c']),
            Paragraph(cust['customer_code'] or '—', ts['td']),
            Paragraph('', ts['td']),
            Paragraph(f"{name} ({detail.get('item_count', 0)} items)", ts['td_bold']),
        ]
        row += _row_cells(cust['year_list'])
        row.append(Paragraph(_fmt(cust['total_sales']), total_cell))
        table_data.append(row)

        for item in detail['items']:
            irow = [
                Paragraph('', ts['td']), Paragraph(item['item_code'] or '—', ts['td_muted']),
                Paragraph('', ts['td']), Paragraph(item['description'] or '—', ts['td_muted']),
            ]
            irow += _row_cells(item['year_list'])
            irow.append(Paragraph(_fmt(item['total_sales']), total_cell))
            item_row_indices.add(len(table_data))
            table_data.append(irow)

        if detail['hidden_count'] > 0:
            more_row = [
                Paragraph('', ts['td']), Paragraph('', ts['td']), Paragraph('', ts['td']),
                Paragraph(f"… +{detail['hidden_count']} more item(s) not shown", ts['td_muted']),
            ]
            more_row += [Paragraph('', yr_td)] * n_year_cols
            more_row.append(Paragraph('', yr_td))
            item_row_indices.add(len(table_data))
            table_data.append(more_row)

    total_row = [
        Paragraph('', ts['td']), Paragraph('TOTAL', ts['td_bold']), Paragraph('', ts['td']),
        Paragraph(f"{len(customer_rows)} customers", ts['total_label']),
    ]
    total_row += _row_cells(grand)
    total_row.append(Paragraph(_fmt(total_sales), total_cell))
    table_data.append(total_row)

    data_table = Table(table_data, colWidths=col_widths, repeatRows=2)
    table_style = _build_analysis_table_style(num_rows=len(table_data), customer_row_indices=item_row_indices)
    last_col = len(col_widths) - 1
    table_style.add('ALIGN', (4, 0), (last_col, -1), 'RIGHT')
    table_style.add('ALIGN', (0, 0), (0, -1), 'CENTER')
    table_style.add('LEFTPADDING', (0, 0), (-1, -1), 2)
    table_style.add('RIGHTPADDING', (0, 0), (-1, -1), 2)
    year_start = 4
    for end_idx in sorted(group_end_cols):
        col_idx = year_start + end_idx
        table_style.add('LINEAFTER', (col_idx, 0), (col_idx, -1), 1.2, CLR_TEXT_MUTED)
    data_table.setStyle(table_style)
    elements.append(data_table)

    if len(customer_rows) > PDF_MAX_CUSTOMERS:
        elements.append(Spacer(1, 0.1 * inch))
        elements.append(Paragraph(
            f'<font color="#6B7280">(Showing top {PDF_MAX_CUSTOMERS} of {len(customer_rows)} customers by sales value — '
            f'use the Salesman/Firm/Customer filters on the report page to export a specific slice in full.)</font>',
            page_styles['label'],
        ))

    return _finish()
