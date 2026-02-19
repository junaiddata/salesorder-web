"""
Item Quoted Analysis PDF Export
Firm-wise analysis: Qty Quoted 2025/2026, Customer count per item.
Uses shared design elements from finance_statement_pdf_export.
Supports include_customers toggle: default off (summary only); on = customer details per item.
"""
from io import BytesIO
from datetime import datetime
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Count, Max, Value, DecimalField
from django.db.models.functions import Coalesce
from django.http import HttpResponse

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

from .models import Items, SAPQuotation, SAPQuotationItem, SAPSalesorder
from .views import salesman_scope_q
import re
from collections import defaultdict

# Shared design system
from .finance_statement_pdf_export import (
    _fmt,
    _get_logo,
    _build_document_header,
    _build_kpi_bar,
    _build_styles,
    _build_section_header,
    _build_page_footer,
    # Design tokens
    CLR_PRIMARY, CLR_PRIMARY_LT, CLR_ACCENT, CLR_ACCENT_LT,
    CLR_BG_HEADER, CLR_BG_TOTAL, CLR_BG_ZEBRA, CLR_BG_SECTION,
    CLR_BORDER, CLR_BORDER_HEAVY, CLR_TEXT, CLR_TEXT_MUTED, CLR_TEXT_FAINT,
    CLR_DANGER, CLR_WHITE,
    SP_SECTION,
)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Customer detail row colors
CLR_CUST_BG       = HexColor('#FAFBFC')     # Subtle tint for customer sub-rows
CLR_CUST_BORDER   = HexColor('#E8ECF0')     # Lighter border for sub-rows
CLR_CUST_ACCENT   = HexColor('#94A3B8')     # Muted accent for arrow/indent
CLR_CUST_TEXT     = HexColor('#475569')      # Slightly muted body text

# Compact font sizes for this report
FONT_TH           = 6.5
FONT_TD           = 6.5
FONT_TD_BOLD      = 6.5
FONT_CUST         = 6                        # Customer detail sub-rows
FONT_CUST_NAME    = 6


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL STYLES — tuned for this report's column density
# ─────────────────────────────────────────────────────────────────────────────

def _build_analysis_styles():
    """
    Build ParagraphStyles specific to the Item Quoted Analysis report.
    Compact sizes to fit 10 columns comfortably on landscape A4.
    """
    from reportlab.lib.styles import getSampleStyleSheet
    base = getSampleStyleSheet()['Normal']

    def _ps(name, **kw):
        return ParagraphStyle(name, parent=base, **kw)

    return {
        # Table header cells
        'th': _ps('AthL',
            fontName='Helvetica-Bold', fontSize=FONT_TH,
            textColor=CLR_WHITE, leading=FONT_TH + 2,
        ),
        'th_r': _ps('AthR',
            fontName='Helvetica-Bold', fontSize=FONT_TH,
            textColor=CLR_WHITE, leading=FONT_TH + 2,
            alignment=TA_RIGHT,
        ),
        'th_c': _ps('AthC',
            fontName='Helvetica-Bold', fontSize=FONT_TH,
            textColor=CLR_WHITE, leading=FONT_TH + 2,
            alignment=TA_CENTER,
        ),

        # Standard data cells
        'td': _ps('Atd',
            fontName='Helvetica', fontSize=FONT_TD,
            textColor=CLR_TEXT, leading=FONT_TD + 2,
        ),
        'td_r': _ps('AtdR',
            fontName='Helvetica', fontSize=FONT_TD,
            textColor=CLR_TEXT, leading=FONT_TD + 2,
            alignment=TA_RIGHT,
        ),
        'td_c': _ps('AtdC',
            fontName='Helvetica', fontSize=FONT_TD,
            textColor=CLR_TEXT, leading=FONT_TD + 2,
            alignment=TA_CENTER,
        ),
        'td_bold': _ps('AtdBold',
            fontName='Helvetica-Bold', fontSize=FONT_TD_BOLD,
            textColor=CLR_TEXT, leading=FONT_TD_BOLD + 2,
        ),
        'td_bold_r': _ps('AtdBoldR',
            fontName='Helvetica-Bold', fontSize=FONT_TD_BOLD,
            textColor=CLR_TEXT, leading=FONT_TD_BOLD + 2,
            alignment=TA_RIGHT,
        ),

        # Muted dash for zero values
        'td_muted': _ps('AtdMuted',
            fontName='Helvetica', fontSize=FONT_TD,
            textColor=CLR_TEXT_FAINT, leading=FONT_TD + 2,
            alignment=TA_RIGHT,
        ),
        'td_muted_c': _ps('AtdMutedC',
            fontName='Helvetica', fontSize=FONT_TD,
            textColor=CLR_TEXT_FAINT, leading=FONT_TD + 2,
            alignment=TA_CENTER,
        ),

        # Customer detail sub-rows
        'cust_name': _ps('AcustName',
            fontName='Helvetica', fontSize=FONT_CUST_NAME,
            textColor=CLR_CUST_TEXT, leading=FONT_CUST_NAME + 2,
            leftIndent=8,
        ),
        'cust_code': _ps('AcustCode',
            fontName='Helvetica', fontSize=FONT_CUST,
            textColor=CLR_CUST_ACCENT, leading=FONT_CUST + 2,
        ),
        'cust_val': _ps('AcustVal',
            fontName='Helvetica', fontSize=FONT_CUST,
            textColor=CLR_CUST_TEXT, leading=FONT_CUST + 2,
            alignment=TA_RIGHT,
        ),
        'cust_val_bold': _ps('AcustValBold',
            fontName='Helvetica-Bold', fontSize=FONT_CUST,
            textColor=CLR_CUST_TEXT, leading=FONT_CUST + 2,
            alignment=TA_RIGHT,
        ),

        # Totals row label
        'total_label': _ps('AtotalLabel',
            fontName='Helvetica-Oblique', fontSize=FONT_TD,
            textColor=CLR_TEXT_MUTED, leading=FONT_TD + 2,
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(x):
    """Safely convert value to float."""
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _fmt_val(value, style_nonzero, style_zero):
    """
    Return a Paragraph: formatted number if > 0, muted en-dash if 0/None.
    Keeps the table scannable — zeros don't compete with real data.
    """
    v = _safe_float(value)
    if v > 0:
        formatted = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
        return Paragraph(formatted, style_nonzero)
    return Paragraph('–', style_zero)


def _fmt_int(value, style_nonzero, style_zero):
    """Same as _fmt_val but for integer counts (no decimals)."""
    try:
        v = int(value) if value else 0
    except (TypeError, ValueError):
        v = 0
    if v > 0:
        return Paragraph(str(v), style_nonzero)
    return Paragraph('–', style_zero)


def _build_analysis_table_style(num_rows, customer_row_indices=None):
    """
    Build a professional TableStyle for the analysis grid.
    Handles: header, zebra striping, customer sub-row tinting, totals row.
    customer_row_indices: set of row indices that are customer detail sub-rows.
    """
    customer_rows = customer_row_indices or set()

    cmds = [
        # Header
        ('BACKGROUND', (0, 0), (-1, 0), CLR_BG_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, 0), CLR_WHITE),

        # Outer border
        ('BOX', (0, 0), (-1, -1), 0.75, CLR_BORDER_HEAVY),
        ('LINEBELOW', (0, 0), (-1, 0), 1, CLR_BORDER_HEAVY),

        # Cell padding — compact
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 1), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),

        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),

        # Column group separator: between Description/UPC and numeric columns
        ('LINEAFTER', (3, 0), (3, -1), 0.6, CLR_BORDER_HEAVY),

        # Totals row
        ('BACKGROUND', (0, -1), (-1, -1), CLR_BG_TOTAL),
        ('LINEABOVE', (0, -1), (-1, -1), 1.2, CLR_PRIMARY),
    ]

    # Zebra striping for item rows, special tint for customer sub-rows
    for i in range(1, num_rows - 1):
        if i in customer_rows:
            # Customer sub-row: distinct subtle tint + lighter top border
            cmds.append(('BACKGROUND', (0, i), (-1, i), CLR_CUST_BG))
            cmds.append(('LINEABOVE', (0, i), (-1, i), 0.15, CLR_CUST_BORDER))
        else:
            # Normal item row: standard zebra
            if i % 2 == 0:
                cmds.append(('BACKGROUND', (0, i), (-1, i), CLR_BG_ZEBRA))
            # Subtle row separator
            cmds.append(('LINEBELOW', (0, i), (-1, i), 0.2, CLR_BORDER))

    return TableStyle(cmds)


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING (business logic unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _get_conversion_metrics_pdf(item_codes, quotation_items_qs):
    """Calculate conversion metrics for PDF export - split by year (same logic as view)."""
    sos_with_nfref = SAPSalesorder.objects.filter(
        nf_ref__isnull=False
    ).exclude(nf_ref='').values('id', 'nf_ref', 'posting_date')
    
    quotation_to_so_data = defaultdict(list)
    for so in sos_with_nfref:
        nf_ref = so['nf_ref']
        if not nf_ref:
            continue
        match = re.search(r'(?:Quotations?|Q)\s+(\d+)', nf_ref, re.IGNORECASE)
        if match:
            quotation_number = match.group(1)
            quotation_to_so_data[quotation_number].append((so['id'], so['posting_date']))
    
    if not quotation_to_so_data:
        return {item_code: {
            'so_qty_from_converted_2025': 0.0,
            'so_qty_from_converted_2026': 0.0,
            'converted_quotation_count_2025': 0,
            'converted_quotation_count_2026': 0,
            'conversion_rate_2025': 0.0,
            'conversion_rate_2026': 0.0,
        } for item_code in item_codes}
    
    converted_quotation_numbers = set(quotation_to_so_data.keys())
    all_so_ids = []
    for so_data_list in quotation_to_so_data.values():
        all_so_ids.extend([so_id for so_id, _ in so_data_list])
    
    from .models import SAPSalesorderItem
    so_items = SAPSalesorderItem.objects.filter(
        salesorder_id__in=all_so_ids,
        item_no__in=item_codes
    ).exclude(item_no__isnull=True).exclude(item_no='').select_related('salesorder').values(
        'item_no', 'quantity', 'salesorder_id', 'salesorder__posting_date'
    )
    
    so_id_to_data = {}
    for quotation_number, so_data_list in quotation_to_so_data.items():
        for so_id, posting_date in so_data_list:
            so_id_to_data[so_id] = (quotation_number, posting_date)
    
    so_qty_by_item_2025 = defaultdict(float)
    so_qty_by_item_2026 = defaultdict(float)
    converted_quotations_by_item_2025 = defaultdict(set)
    converted_quotations_by_item_2026 = defaultdict(set)
    
    for so_item in so_items:
        item_code = so_item['item_no']
        so_id = so_item['salesorder_id']
        posting_date = so_item['salesorder__posting_date']
        data = so_id_to_data.get(so_id)
        
        if item_code and data:
            quotation_number, _ = data
            qty = _safe_float(so_item['quantity'])
            
            if posting_date:
                year = posting_date.year if hasattr(posting_date, 'year') else (posting_date.year if hasattr(posting_date, 'year') else None)
                if year == 2025:
                    so_qty_by_item_2025[item_code] += qty
                    converted_quotations_by_item_2025[item_code].add(quotation_number)
                elif year == 2026:
                    so_qty_by_item_2026[item_code] += qty
                    converted_quotations_by_item_2026[item_code].add(quotation_number)
    
    converted_quotation_items_2025 = quotation_items_qs.filter(
        quotation__q_number__in=converted_quotation_numbers,
        quotation__posting_date__year=2025
    ).exclude(quotation__posting_date__isnull=True)
    
    converted_quotation_items_2026 = quotation_items_qs.filter(
        quotation__q_number__in=converted_quotation_numbers,
        quotation__posting_date__year=2026
    ).exclude(quotation__posting_date__isnull=True)
    
    converted_qty_by_item_2025 = defaultdict(float)
    converted_qty_by_item_2026 = defaultdict(float)
    
    for qi in converted_quotation_items_2025:
        item_code = qi.item_no
        if item_code and item_code in item_codes:
            converted_qty_by_item_2025[item_code] += _safe_float(qi.quantity)
    
    for qi in converted_quotation_items_2026:
        item_code = qi.item_no
        if item_code and item_code in item_codes:
            converted_qty_by_item_2026[item_code] += _safe_float(qi.quantity)
    
    result = {}
    for item_code in item_codes:
        so_qty_2025 = so_qty_by_item_2025.get(item_code, 0.0)
        so_qty_2026 = so_qty_by_item_2026.get(item_code, 0.0)
        converted_count_2025 = len(converted_quotations_by_item_2025.get(item_code, set()))
        converted_count_2026 = len(converted_quotations_by_item_2026.get(item_code, set()))
        quoted_qty_2025 = converted_qty_by_item_2025.get(item_code, 0.0)
        quoted_qty_2026 = converted_qty_by_item_2026.get(item_code, 0.0)
        
        conversion_rate_2025 = 0.0
        if quoted_qty_2025 > 0:
            conversion_rate_2025 = (so_qty_2025 / quoted_qty_2025) * 100.0
        
        conversion_rate_2026 = 0.0
        if quoted_qty_2026 > 0:
            conversion_rate_2026 = (so_qty_2026 / quoted_qty_2026) * 100.0
        
        result[item_code] = {
            'so_qty_from_converted_2025': so_qty_2025,
            'so_qty_from_converted_2026': so_qty_2026,
            'converted_quotation_count_2025': converted_count_2025,
            'converted_quotation_count_2026': converted_count_2026,
            'conversion_rate_2025': conversion_rate_2025,
            'conversion_rate_2026': conversion_rate_2026,
        }
    
    return result


def _get_items_data(request, include_customers=False, include_conversion=False):
    """
    Build items list for PDF export.
    Same logic as item_quoted_analysis view but without pagination.
    """
    selected_firms = request.GET.getlist('firm')
    firm_list = list(dict.fromkeys([f.strip() for f in selected_firms if f and str(f).strip()]))
    if not firm_list:
        return [], [], 0, 0, 0

    items_qs = Items.objects.filter(item_firm__in=firm_list)
    item_codes = list(items_qs.values_list('item_code', flat=True).distinct())
    if not item_codes:
        return [], firm_list, 0, 0, 0

    quotation_qs = SAPQuotation.objects.filter(salesman_scope_q(request.user))
    quotation_items_qs = SAPQuotationItem.objects.filter(
        quotation__in=quotation_qs,
        item_no__in=item_codes,
    ).exclude(item_no__isnull=True).exclude(item_no='').select_related('quotation')

    quoted_2025_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2025)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(qty_quoted=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    quoted_2025_dict = {row['item_no']: _safe_float(row['qty_quoted']) for row in quoted_2025_agg}

    quoted_2026_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2026)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(qty_quoted=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())))
    )
    quoted_2026_dict = {row['item_no']: _safe_float(row['qty_quoted']) for row in quoted_2026_agg}

    quotations_2025_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2025)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(quotation_count=Count('quotation', distinct=True))
    )
    quotations_2025_dict = {row['item_no']: row['quotation_count'] for row in quotations_2025_agg}

    quotations_2026_agg = list(
        quotation_items_qs.filter(quotation__posting_date__year=2026)
        .exclude(quotation__posting_date__isnull=True)
        .values('item_no')
        .annotate(quotation_count=Count('quotation', distinct=True))
    )
    quotations_2026_dict = {row['item_no']: row['quotation_count'] for row in quotations_2026_agg}

    customer_count_agg = list(
        quotation_items_qs
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('item_no')
        .annotate(customer_count=Count('quotation__customer_code', distinct=True))
    )
    customer_count_dict = {row['item_no']: row['customer_count'] for row in customer_count_agg}

    items_info = {}
    for item in items_qs:
        if item.item_code not in items_info:
            items_info[item.item_code] = {
                'description': item.item_description or '',
                'upc': item.item_upvc or '',
                'total_stock': _safe_float(item.total_available_stock) if hasattr(item, 'total_available_stock') else 0.0,
            }

    for qi in quotation_items_qs[:1000]:
        if qi.item_no and qi.item_no not in items_info:
            items_info[qi.item_no] = {
                'description': qi.description or '',
                'upc': '',
                'total_stock': 0.0,
            }

    items_list = []
    all_item_codes = set(item_codes)
    for item_code in all_item_codes:
        item_info = items_info.get(item_code, {'description': '', 'upc': '', 'total_stock': 0.0})
        items_list.append({
            'item_code': item_code,
            'item_description': item_info['description'],
            'upc_code': item_info['upc'],
            'total_stock': item_info['total_stock'],
            'qty_quoted_2025': quoted_2025_dict.get(item_code, 0.0),
            'qty_quoted_2026': quoted_2026_dict.get(item_code, 0.0),
            'total_quotations_2025': quotations_2025_dict.get(item_code, 0),
            'total_quotations_2026': quotations_2026_dict.get(item_code, 0),
            'customer_quoted_count': customer_count_dict.get(item_code, 0),
            'customers': [],
        })

    items_list.sort(
        key=lambda x: (x['qty_quoted_2025'] + x['qty_quoted_2026'], x['customer_quoted_count']),
        reverse=True,
    )

    grand_total_2025 = sum(i['qty_quoted_2025'] for i in items_list)
    grand_total_2026 = sum(i['qty_quoted_2026'] for i in items_list)
    grand_total_customers = len(
        quotation_items_qs.exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('quotation__customer_code')
        .distinct()
    )

    if include_customers and items_list:
        item_codes_all = [i['item_code'] for i in items_list]
        customer_details = _get_customer_details_for_items(quotation_items_qs, item_codes_all)
        for item in items_list:
            item['customers'] = customer_details.get(item['item_code'], [])
    
    if include_conversion and items_list:
        item_codes_all = [i['item_code'] for i in items_list]
        conversion_metrics = _get_conversion_metrics_pdf(item_codes_all, quotation_items_qs)
        for item in items_list:
            metrics = conversion_metrics.get(item['item_code'], {
                'so_qty_from_converted': 0.0,
                'converted_quotation_count': 0,
                'conversion_rate': 0.0
            })
            item.update(metrics)

    return items_list, firm_list, grand_total_2025, grand_total_2026, grand_total_customers


def _get_customer_details_for_items(quotation_items_qs, item_codes):
    """Get customer details per item. Includes qty split by year 2025/2026."""
    filtered_items = quotation_items_qs.filter(item_no__in=item_codes)
    customer_aggs = list(
        filtered_items
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('item_no', 'quotation__customer_code')
        .annotate(
            total_quantity=Sum('quantity'),
            qty_2025=Coalesce(Sum('quantity', filter=Q(quotation__posting_date__year=2025)), Value(0, output_field=DecimalField())),
            qty_2026=Coalesce(Sum('quantity', filter=Q(quotation__posting_date__year=2026)), Value(0, output_field=DecimalField())),
            quotation_count_2025=Count('quotation', distinct=True, filter=Q(quotation__posting_date__year=2025)),
            quotation_count_2026=Count('quotation', distinct=True, filter=Q(quotation__posting_date__year=2026)),
            customer_name=Max('quotation__customer_name'),
        )
    )
    quotation_numbers_raw = list(
        filtered_items
        .exclude(quotation__customer_code__isnull=True)
        .exclude(quotation__customer_code='')
        .values('item_no', 'quotation__customer_code', 'quotation__q_number')
        .distinct()
    )
    quotation_numbers_lookup = defaultdict(lambda: defaultdict(list))
    for row in quotation_numbers_raw:
        if row['quotation__q_number']:
            quotation_numbers_lookup[row['item_no']][row['quotation__customer_code']].append(
                row['quotation__q_number']
            )

    result = defaultdict(list)
    for agg in customer_aggs:
        qty = _safe_float(agg['total_quantity'] or 0)
        if qty == 0:
            continue
        item_code = agg['item_no']
        quotation_numbers = sorted(quotation_numbers_lookup[item_code][agg['quotation__customer_code'] or ''])
        result[item_code].append({
            'customer_code': agg['quotation__customer_code'] or '',
            'customer_name': agg['customer_name'] or 'Unknown',
            'qty_quoted': qty,
            'qty_quoted_2025': _safe_float(agg.get('qty_2025', 0)),
            'qty_quoted_2026': _safe_float(agg.get('qty_2026', 0)),
            'quotation_count': len(quotation_numbers_lookup[item_code][agg['quotation__customer_code'] or '']),
            'quotation_count_2025': agg.get('quotation_count_2025', 0) or 0,
            'quotation_count_2026': agg.get('quotation_count_2026', 0) or 0,
            'quotation_numbers': quotation_numbers[:10],
        })
    for item_code in result:
        result[item_code].sort(key=lambda x: x['qty_quoted'], reverse=True)
    return dict(result)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXPORT VIEW
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def export_item_quoted_analysis_pdf(request):
    """
    Export Item Quoted Analysis to PDF.
    Query params: firm (multi), include_customers (1/true/yes/on), include_conversion (1/true/yes/on).
    Default: summary table only.
    """
    include_customers = request.GET.get('include_customers', '').strip().lower() in ('1', 'true', 'yes', 'on')
    include_conversion = request.GET.get('include_conversion', '').strip().lower() in ('1', 'true', 'yes', 'on')

    items_list, firm_list, grand_total_2025, grand_total_2026, grand_total_customers = _get_items_data(
        request, include_customers=include_customers, include_conversion=include_conversion,
    )

    # ── PDF setup ──
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="item_quoted_analysis_'
        f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    margin_h = 18 if include_customers else 22
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
    page_styles = _build_styles()       # Shared styles for header/KPI
    ts = _build_analysis_styles()       # Local compact styles for table
    elements = []

    # ── 1. Document Header ──
    firm_label = ', '.join(firm_list[:3])
    if len(firm_list) > 3:
        firm_label += f' (+{len(firm_list) - 3} more)'

    subtitle_parts = [firm_label, 'Qty Quoted 2025 & 2026']
    if include_customers:
        subtitle_parts.append('With Customer Breakdown')

    elements.extend(_build_document_header(
        page_styles,
        title_text='ITEM QUOTED ANALYSIS',
        subtitle_text=' — '.join(subtitle_parts),
        page_width=usable_width,
    ))

    # ── 2. KPI Bar ──
    total_qty = grand_total_2025 + grand_total_2026
    kpi_items = [
        ('Qty Quoted 2025', _fmt(grand_total_2025)),
        ('Qty Quoted 2026', _fmt(grand_total_2026)),
        ('Combined Total', _fmt(total_qty)),
        ('Unique Items', str(len(items_list))),
        ('Unique Customers', str(grand_total_customers)),
    ]
    elements.append(_build_kpi_bar(kpi_items, page_styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    # ── 3. Empty states ──
    if not firm_list:
        elements.append(Paragraph(
            '<font color="#6B7280">No firm selected. Use the report page to select firms, then export.</font>',
            page_styles['label'],
        ))
        doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
        response.write(buffer.getvalue())
        return response

    if not items_list:
        elements.append(Paragraph(
            '<font color="#6B7280">No items found for the selected firm(s).</font>',
            page_styles['label'],
        ))
        doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
        response.write(buffer.getvalue())
        return response

    # ── 4. Column layout ──
    #
    # 10 columns (or 13 with conversion) on landscape A4 (~806pt usable at 18pt margins)
    #
    # Fixed columns:
    #   # = 20  |  Code = 62  |  UPC = 58  |  Stock = 50
    #   Qty2025 = 56  |  Qty2026 = 56  |  Q#25 = 42  |  Q#26 = 42  |  Cust = 40
    #   [+ SO Qty = 50 | Conv Quotes = 38 | Conv Rate = 45 if include_conversion]
    # Flexible: Description absorbs remainder

    W_NUM     = 20
    W_CODE    = 62
    W_UPC     = 58
    W_STOCK   = 50
    W_QTY     = 56     # Qty 2025 / 2026
    W_QUOTES  = 42     # Quote count columns
    W_CUST    = 40
    W_SO_QTY  = 45     # SO Qty from converted (split by year)
    W_CONV_Q  = 35     # Converted quotes count (split by year)
    W_CONV_R  = 40     # Conversion rate % (split by year)

    base_fixed = W_NUM + W_CODE + W_UPC + W_STOCK + (2 * W_QTY) + (2 * W_QUOTES) + W_CUST
    if include_conversion:
        fixed_total = base_fixed + (2 * W_SO_QTY) + (2 * W_CONV_Q) + (2 * W_CONV_R)
    else:
        fixed_total = base_fixed
    W_DESC = max(120, usable_width - fixed_total)

    col_widths = [
        W_NUM,       # 0: #
        W_CODE,      # 1: Item Code
        W_DESC,      # 2: Description
        W_UPC,       # 3: UPC
        W_STOCK,     # 4: Stock
        W_QTY,       # 5: Qty 2025
        W_QTY,       # 6: Qty 2026
        W_QUOTES,    # 7: Quotes 2025
        W_QUOTES,    # 8: Quotes 2026
        W_CUST,      # 9: Customers
    ]
    if include_conversion:
        col_widths.extend([
            W_SO_QTY,   # 10: SO Qty 2025
            W_SO_QTY,   # 11: SO Qty 2026
            W_CONV_Q,   # 12: Conv Quotes 2025
            W_CONV_Q,   # 13: Conv Quotes 2026
            W_CONV_R,   # 14: Conv % 2025
            W_CONV_R,   # 15: Conv % 2026
        ])

    # ── 5. Header row ──
    hdr = [
        Paragraph('#',           ts['th_c']),
        Paragraph('Item Code',   ts['th']),
        Paragraph('Description', ts['th']),
        Paragraph('UPC',         ts['th']),
        Paragraph('Stock',       ts['th_r']),
        Paragraph('Qty 2025',    ts['th_r']),
        Paragraph('Qty 2026',    ts['th_r']),
        Paragraph("Q's 25",     ts['th_r']),
        Paragraph("Q's 26",     ts['th_r']),
        Paragraph('Cust.',       ts['th_c']),
    ]
    if include_conversion:
        hdr.extend([
            Paragraph('SO 25',       ts['th_r']),
            Paragraph('SO 26',       ts['th_r']),
            Paragraph('CQ 25',       ts['th_c']),
            Paragraph('CQ 26',       ts['th_c']),
            Paragraph('% 25',        ts['th_r']),
            Paragraph('% 26',        ts['th_r']),
        ])
    table_data = [hdr]
    customer_row_indices = set()   # Track which rows are customer sub-rows

    # ── 6. Data rows ──
    row_num = 0    # Running row counter (excluding header)
    for idx, item in enumerate(items_list, start=1):
        row_num += 1
        desc_text = (item['item_description'] or '—')[:50]

        row = [
            Paragraph(str(idx), ts['td_c']),
            Paragraph(item['item_code'] or '—', ts['td_bold']),
            Paragraph(desc_text, ts['td']),
            Paragraph((item['upc_code'] or '—')[:16], ts['td']),
            _fmt_val(item['total_stock'], ts['td_r'], ts['td_muted']),
            _fmt_val(item['qty_quoted_2025'], ts['td_bold_r'], ts['td_muted']),
            _fmt_val(item['qty_quoted_2026'], ts['td_bold_r'], ts['td_muted']),
            _fmt_int(item['total_quotations_2025'], ts['td_r'], ts['td_muted']),
            _fmt_int(item['total_quotations_2026'], ts['td_r'], ts['td_muted']),
            _fmt_int(item['customer_quoted_count'], ts['td_c'], ts['td_muted_c']),
        ]
        if include_conversion:
            row.extend([
                _fmt_val(item.get('so_qty_from_converted_2025', 0), ts['td_bold_r'], ts['td_muted']),
                _fmt_val(item.get('so_qty_from_converted_2026', 0), ts['td_bold_r'], ts['td_muted']),
                _fmt_int(item.get('converted_quotation_count_2025', 0), ts['td_c'], ts['td_muted_c']),
                _fmt_int(item.get('converted_quotation_count_2026', 0), ts['td_c'], ts['td_muted_c']),
                _fmt_val(item.get('conversion_rate_2025', 0), ts['td_bold_r'], ts['td_muted']),
                _fmt_val(item.get('conversion_rate_2026', 0), ts['td_bold_r'], ts['td_muted']),
            ])
        table_data.append(row)

        # Customer sub-rows (indented, muted styling)
        if include_customers and item.get('customers'):
            for cust in item['customers']:
                row_num += 1
                customer_row_indices.add(row_num)

                cust_display = f"↳ {cust['customer_name'][:32]}"
                cust_code_display = cust['customer_code'] or ''

                cust_row = [
                    Paragraph('', ts['td']),                                               # #
                    Paragraph(cust_code_display, ts['cust_code']),                         # Code col → customer code
                    Paragraph(cust_display, ts['cust_name']),                              # Desc col → customer name
                    Paragraph('', ts['td']),                                               # UPC
                    Paragraph('', ts['td']),                                               # Stock
                    _fmt_val(cust.get('qty_quoted_2025', 0), ts['cust_val_bold'], ts['td_muted']),
                    _fmt_val(cust.get('qty_quoted_2026', 0), ts['cust_val_bold'], ts['td_muted']),
                    _fmt_int(cust.get('quotation_count_2025', 0), ts['cust_val'], ts['td_muted']),
                    _fmt_int(cust.get('quotation_count_2026', 0), ts['cust_val'], ts['td_muted']),
                    Paragraph('', ts['td']),                                               # Cust
                ]
                if include_conversion:
                    cust_row.extend([
                        Paragraph('', ts['td']),                                            # SO 25
                        Paragraph('', ts['td']),                                            # SO 26
                        Paragraph('', ts['td']),                                            # CQ 25
                        Paragraph('', ts['td']),                                            # CQ 26
                        Paragraph('', ts['td']),                                            # % 25
                        Paragraph('', ts['td']),                                            # % 26
                    ])
                table_data.append(cust_row)

    # ── 7. Totals row ──
    total_stock = sum(i['total_stock'] for i in items_list)
    total_q25 = sum(i['total_quotations_2025'] for i in items_list)
    total_q26 = sum(i['total_quotations_2026'] for i in items_list)

    total_row = [
        Paragraph('', ts['td']),
        Paragraph('TOTAL', ts['td_bold']),
        Paragraph(f'{len(items_list)} items', ts['total_label']),
        Paragraph('', ts['td']),
        Paragraph(_fmt(total_stock), ts['td_bold_r']),
        Paragraph(_fmt(grand_total_2025), ts['td_bold_r']),
        Paragraph(_fmt(grand_total_2026), ts['td_bold_r']),
        Paragraph(str(total_q25), ts['td_bold_r']),
        Paragraph(str(total_q26), ts['td_bold_r']),
        Paragraph(str(grand_total_customers), ts['td_bold']),
    ]
    if include_conversion:
        total_so_qty_2025 = sum(i.get('so_qty_from_converted_2025', 0) for i in items_list)
        total_so_qty_2026 = sum(i.get('so_qty_from_converted_2026', 0) for i in items_list)
        total_conv_quotes_2025 = sum(i.get('converted_quotation_count_2025', 0) for i in items_list)
        total_conv_quotes_2026 = sum(i.get('converted_quotation_count_2026', 0) for i in items_list)
        total_row.extend([
            Paragraph(_fmt(total_so_qty_2025), ts['td_bold_r']),
            Paragraph(_fmt(total_so_qty_2026), ts['td_bold_r']),
            Paragraph(str(total_conv_quotes_2025), ts['td_bold']),
            Paragraph(str(total_conv_quotes_2026), ts['td_bold']),
            Paragraph('', ts['td']),  # Conversion rate totals not meaningful
            Paragraph('', ts['td']),
        ])
    table_data.append(total_row)

    # ── 8. Build table with style ──
    data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = _build_analysis_table_style(
        num_rows=len(table_data),
        customer_row_indices=customer_row_indices,
    )

    # Right-align numeric columns (4–8), center column 9
    table_style.add('ALIGN', (4, 0), (8, -1), 'RIGHT')
    table_style.add('ALIGN', (9, 0), (9, -1), 'CENTER')
    # Center the row number column
    table_style.add('ALIGN', (0, 0), (0, -1), 'CENTER')

    data_table.setStyle(table_style)
    elements.append(data_table)

    # ── Build and return ──
    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response