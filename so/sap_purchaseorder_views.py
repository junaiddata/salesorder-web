"""
Views for SAP Purchase Order: sync receive, list, detail, search, PDF/Excel exports.
"""
import json
import pandas as pd
import logging
import os
from datetime import datetime, date, timedelta
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum, Value, DecimalField, Exists, OuterRef, Case, When, CharField, F
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, KeepTogether, SimpleDocTemplate, Image
from reportlab.lib.styles import getSampleStyleSheet

from .models import SAPPurchaseOrder, SAPPurchaseOrderItem, Items
from .views_quotation import QuotationPDFTemplate, styles

logger = logging.getLogger(__name__)


def _open_row_status_q_po() -> Q:
    """Return Q for open line status (O/Open) on SAPPurchaseOrderItem."""
    return Q(row_status__iexact="open") | Q(row_status__iexact="o") | Q(row_status__iexact="OPEN") | Q(row_status__iexact="O")


def _dec2(x) -> Decimal:
    """Convert to Decimal with 2 decimal places; treat None/NaN as 0.00."""
    try:
        if x is None:
            return Decimal("0.00")
        if isinstance(x, float) and x != x:  # NaN
            return Decimal("0.00")
        return Decimal(str(x)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _dec_any(x) -> Decimal:
    """Convert to Decimal; treat None/NaN as 0."""
    try:
        if x is None:
            return Decimal("0")
        if isinstance(x, float) and x != x:
            return Decimal("0")
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def save_purchaseorders_locally(purchase_orders, api_po_numbers):
    """
    Replace all open purchase orders with data from the latest API call.
    Each sync completely replaces local data with what the API returns.
    purchase_orders: list of dicts (same format as receive endpoint; posting_date can be ISO string).
    api_po_numbers: ignored; kept for API compatibility.
    Returns: dict with keys replaced (count), total_items.
    """
    if not purchase_orders:
        # Empty response: remove all existing POs (full replacement)
        with transaction.atomic():
            SAPPurchaseOrderItem.objects.all().delete()
            deleted = SAPPurchaseOrder.objects.all().delete()
            count = deleted[0] if isinstance(deleted, tuple) else deleted
        return {'replaced': count, 'total_items': 0}

    stats = {'replaced': 0, 'total_items': 0}
    po_numbers = [m['po_number'] for m in purchase_orders if m.get('po_number')]

    with transaction.atomic():
        # Full replacement: delete all existing POs and items first
        SAPPurchaseOrderItem.objects.all().delete()
        SAPPurchaseOrder.objects.all().delete()

        to_create = []
        for mapped in purchase_orders:
            po_no = mapped.get('po_number')
            if not po_no:
                continue

            posting_date = mapped.get('posting_date')
            if isinstance(posting_date, str):
                try:
                    posting_date = datetime.strptime(posting_date, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    try:
                        posting_date = datetime.strptime(posting_date, '%Y/%m/%d').date()
                    except (ValueError, TypeError):
                        posting_date = None
            elif posting_date and hasattr(posting_date, 'date'):
                posting_date = posting_date.date() if hasattr(posting_date, 'date') else posting_date

            to_create.append(SAPPurchaseOrder(
                po_number=po_no,
                posting_date=posting_date,
                supplier_code=mapped.get('supplier_code', '') or '',
                supplier_name=mapped.get('supplier_name', '') or '',
                supplier_address=mapped.get('supplier_address', '') or '',
                supplier_phone=mapped.get('supplier_phone', '') or '',
                vat_number=mapped.get('vat_number', '') or '',
                bp_reference_no=mapped.get('bp_reference_no', '') or '',
                salesman_name=mapped.get('salesman_name', '') or '',
                discount_percentage=_dec2(mapped.get('discount_percentage', 0)),
                document_total=_dec2(mapped.get('document_total', 0)),
                row_total_sum=_dec2(mapped.get('row_total_sum', 0)),
                vat_sum=_dec2(mapped.get('vat_sum', 0)),
                total_discount=_dec2(mapped.get('total_discount', 0)),
                status=mapped.get('status', 'C'),
                closing_remarks=mapped.get('closing_remarks', '') or '',
                internal_number=mapped.get('internal_number') or '',
            ))

        if to_create:
            SAPPurchaseOrder.objects.bulk_create(to_create, batch_size=5000)
            stats['replaced'] = len(to_create)

        order_id_map = dict(
            SAPPurchaseOrder.objects.filter(po_number__in=po_numbers).values_list("po_number", "id")
        )

        items_to_create = []
        for mapped in purchase_orders:
            po_no = mapped.get('po_number')
            po_id = order_id_map.get(po_no)
            if not po_id:
                continue

            for item_data in mapped.get('items', []):
                items_to_create.append(
                    SAPPurchaseOrderItem(
                        purchaseorder_id=po_id,
                        line_no=item_data.get('line_no', 1),
                        item_no=item_data.get('item_no', ''),
                        description=item_data.get('description', ''),
                        quantity=_dec_any(item_data.get('quantity', 0)),
                        price=_dec_any(item_data.get('price', 0)),
                        row_total=_dec_any(item_data.get('row_total', 0)),
                        row_status=item_data.get('row_status', 'C'),
                        job_type=item_data.get('job_type', '') or '',
                        manufacture=item_data.get('manufacture', '') or '',
                        remaining_open_quantity=_dec_any(item_data.get('remaining_open_quantity', 0)),
                        pending_amount=_dec_any(item_data.get('pending_amount', 0)),
                    )
                )

                if len(items_to_create) >= 20000:
                    SAPPurchaseOrderItem.objects.bulk_create(items_to_create, batch_size=20000)
                    items_to_create = []

        if items_to_create:
            SAPPurchaseOrderItem.objects.bulk_create(items_to_create, batch_size=20000)

        stats['total_items'] = sum(len(m.get('items', [])) for m in purchase_orders)

    return stats


@csrf_exempt
@require_POST
def sync_purchaseorders_api_receive(request):
    """
    Receive purchase orders data from PC script or Django command via HTTP API.
    """
    try:
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body)
        else:
            try:
                data = json.loads(request.body)
            except Exception:
                data = request.POST.dict()

        api_key = data.get('api_key')
        expected_key = getattr(settings, 'VPS_API_KEY', 'your-secret-api-key')

        if not api_key or api_key != expected_key:
            return JsonResponse({
                'success': False,
                'error': 'Invalid API key'
            }, status=401)

        purchase_orders = data.get('purchase_orders', [])
        api_po_numbers = data.get('api_po_numbers', [])

        if not purchase_orders:
            return JsonResponse({
                'success': False,
                'error': 'No purchase orders provided'
            })

        stats = save_purchaseorders_locally(purchase_orders, api_po_numbers)

        return JsonResponse({
            'success': True,
            'stats': stats,
            'message': f'Replaced with {len(purchase_orders)} open purchase orders'
        })

    except Exception as e:
        logger.exception('Error in sync_purchaseorders_api_receive')
        return JsonResponse({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }, status=500)


# =====================
# API: Item totals from Open POs (for external apps)
# =====================


@csrf_exempt
@require_GET
def api_open_purchaseorder_item_totals(request):
    """
    API endpoint: aggregated item_code and total_qty from open purchase order lines.
    Only includes POs from the last 6 months (by posting_date).
    Use remaining_open_quantity when available (what's still coming), else quantity.
    No authentication required.
    Response: { "items": [ { "item_code": "...", "total_qty": 123.45 }, ... ] }
    """
    six_months_ago = date.today() - timedelta(days=180)
    qs = (
        SAPPurchaseOrderItem.objects.filter(_open_row_status_q_po())
        .filter(purchaseorder__posting_date__gte=six_months_ago)
        .values('item_no')
        .annotate(
            total_qty=Sum(Coalesce(F('remaining_open_quantity'), F('quantity'), Value(0, output_field=DecimalField())))
        )
        .order_by('item_no')
    )

    items = [
        {'item_code': row['item_no'] or '', 'total_qty': float(row['total_qty'] or 0)}
        for row in qs
    ]

    return JsonResponse({'items': items})


# =====================
# Purchase Order: List, Detail, Search, Exports
# =====================

def _purchaseorder_items_qs():
    """Base queryset for item-level PO list: open items only, with purchaseorder joined."""
    return SAPPurchaseOrderItem.objects.filter(
        _open_row_status_q_po()
    ).select_related('purchaseorder')


@login_required
def purchaseorder_list(request):
    """Item-level list for SAP Open Purchase Orders (one row per line item)."""
    qs = _purchaseorder_items_qs()

    q = request.GET.get('q', '').strip()
    item_filter = request.GET.get('item', '').strip()
    firm_filter = request.GET.getlist('firm')
    purchaser_filter = request.GET.getlist('purchaser')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    total_range = request.GET.get('total', '').strip()

    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_codes = set(
                Items.objects.filter(item_firm__in=clean_firms).values_list('item_code', flat=True)
            )
            if firm_item_codes:
                qs = qs.filter(item_no__in=firm_item_codes)
            else:
                qs = qs.none()

    if purchaser_filter:
        clean = [p for p in purchaser_filter if p.strip()]
        if clean:
            qs = qs.filter(purchaseorder__salesman_name__in=clean)

    if item_filter:
        qs = qs.filter(
            Q(item_no__icontains=item_filter) |
            Q(description__icontains=item_filter)
        )

    if total_range:
        if total_range == "0-5000":
            qs = qs.filter(pending_amount__gte=0, pending_amount__lte=5000)
        elif total_range == "5001-10000":
            qs = qs.filter(pending_amount__gte=5001, pending_amount__lte=10000)
        elif total_range == "10001-25000":
            qs = qs.filter(pending_amount__gte=10001, pending_amount__lte=25000)
        elif total_range == "25001-50000":
            qs = qs.filter(pending_amount__gte=25001, pending_amount__lte=50000)
        elif total_range == "50001-100000":
            qs = qs.filter(pending_amount__gte=50001, pending_amount__lte=100000)
        elif total_range == "100000+":
            qs = qs.filter(pending_amount__gt=100000)

    if q:
        if q.isdigit():
            qs = qs.filter(
                Q(purchaseorder__po_number__istartswith=q) |
                Q(item_no__istartswith=q) |
                Q(item_no__icontains=q)
            )
        elif len(q) < 3:
            qs = qs.filter(
                Q(purchaseorder__supplier_name__istartswith=q) |
                Q(purchaseorder__salesman_name__istartswith=q) |
                Q(purchaseorder__supplier_code__istartswith=q) |
                Q(purchaseorder__bp_reference_no__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(purchaseorder__po_number__icontains=q) |
                Q(purchaseorder__supplier_name__icontains=q) |
                Q(purchaseorder__salesman_name__icontains=q) |
                Q(purchaseorder__bp_reference_no__icontains=q) |
                Q(purchaseorder__supplier_code__icontains=q) |
                Q(item_no__icontains=q) |
                Q(description__icontains=q)
            )

    def parse_date(s):
        if not s:
            return None
        try:
            if len(s) == 7:
                return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date:
        qs = qs.filter(purchaseorder__posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(purchaseorder__posting_date__lte=end_date)

    total_value = qs.aggregate(
        total=Coalesce(Sum('pending_amount'), Value(0, output_field=DecimalField()))
    )['total']

    yearly_agg = qs.aggregate(
        total_2025=Coalesce(Sum('pending_amount', filter=Q(purchaseorder__posting_date__year=2025)), Value(0, output_field=DecimalField())),
        total_2026=Coalesce(Sum('pending_amount', filter=Q(purchaseorder__posting_date__year=2026)), Value(0, output_field=DecimalField())),
    )
    total_2025 = yearly_agg['total_2025']
    total_2026 = yearly_agg['total_2026']

    qs = qs.order_by('-purchaseorder__posting_date', '-purchaseorder__po_number', 'line_no')

    try:
        page_size = int(request.GET.get('page_size', 100))
    except ValueError:
        page_size = 100
    page_size = max(5, min(page_size, 200))
    paginator = Paginator(qs, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Firms from Item Master ( Items.item_firm ) like in salesorder
    firms_list = list(
        Items.objects.exclude(item_firm__isnull=True)
        .exclude(item_firm='')
        .values_list('item_firm', flat=True)
        .distinct()
        .order_by('item_firm')
    )

    purchasers = list(
        _purchaseorder_items_qs()
        .values_list('purchaseorder__salesman_name', flat=True)
        .distinct()
        .order_by('purchaseorder__salesman_name')
    )
    purchasers = [p for p in purchasers if p]

    show_price = request.user.is_staff or request.user.is_superuser

    return render(request, 'purchaseorders/purchaseorder_list.html', {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'total_2025': total_2025,
        'total_2026': total_2026,
        'firms': firms_list,  # From Items.item_firm (Item Master)
        'purchasers': purchasers,
        'total_value': total_value,
        'show_price': show_price,
        'filters': {
            'q': q,
            'item': item_filter,
            'firm_filter': firm_filter,
            'purchaser_filter': purchaser_filter,
            'start': start,
            'end': end,
            'page_size': page_size,
            'total': total_range,
        }
    })


@login_required
def purchaseorder_detail(request, po_number):
    """Detail view for a single SAP Purchase Order."""
    purchaseorder = get_object_or_404(SAPPurchaseOrder, po_number=po_number)
    items = purchaseorder.items.all().order_by('line_no', 'id')

    any_open = items.filter(_open_row_status_q_po()).exists()
    derived_status = "O" if any_open else "C"
    if (purchaseorder.status or "").strip().upper() not in (derived_status, "OPEN", "CLOSED"):
        purchaseorder.status = derived_status
        purchaseorder.save(update_fields=["status"])

    status_raw = (purchaseorder.status or "").strip()
    status_key = status_raw.upper()
    if status_key in ("O", "OPEN"):
        status_label = "Open"
    elif status_key in ("C", "CLOSED"):
        status_label = "Closed"
    else:
        status_label = status_raw or "—"

    pending_total = items.aggregate(
        total=Coalesce(Sum("pending_amount"), Value(0, output_field=DecimalField()))
    )["total"] or Decimal("0.00")

    row_total_sum = getattr(purchaseorder, "row_total_sum", None) or Decimal("0")
    subtotal = row_total_sum
    discount_percentage = purchaseorder.discount_percentage or Decimal("0.00")
    discount_amount = (subtotal * discount_percentage / 100).quantize(Decimal("0.01")) if subtotal else Decimal("0.00")
    total_before_tax = (subtotal - discount_amount).quantize(Decimal("0.01")) if subtotal else Decimal("0.00")
    vat_sum = getattr(purchaseorder, "vat_sum", None) or Decimal("0.00")
    grand_total = (total_before_tax + vat_sum).quantize(Decimal("0.01"))

    for it in items:
        qty = it.quantity or Decimal("0")
        row_total = it.row_total or Decimal("0")
        it.unit_price = (row_total / qty).quantize(Decimal("0.01")) if qty else Decimal("0.00")

    show_price = request.user.is_staff or request.user.is_superuser

    context = {
        'purchaseorder': purchaseorder,
        'items': items,
        'status_label': status_label,
        'pending_total': pending_total,
        'row_total_sum': row_total_sum,
        'subtotal': subtotal,
        'discount_percentage': round(float(discount_percentage), 1),
        'discount_amount': discount_amount,
        'total_before_tax': total_before_tax,
        'vat_sum': vat_sum,
        'grand_total': grand_total,
        'show_price': show_price,
    }
    return render(request, 'purchaseorders/purchaseorder_detail.html', context)


@login_required
def purchaseorder_search(request):
    """AJAX search for Open Purchase Order item-level rows and pagination."""
    qs = _purchaseorder_items_qs()

    q = request.GET.get('q', '').strip()
    item_filter = request.GET.get('item', '').strip()
    firm_filter = request.GET.getlist('firm')
    purchaser_filter = request.GET.getlist('purchaser')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    total_range = request.GET.get('total', '').strip()

    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_codes = set(
                Items.objects.filter(item_firm__in=clean_firms).values_list('item_code', flat=True)
            )
            if firm_item_codes:
                qs = qs.filter(item_no__in=firm_item_codes)
            else:
                qs = qs.none()

    if purchaser_filter:
        clean = [p for p in purchaser_filter if p.strip()]
        if clean:
            qs = qs.filter(purchaseorder__salesman_name__in=clean)

    if item_filter:
        qs = qs.filter(
            Q(item_no__icontains=item_filter) |
            Q(description__icontains=item_filter)
        )

    if total_range:
        if total_range == "0-5000":
            qs = qs.filter(pending_amount__gte=0, pending_amount__lte=5000)
        elif total_range == "5001-10000":
            qs = qs.filter(pending_amount__gte=5001, pending_amount__lte=10000)
        elif total_range == "10001-25000":
            qs = qs.filter(pending_amount__gte=10001, pending_amount__lte=25000)
        elif total_range == "25001-50000":
            qs = qs.filter(pending_amount__gte=25001, pending_amount__lte=50000)
        elif total_range == "50001-100000":
            qs = qs.filter(pending_amount__gte=50001, pending_amount__lte=100000)
        elif total_range == "100000+":
            qs = qs.filter(pending_amount__gt=100000)

    if q:
        if q.isdigit():
            qs = qs.filter(
                Q(purchaseorder__po_number__istartswith=q) |
                Q(item_no__istartswith=q) |
                Q(item_no__icontains=q)
            )
        elif len(q) < 3:
            qs = qs.filter(
                Q(purchaseorder__supplier_name__istartswith=q) |
                Q(purchaseorder__salesman_name__istartswith=q) |
                Q(purchaseorder__supplier_code__istartswith=q) |
                Q(purchaseorder__bp_reference_no__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(purchaseorder__po_number__icontains=q) |
                Q(purchaseorder__supplier_name__icontains=q) |
                Q(purchaseorder__salesman_name__icontains=q) |
                Q(purchaseorder__bp_reference_no__icontains=q) |
                Q(purchaseorder__supplier_code__icontains=q) |
                Q(item_no__icontains=q) |
                Q(description__icontains=q)
            )

    def parse_date(s):
        if not s:
            return None
        try:
            if len(s) == 7:
                return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date:
        qs = qs.filter(purchaseorder__posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(purchaseorder__posting_date__lte=end_date)

    total_value = qs.aggregate(
        total=Coalesce(Sum('pending_amount'), Value(0, output_field=DecimalField()))
    )['total']

    qs = qs.order_by('-purchaseorder__posting_date', '-purchaseorder__po_number', 'line_no')
    try:
        page_size = int(request.GET.get('page_size', 100))
    except ValueError:
        page_size = 100
    page_size = max(5, min(page_size, 200))
    paginator = Paginator(qs, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    show_price = request.user.is_staff or request.user.is_superuser
    rows_html = render_to_string('purchaseorders/_purchaseorder_rows.html', {'page_obj': page_obj, 'show_price': show_price}, request=request)
    pagination_html = render_to_string('purchaseorders/_pagination.html', {'page_obj': page_obj}, request=request)

    return JsonResponse({
        'rows_html': rows_html,
        'pagination_html': pagination_html,
        'count': paginator.count,
        'total_value': float(total_value or 0),
        'show_price': show_price,
    })


@login_required
def export_sap_purchaseorder_pdf(request, po_number):
    """Generate PDF for a single SAP Purchase Order. Price columns only for admin."""
    purchaseorder = get_object_or_404(SAPPurchaseOrder, po_number=po_number)
    items_qs = purchaseorder.items.all().order_by('id')
    show_price = request.user.is_staff or request.user.is_superuser

    response = HttpResponse(content_type='application/pdf')
    date_str = purchaseorder.posting_date.strftime('%Y%m%d') if purchaseorder.posting_date else 'NA'
    filename = f"SAP_PurchaseOrder_{purchaseorder.po_number}_{date_str}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    buffer = BytesIO()
    company_config = {
        'name': "Junaid Sanitary & Electrical Trading LLC",
        'address': "Dubai Investment Parks 2, Dubai, UAE",
        'contact': "Email: sales@junaid.ae | Phone: +97142367723",
        'logo_url': "https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp",
        'local_logo_path': os.path.join(settings.BASE_DIR, 'static', 'images', 'footer-logo.png.webp')
    }
    theme_config = {'primary': HexColor('#2C5530')}

    doc = QuotationPDFTemplate(
        buffer,
        company_config=company_config,
        theme_config=theme_config,
        pagesize=A4,
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=1.0*inch
    )
    elements = []
    elements.append(Spacer(1, -1.3*inch))

    title_table = Table(
        [[Paragraph('PURCHASE ORDER', styles['MainTitle'])]],
        colWidths=[7.5*inch]
    )
    title_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TEXTCOLOR', (0, 0), (-1, -1), theme_config['primary']),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 0.1*inch))

    main_table_width = 7.2 * inch
    bg_color = theme_config['primary']

    po_data = [
        [Paragraph('Purchase Order Details', styles['SectionHeader'])],
        [Paragraph(f"<b>Number:</b> {purchaseorder.po_number}", styles['Normal'])],
        [Paragraph(f"<b>Date:</b> {purchaseorder.posting_date or '-'}", styles['Normal'])],
        [Paragraph(f"<b>BP Ref No:</b> {purchaseorder.bp_reference_no or '—'}", styles['Normal'])],
        [Paragraph(f"<b>Status:</b> {purchaseorder.status or '—'}", styles['Normal'])],
    ]
    po_info_table = Table(po_data, colWidths=[main_table_width / 2])
    po_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), bg_color),
        ('TEXTCOLOR', (0, 0), (0, 0), white),
    ]))

    supplier_data = [
        [Paragraph('Supplier Information', styles['SectionHeader'])],
        [Paragraph(f"<b>Name:</b> {purchaseorder.supplier_name or '—'}", styles['Normal'])],
        [Paragraph(f"<b>Code:</b> {purchaseorder.supplier_code or '—'}", styles['Normal'])],
        [Paragraph(f"<b>Purchaser:</b> {purchaseorder.salesman_name or '—'}", styles['Normal'])],
    ]
    supplier_info_table = Table(supplier_data, colWidths=[main_table_width / 2])
    supplier_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), bg_color),
        ('TEXTCOLOR', (0, 0), (0, 0), white),
    ]))

    info_table = Table([[po_info_table, supplier_info_table]], colWidths=[main_table_width / 2, main_table_width / 2])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.2 * inch))

    def _to_decimal(x):
        if x is None:
            return Decimal('0')
        if isinstance(x, Decimal):
            return x
        try:
            return Decimal(str(x))
        except Exception:
            return Decimal('0')

    if show_price:
        items_header = ['#', 'Item No.', 'Description', 'Qty', 'Unit Price', 'Total']
        col_widths_items = [
            main_table_width * 0.05, main_table_width * 0.15, main_table_width * 0.43,
            main_table_width * 0.07, main_table_width * 0.15, main_table_width * 0.15
        ]
    else:
        items_header = ['#', 'Item No.', 'Description', 'Qty']
        col_widths_items = [
            main_table_width * 0.05, main_table_width * 0.15, main_table_width * 0.60,
            main_table_width * 0.20
        ]
    items_data = [items_header]
    subtotal = Decimal('0')
    for idx, it in enumerate(items_qs, 1):
        qty = _to_decimal(it.quantity)
        price = _to_decimal(it.price)
        row_total = _to_decimal(it.row_total) if it.row_total is not None else (qty * price)
        if (price == 0 or price is None) and qty:
            try:
                price = (row_total / qty).quantize(Decimal("0.01"))
            except Exception:
                price = Decimal("0")
        subtotal += row_total
        desc_para = Paragraph(it.description or '—', styles['ItemDescription'])
        if show_price:
            items_data.append([
                str(idx),
                it.item_no or '—',
                desc_para,
                f"{qty.normalize():f}".rstrip('0').rstrip('.') if qty else "0",
                f"AED {price:,.2f}",
                f"AED {row_total:,.2f}",
            ])
        else:
            items_data.append([
                str(idx),
                it.item_no or '—',
                desc_para,
                f"{qty.normalize():f}".rstrip('0').rstrip('.') if qty else "0",
            ])

    items_table = Table(
        items_data,
        colWidths=col_widths_items,
        repeatRows=1
    )
    style_items = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), bg_color),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#F0F7F4'), white]),
        ('ALIGN', (0, 1), (1, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
    ])
    if show_price:
        style_items.add('ALIGN', (4, 1), (-1, -1), 'RIGHT')
    items_table.setStyle(style_items)
    elements.append(items_table)
    elements.append(Spacer(1, 0.1 * inch))

    if show_price:
        stored_row_total_sum = _to_decimal(getattr(purchaseorder, 'row_total_sum', None))
        doc_total = (stored_row_total_sum if stored_row_total_sum else subtotal).quantize(Decimal("0.01"))
        vat_sum_val = _to_decimal(getattr(purchaseorder, 'vat_sum', None))
        grand_total_val = (doc_total + vat_sum_val).quantize(Decimal("0.01"))
        summary_data = [
            ['Document Total:', f"AED {doc_total:,.2f}"],
            ['VAT:', f"AED {vat_sum_val:,.2f}"],
            ['Grand Total:', f"AED {grand_total_val:,.2f}"],
        ]
    if show_price:
        summary_table = Table(summary_data, colWidths=[main_table_width * 0.5, main_table_width * 0.5])
        summary_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, -1), (-1, -1), 12),
            ('BACKGROUND', (0, -1), (-1, -1), bg_color),
            ('TEXTCOLOR', (0, -1), (-1, -1), white),
        ]))
        elements.append(KeepTogether(Table([[summary_table]], colWidths=[main_table_width])))
    elements.append(Spacer(1, 0.3 * inch))

    if getattr(purchaseorder, 'closing_remarks', None):
        elements.extend([
            Paragraph("Remarks:", styles['h3']),
            Paragraph(purchaseorder.closing_remarks, styles['Normal']),
            Spacer(1, 0.2 * inch)
        ])

    elements.extend([
        Paragraph("Terms & Conditions:", styles['h3']),
        Paragraph("1. This purchase order is valid for 30 days from the date of issue.", styles['Normal']),
        Paragraph("2. Delivery timelines to be confirmed upon order confirmation.", styles['Normal']),
        Paragraph("3. System-generated document.", styles['Normal']),
    ])

    doc.multiBuild(elements)
    pdf = buffer.getvalue()
    buffer.close()
    response.write(pdf)
    return response


@login_required
def export_sap_purchaseorder_open_items_pdf(request, po_number):
    """Export only OPEN line items for a single Purchase Order. Price columns only for admin."""
    purchaseorder = get_object_or_404(SAPPurchaseOrder, po_number=po_number)
    items_qs = purchaseorder.items.all().filter(_open_row_status_q_po()).order_by("id")
    show_price = request.user.is_staff or request.user.is_superuser

    response = HttpResponse(content_type="application/pdf")
    date_str = purchaseorder.posting_date.strftime("%Y%m%d") if purchaseorder.posting_date else "NA"
    filename = f"Open_PurchaseOrder_{purchaseorder.po_number}_{date_str}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    doc = SimpleDocTemplate(
        response,
        pagesize=landscape(A4),
        rightMargin=20,
        leftMargin=20,
        topMargin=24,
        bottomMargin=18,
    )
    styles_local = getSampleStyleSheet()
    normal_style = styles_local["Normal"]
    normal_style.fontSize = 8
    elements = []

    if show_price:
        headers = ["Date", "PO No.", "LPO", "Supplier", "Item No", "Description", "Total PO", "Open Qty", "Open Amt"]
        col_widths = [50, 60, 80, 140, 80, 160, 70, 55, 70]
    else:
        headers = ["Date", "PO No.", "LPO", "Supplier", "Item No", "Description", "Open Qty"]
        col_widths = [50, 60, 80, 140, 80, 180, 55]
    data = [headers]

    def _fmt_date(d):
        return d.strftime("%d/%m/%Y") if d else "-"

    def _d(x):
        if x is None:
            return Decimal("0")
        if isinstance(x, Decimal):
            return x
        try:
            return Decimal(str(x))
        except Exception:
            return Decimal("0")

    for it in items_qs:
        qty = _d(it.quantity)
        open_qty = _d(it.remaining_open_quantity)
        row_total = _d(it.row_total)
        pending = _d(it.pending_amount)

        supplier_cell = Paragraph((purchaseorder.supplier_name or "-")[:45], normal_style)
        desc_cell = Paragraph((it.description or "-")[:55], normal_style)
        lpo_cell = Paragraph((purchaseorder.bp_reference_no or "-")[:25], normal_style)

        if show_price:
            data.append([
                _fmt_date(purchaseorder.posting_date),
                purchaseorder.po_number,
                lpo_cell,
                supplier_cell,
                it.item_no or "-",
                desc_cell,
                f"AED {row_total:,.2f}",
                f"{open_qty.normalize():f}".rstrip('0').rstrip('.'),
                f"AED {pending:,.2f}",
            ])
        else:
            data.append([
                _fmt_date(purchaseorder.posting_date),
                purchaseorder.po_number,
                lpo_cell,
                supplier_cell,
                it.item_no or "-",
                desc_cell,
                f"{open_qty.normalize():f}".rstrip('0').rstrip('.'),
            ])

    from reportlab.lib import colors
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#2C5530")),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#F0F7F4"), white]),
    ]))
    elements.append(t)
    doc.build(elements)
    return response


def _apply_purchaseorder_list_filters(qs, request):
    """Apply same filters as purchaseorder_list / purchaseorder_search (for reuse in export)."""
    q = request.GET.get('q', '').strip()
    item_filter = request.GET.get('item', '').strip()
    firm_filter = request.GET.getlist('firm')
    purchaser_filter = request.GET.getlist('purchaser')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    total_range = request.GET.get('total', '').strip()

    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_codes = set(
                Items.objects.filter(item_firm__in=clean_firms).values_list('item_code', flat=True)
            )
            if firm_item_codes:
                qs = qs.filter(item_no__in=firm_item_codes)
            else:
                qs = qs.none()

    if purchaser_filter:
        clean = [p for p in purchaser_filter if p.strip()]
        if clean:
            qs = qs.filter(purchaseorder__salesman_name__in=clean)

    if item_filter:
        qs = qs.filter(
            Q(item_no__icontains=item_filter) |
            Q(description__icontains=item_filter)
        )

    if total_range:
        if total_range == "0-5000":
            qs = qs.filter(pending_amount__gte=0, pending_amount__lte=5000)
        elif total_range == "5001-10000":
            qs = qs.filter(pending_amount__gte=5001, pending_amount__lte=10000)
        elif total_range == "10001-25000":
            qs = qs.filter(pending_amount__gte=10001, pending_amount__lte=25000)
        elif total_range == "25001-50000":
            qs = qs.filter(pending_amount__gte=25001, pending_amount__lte=50000)
        elif total_range == "50001-100000":
            qs = qs.filter(pending_amount__gte=50001, pending_amount__lte=100000)
        elif total_range == "100000+":
            qs = qs.filter(pending_amount__gt=100000)

    if q:
        if q.isdigit():
            qs = qs.filter(
                Q(purchaseorder__po_number__istartswith=q) |
                Q(item_no__istartswith=q) |
                Q(item_no__icontains=q)
            )
        elif len(q) < 3:
            qs = qs.filter(
                Q(purchaseorder__supplier_name__istartswith=q) |
                Q(purchaseorder__salesman_name__istartswith=q) |
                Q(purchaseorder__supplier_code__istartswith=q) |
                Q(purchaseorder__bp_reference_no__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(purchaseorder__po_number__icontains=q) |
                Q(purchaseorder__supplier_name__icontains=q) |
                Q(purchaseorder__salesman_name__icontains=q) |
                Q(purchaseorder__bp_reference_no__icontains=q) |
                Q(purchaseorder__supplier_code__icontains=q) |
                Q(item_no__icontains=q) |
                Q(description__icontains=q)
            )

    def parse_date(s):
        if not s:
            return None
        try:
            if len(s) == 7:
                return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date:
        qs = qs.filter(purchaseorder__posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(purchaseorder__posting_date__lte=end_date)

    return qs.order_by('-purchaseorder__posting_date', '-purchaseorder__po_number', 'line_no')


@login_required
def export_purchaseorder_list_excel(request):
    """
    Export filtered item-level list of Open Purchase Orders to Excel.
    Same structure as Combined Report Export: DataFrame -> Excel with proper column headers.
    Respects all filters: q, item, firm, purchaser, start/end date, total range.
    Price columns (Price, Pending Amount, Row Total) only for admin when show_price=1.
    """
    qs = _purchaseorder_items_qs()
    qs = _apply_purchaseorder_list_filters(qs, request)

    is_admin = request.user.is_staff or request.user.is_superuser
    show_price = is_admin and request.GET.get('show_price') == '1'

    data = []
    for item in qs:
        po = item.purchaseorder
        row = {
            'Doc No': po.po_number or '',
            'Status': 'Open',
            'Posting Date': po.posting_date.strftime('%Y-%m-%d') if po.posting_date else '',
            'Supplier Code': po.supplier_code or '',
            'Supplier Name': po.supplier_name or '',
            'LPO Reference': po.bp_reference_no or '',
            'Purchaser': po.salesman_name or '',
            'Item No': item.item_no or '',
            'Description': item.description or '',
            'Quantity': float(item.quantity or 0),
        }
        if show_price:
            row['Price'] = float(item.price or 0)
            row['Pending Amount'] = float(item.pending_amount or 0)
            row['Row Total'] = float(item.row_total or 0)
        data.append(row)

    df = pd.DataFrame(data)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Open Purchase Orders')

        worksheet = writer.sheets['Open Purchase Orders']

        from openpyxl.utils import get_column_letter
        for idx, col in enumerate(df.columns, 1):
            col_max = int(df[col].astype(str).map(len).max()) if len(df) > 0 else 0
            max_length = max(col_max, len(str(col)))
            worksheet.column_dimensions[get_column_letter(idx)].width = min(max_length + 2, 50)

    output.seek(0)

    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f'open_purchase_orders_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    return response
