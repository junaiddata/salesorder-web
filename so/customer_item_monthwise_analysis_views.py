"""
Customer-wise Item Sold — Month-wise Analysis
==============================================
Pivot table:
  - Rows    = customers (collapsed by default; expand to reveal their items)
  - Nested  = items bought by that customer, each with 3 sub-rows: Qty / Avg Rate / GP%
  - Columns = months from Jan 2025 through the current month
  - Filters = Salesman (multi-select), Customer search, Item search

Net qty/sales/GP = AR Invoice line values + AR Credit Memo line values, summed
directly (credit-memo lines are stored negative, so summing nets returns
against sales) — same convention as Brandwise Sales Analysis.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum, Q
from django.shortcuts import render

from .models import SAPARInvoice, SAPARInvoiceItem, SAPARCreditMemo, SAPARCreditMemoItem, Items
from .sap_salesorder_views import salesman_scope_q_salesorder
from .brandwise_sales_analysis_views import (
    _net_value_expr, _gp_value_expr, _pct, _user_is_admin, MONTH_NAMES_SHORT,
)

# Some customers carry hundreds of distinct items — an unbounded item x month x
# metric pivot per customer would balloon page size into the tens of MB. Cap the
# items shown per customer to the highest-value ones; the Item filter can be used
# to search across all of a customer's items.
MAX_ITEMS_PER_CUSTOMER = 40


def _month_columns():
    """Month columns paired by month name across years: Jan-25, Jan-26, Feb-25,
    Feb-26, ... through Dec-25 (2026 months beyond the current month are omitted
    since they have no data yet)."""
    today = date.today()
    valid_year_months = set()
    y, m = 2025, 1
    while (y, m) <= (today.year, today.month):
        valid_year_months.add((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    years = sorted({yr for yr, _ in valid_year_months})

    months = []
    for month_num in range(1, 13):
        for yr in years:
            if (yr, month_num) in valid_year_months:
                months.append({
                    'year': yr, 'month': month_num,
                    'label': f"{MONTH_NAMES_SHORT[month_num - 1]}-{str(yr)[2:]}",
                })
    return months


def _compute_customer_item_monthwise(request):
    """Shared computation for the HTML page and the PDF export.

    Returns the full context dict (filters, full unpaginated customer_rows,
    months/years, and the option lists for the filter widgets).
    """
    is_admin = _user_is_admin(request.user)
    months = _month_columns()
    n_months = len(months)
    month_index = {(m['year'], m['month']): i for i, m in enumerate(months)}
    years = sorted({m['year'] for m in months})
    year_col_indices = {yr: [i for i, m in enumerate(months) if m['year'] == yr] for yr in years}

    selected_salesmen = [s.strip() for s in request.GET.getlist('salesman') if s.strip()]
    selected_firms = list(dict.fromkeys(f.strip() for f in request.GET.getlist('firm') if f and f.strip()))
    customer_search = request.GET.get('customer', '').strip()
    item_search = request.GET.get('item', '').strip()

    start_date = date(2025, 1, 1)
    end_date = date.today()

    scope_q = salesman_scope_q_salesorder(request.user)
    inv_headers = (
        SAPARInvoice.objects.filter(scope_q)
        .exclude(salesman_name__iexact='Z.DUTY')
        .filter(posting_date__gte=start_date, posting_date__lte=end_date)
    )
    cm_headers = (
        SAPARCreditMemo.objects.filter(scope_q)
        .exclude(salesman_name__iexact='Z.DUTY')
        .filter(posting_date__gte=start_date, posting_date__lte=end_date)
    )
    if selected_salesmen:
        inv_headers = inv_headers.filter(salesman_name__in=selected_salesmen)
        cm_headers = cm_headers.filter(salesman_name__in=selected_salesmen)

    inv_items = (
        SAPARInvoiceItem.objects.filter(invoice__in=inv_headers)
        .exclude(invoice__customer_code__isnull=True).exclude(invoice__customer_code='')
        .exclude(item_code__isnull=True).exclude(item_code='')
    )
    cm_items = (
        SAPARCreditMemoItem.objects.filter(credit_memo__in=cm_headers)
        .exclude(credit_memo__customer_code__isnull=True).exclude(credit_memo__customer_code='')
        .exclude(item_code__isnull=True).exclude(item_code='')
    )
    if customer_search:
        inv_items = inv_items.filter(
            Q(invoice__customer_code__icontains=customer_search) |
            Q(invoice__customer_name__icontains=customer_search)
        )
        cm_items = cm_items.filter(
            Q(credit_memo__customer_code__icontains=customer_search) |
            Q(credit_memo__customer_name__icontains=customer_search)
        )
    if item_search:
        inv_items = inv_items.filter(
            Q(item_code__icontains=item_search) | Q(item_description__icontains=item_search)
        )
        cm_items = cm_items.filter(
            Q(item_code__icontains=item_search) | Q(item_description__icontains=item_search)
        )
    if selected_firms:
        inv_items = inv_items.filter(item__item_firm__in=selected_firms)
        cm_items = cm_items.filter(item__item_firm__in=selected_firms)

    # customer_code -> {'name': str, 'items': {item_code: {'description', 'qty':[], 'amt':[], 'gp':[]}}}
    customers = defaultdict(lambda: {'name': '', 'items': {}})

    def _item_bucket(cust, item_code, description):
        items = cust['items']
        bucket = items.get(item_code)
        if bucket is None:
            bucket = {
                'description': description or '',
                'qty': [Decimal('0')] * n_months,
                'amt': [Decimal('0')] * n_months,
                'gp': [Decimal('0')] * n_months,
            }
            items[item_code] = bucket
        elif description and not bucket['description']:
            bucket['description'] = description
        return bucket

    def _accumulate(rows, code_key, name_key, year_key, month_key):
        for r in rows:
            code = (r[code_key] or '').strip()
            if not code:
                continue
            idx = month_index.get((r[year_key], r[month_key]))
            if idx is None:
                continue
            cust = customers[code]
            if not cust['name']:
                cust['name'] = r.get(name_key) or code
            bucket = _item_bucket(cust, r['item_code'], r.get('item_description'))
            bucket['qty'][idx] += r['qty'] or Decimal('0')
            bucket['amt'][idx] += r['sales'] or Decimal('0')
            bucket['gp'][idx] += r['gp'] or Decimal('0')

    inv_rows = (
        inv_items
        .values('invoice__customer_code', 'invoice__customer_name', 'item_code', 'item_description',
                 'invoice__posting_date__year', 'invoice__posting_date__month')
        .annotate(qty=Sum('quantity'), sales=_net_value_expr(), gp=_gp_value_expr())
    )
    _accumulate(inv_rows, 'invoice__customer_code', 'invoice__customer_name',
                'invoice__posting_date__year', 'invoice__posting_date__month')

    cm_rows = (
        cm_items
        .values('credit_memo__customer_code', 'credit_memo__customer_name', 'item_code', 'item_description',
                 'credit_memo__posting_date__year', 'credit_memo__posting_date__month')
        .annotate(qty=Sum('quantity'), sales=_net_value_expr(), gp=_gp_value_expr())
    )
    _accumulate(cm_rows, 'credit_memo__customer_code', 'credit_memo__customer_name',
                'credit_memo__posting_date__year', 'credit_memo__posting_date__month')

    # ── Build display rows ───────────────────────────────────────
    customer_rows = []
    for code, cust in customers.items():
        item_rows = []
        cust_total_amt = Decimal('0')
        cust_total_qty = Decimal('0')
        cust_total_gp = Decimal('0')
        for item_code, d in cust['items'].items():
            total_qty = sum(d['qty'], Decimal('0'))
            total_amt = sum(d['amt'], Decimal('0'))
            total_gp = sum(d['gp'], Decimal('0'))
            if not total_qty and not total_amt:
                continue
            cells = []
            for i in range(n_months):
                q, a, g = d['qty'][i], d['amt'][i], d['gp'][i]
                cells.append({
                    'qty': q,
                    'rate': (a / q) if q else Decimal('0'),
                    'avg_gp': (g / q) if q else Decimal('0'),
                    'gp_pct': _pct(g, a),
                })
            # Per-year totals (2025 and 2026 kept separate, not blended together) —
            # each year's avg rate/avg GP/GP% is computed from that year's own qty/amt/gp sums.
            year_totals = []
            for yr in years:
                idxs = year_col_indices[yr]
                yr_qty = sum((d['qty'][i] for i in idxs), Decimal('0'))
                yr_amt = sum((d['amt'][i] for i in idxs), Decimal('0'))
                yr_gp = sum((d['gp'][i] for i in idxs), Decimal('0'))
                year_totals.append({
                    'year': yr,
                    'qty': yr_qty,
                    'rate': (yr_amt / yr_qty) if yr_qty else Decimal('0'),
                    'avg_gp': (yr_gp / yr_qty) if yr_qty else Decimal('0'),
                    'gp_pct': _pct(yr_gp, yr_amt),
                })
            item_rows.append({
                'item_code': item_code,
                'description': d['description'],
                'cells': cells,
                'total_qty': total_qty,
                'total_amt': total_amt,
                'total_gp': total_gp,
                'year_totals': year_totals,
            })
            cust_total_amt += total_amt
            cust_total_qty += total_qty
            cust_total_gp += total_gp
        if not item_rows:
            continue
        item_rows.sort(key=lambda r: r['total_amt'], reverse=True)
        full_item_count = len(item_rows)
        shown_items = item_rows[:MAX_ITEMS_PER_CUSTOMER]
        customer_rows.append({
            'customer_code': code,
            'customer_name': cust['name'],
            'items': shown_items,
            'item_count': full_item_count,
            'items_truncated': full_item_count > len(shown_items),
            'hidden_item_count': full_item_count - len(shown_items),
            'total_qty': cust_total_qty,
            'total_amt': cust_total_amt,
            'gp_pct': _pct(cust_total_gp, cust_total_amt),
        })
    customer_rows.sort(key=lambda r: r['total_amt'], reverse=True)

    grand_total_amt = sum((r['total_amt'] for r in customer_rows), Decimal('0'))
    grand_total_qty = sum((r['total_qty'] for r in customer_rows), Decimal('0'))

    # ── Salesman list for the filter dropdown (scoped) ────────────
    salesmen = list(
        SAPARInvoice.objects.filter(scope_q)
        .exclude(salesman_name__isnull=True).exclude(salesman_name='')
        .exclude(salesman_name__iexact='Z.DUTY')
        .values_list('salesman_name', flat=True).distinct().order_by('salesman_name')
    )

    # ── Firm list for the filter dropdown (all firms in Items) ────
    firms = list(
        Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='')
        .values_list('item_firm', flat=True).distinct().order_by('item_firm')
    )

    return {
        'months': months,
        'years': years,
        'customer_rows': customer_rows,
        'total_customers': len(customer_rows),
        'grand_total_amt': grand_total_amt,
        'grand_total_qty': grand_total_qty,
        'is_admin': is_admin,
        'salesmen': salesmen,
        'selected_salesmen': selected_salesmen,
        'firms': firms,
        'selected_firms': selected_firms,
        'customer_search': customer_search,
        'item_search': item_search,
        'period_label': f"Jan 2025 – {MONTH_NAMES_SHORT[end_date.month - 1]} {end_date.year}",
    }


@login_required
def customer_item_monthwise_analysis(request):
    ctx = _compute_customer_item_monthwise(request)

    page_size = 15
    paginator = Paginator(ctx['customer_rows'], page_size)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    ctx['customers'] = page_obj
    ctx['page_obj'] = page_obj

    return render(request, 'salesorders/customer_item_monthwise_analysis.html', ctx)


@login_required
def export_customer_item_monthwise_analysis_pdf(request):
    """
    Export a customer-level summary (Customer / Total Qty / Total Sales / GP% /
    Items) to PDF — same design system as Item Sold Analysis' PDF export
    (navy document header, KPI bar, zebra-striped bordered table, totals row).
    The full item x month pivot isn't exported (60+ columns per item across
    both years wouldn't fit any page); this mirrors what's visible collapsed
    on the HTML page, across all customers rather than just the current page.
    """
    from datetime import datetime
    from io import BytesIO

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.colors import HexColor

    from django.http import HttpResponse

    from .finance_statement_pdf_export import (
        _build_document_header, _build_kpi_bar, _build_styles, _fmt,
        CLR_TEXT_MUTED, CLR_BORDER, CLR_PRIMARY, CLR_TEXT,
    )
    from .item_quoted_analysis_pdf_export import _build_analysis_styles, _build_analysis_table_style

    # Items shown per customer in the PDF — smaller than the on-screen cap
    # (MAX_ITEMS_PER_CUSTOMER) since every extra item now costs 2-3 printed rows
    # (Qty/Rate/GP% per item), not one.
    PDF_MAX_ITEMS_PER_CUSTOMER = 10
    # Customers shown in the PDF. With month-wise item detail this report can run
    # to hundreds of pages and tens of seconds to build for the full unfiltered
    # customer list — cap it and point the user at the Salesman/Firm filters to
    # get a complete, fast export of the slice they actually need.
    PDF_MAX_CUSTOMERS = 150

    ctx = _compute_customer_item_monthwise(request)
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
    subtitle = ctx['period_label'] + (' — ' + ' · '.join(filter_parts) if filter_parts else ' — All customers')

    elements.extend(_build_document_header(
        page_styles,
        title_text='CUSTOMER ITEM MONTH-WISE ANALYSIS',
        subtitle_text=subtitle,
        page_width=usable_width,
    ))

    kpi_items = [
        ('Total Sales', _fmt(ctx['grand_total_amt'])),
        ('Total Qty', _fmt(ctx['grand_total_qty'])),
        ('Customers', str(ctx['total_customers'])),
    ]
    if is_admin and customer_rows:
        overall_gp_pct = sum(r['gp_pct'] * float(r['total_amt']) for r in customer_rows)
        overall_gp_pct = (overall_gp_pct / float(ctx['grand_total_amt'])) if ctx['grand_total_amt'] else 0
        kpi_items.append(('GP %', f"{overall_gp_pct:.1f}%"))
    kpi_items.append(('Period', ctx['period_label']))

    elements.append(_build_kpi_bar(kpi_items, page_styles, usable_width))
    elements.append(Spacer(1, 10))

    response = HttpResponse(content_type='application/pdf')
    fname = f"customer_item_monthwise_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
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
                f"Page {doc_.page}  •  Customer Item Month-wise Analysis  •  "
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

    months = ctx['months']
    n_months = len(months)

    # Compact styles for the month grid — smaller than the standard analysis
    # styles since up to 19 month columns have to share the page width.
    base = getSampleStyleSheet()['Normal']
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT
    mth_hdr = ParagraphStyle('MthHdr', parent=base, fontName='Helvetica-Bold', fontSize=5.2,
                              textColor=CLR_TEXT, leading=6.5, alignment=TA_CENTER)
    # Header cells hold Paragraph flowables, and ReportLab's TableStyle
    # TEXTCOLOR command has no effect on Paragraph content (only plain
    # strings) — so the header text color must be set on the style itself,
    # not via a table-wide override, or it silently stays whatever the base
    # style says (white, invisible against this codebase's light header bg).
    th_dark = ParagraphStyle('ThDark', parent=base, fontName='Helvetica-Bold', fontSize=6.5,
                              textColor=CLR_TEXT, leading=8)
    th_dark_c = ParagraphStyle('ThDarkC', parent=base, fontName='Helvetica-Bold', fontSize=6.5,
                                textColor=CLR_TEXT, leading=8, alignment=TA_CENTER)
    mth_td = ParagraphStyle('MthTd', parent=base, fontName='Helvetica', fontSize=5.4,
                             textColor=CLR_TEXT, leading=6.6, alignment=TA_RIGHT)
    mth_td_faint = ParagraphStyle('MthTdFaint', parent=base, fontName='Helvetica', fontSize=5.4,
                                   textColor=HexColor('#B0B7C0'), leading=6.6, alignment=TA_RIGHT)
    metric_lbl = ParagraphStyle('MetricLbl', parent=base, fontName='Helvetica-Oblique', fontSize=5.8,
                                 textColor=CLR_TEXT_MUTED, leading=7, alignment=TA_LEFT)
    total_cell = ParagraphStyle('TotalCell', parent=base, fontName='Helvetica-Bold', fontSize=6,
                                 textColor=CLR_PRIMARY, leading=7.2, alignment=TA_RIGHT)

    W_NUM, W_CODE, W_METRIC, W_TOTAL = 15, 40, 26, 42
    fixed_total = W_NUM + W_CODE + W_METRIC + W_TOTAL
    month_w = max(17, (usable_width - fixed_total - 150) / max(n_months, 1))
    W_NAME = usable_width - fixed_total - (month_w * n_months)

    col_widths = [W_NUM, W_CODE, W_METRIC, W_NAME] + [month_w] * n_months + [W_TOTAL]

    hdr = [
        Paragraph('#', th_dark_c),
        Paragraph('Code', th_dark),
        Paragraph('', th_dark),
        Paragraph('Customer / Item', th_dark),
    ]
    hdr += [Paragraph(m['label'], mth_hdr) for m in months]
    hdr.append(Paragraph('Total', th_dark_c))
    table_data = [hdr]
    item_sub_row_indices = set()   # rows tinted as "item" detail rows (vs. customer rows)

    def _num(v, style=mth_td, dp=0):
        if not v:
            return Paragraph('–', mth_td_faint)
        return Paragraph(f"{v:,.{dp}f}", style)

    def _item_rows(item):
        rows = []
        code_desc = item['item_code']
        if item['description']:
            code_desc += f" — {item['description']}"
        total_qty = item['total_qty']
        total_amt = item['total_amt']
        total_gp = item['total_gp']
        avg_rate = (total_amt / total_qty) if total_qty else Decimal('0')
        gp_pct = _pct(total_gp, total_amt)

        qty_row = [Paragraph('', ts['td']), Paragraph('', ts['td']),
                   Paragraph('Qty', metric_lbl), Paragraph(code_desc, ts['td_muted'])]
        qty_row += [_num(c['qty']) for c in item['cells']]
        qty_row.append(Paragraph(f"{total_qty:,.0f}", total_cell))
        rows.append(qty_row)

        rate_row = [Paragraph('', ts['td']), Paragraph('', ts['td']),
                    Paragraph('Rate', metric_lbl), Paragraph('', ts['td'])]
        rate_row += [_num(c['rate'], dp=2) for c in item['cells']]
        rate_row.append(Paragraph(f"{avg_rate:,.2f}", total_cell))
        rows.append(rate_row)

        if is_admin:
            gp_row = [Paragraph('', ts['td']), Paragraph('', ts['td']),
                      Paragraph('GP %', metric_lbl), Paragraph('', ts['td'])]
            gp_row += [Paragraph(f"{c['gp_pct']:.0f}%", mth_td) if c['qty'] else Paragraph('–', mth_td_faint)
                       for c in item['cells']]
            gp_row.append(Paragraph(f"{gp_pct:.1f}%", total_cell))
            rows.append(gp_row)
        return rows

    for idx, cust in enumerate(customer_rows[:PDF_MAX_CUSTOMERS], start=1):
        row = [
            Paragraph(str(idx), ts['td_c']),
            Paragraph(cust['customer_code'] or '—', ts['td']),
            Paragraph('', ts['td']),
            Paragraph(f"{cust['customer_name'] or '—'} ({cust['item_count']} items)", ts['td_bold']),
        ]
        row += [Paragraph('', mth_td)] * n_months
        gp_suffix = f" · GP {cust['gp_pct']:.1f}%" if is_admin else ''
        row.append(Paragraph(f"{_fmt(cust['total_amt'])}<br/>"
                              f"<font size=4.5>Qty {_fmt(cust['total_qty'])}{gp_suffix}</font>", total_cell))
        table_data.append(row)

        shown_items = cust['items'][:PDF_MAX_ITEMS_PER_CUSTOMER]
        for item in shown_items:
            for r in _item_rows(item):
                item_sub_row_indices.add(len(table_data))
                table_data.append(r)
        hidden = cust['item_count'] - len(shown_items)
        if hidden > 0:
            item_sub_row_indices.add(len(table_data))
            more_row = [
                Paragraph('', ts['td']), Paragraph('', ts['td']), Paragraph('', ts['td']),
                Paragraph(f"… +{hidden} more item(s) not shown", ts['td_muted']),
            ]
            more_row += [Paragraph('', mth_td)] * n_months
            more_row.append(Paragraph('', mth_td))
            table_data.append(more_row)

    total_row = [
        Paragraph('', ts['td']),
        Paragraph('TOTAL', ts['td_bold']),
        Paragraph('', ts['td']),
        Paragraph(f"{len(customer_rows)} customers", ts['total_label']),
    ]
    total_row += [Paragraph('', mth_td)] * n_months
    gp_suffix = f" · GP {overall_gp_pct:.1f}%" if is_admin else ''
    total_row.append(Paragraph(f"{_fmt(ctx['grand_total_amt'])}<br/>"
                                f"<font size=4.5>Qty {_fmt(ctx['grand_total_qty'])}{gp_suffix}</font>", total_cell))
    table_data.append(total_row)

    data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = _build_analysis_table_style(num_rows=len(table_data), customer_row_indices=item_sub_row_indices)
    last_col = len(col_widths) - 1
    table_style.add('ALIGN', (4, 0), (last_col, -1), 'RIGHT')
    table_style.add('ALIGN', (0, 0), (0, -1), 'CENTER')
    table_style.add('LEFTPADDING', (0, 0), (-1, -1), 2)
    table_style.add('RIGHTPADDING', (0, 0), (-1, -1), 2)
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
