from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404, JsonResponse
from django.db.models import Q, Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db import transaction
from django.template.loader import render_to_string
from datetime import datetime
from decimal import Decimal
from io import BytesIO
import pandas as pd
import os

# PDF imports
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, KeepTogether, SimpleDocTemplate
from reportlab.lib.styles import getSampleStyleSheet
from django.conf import settings

# Import models
from .models import SAPSalesorder, SAPSalesorderItem

# Import shared utilities
from .views import get_stock_costs, SALES_USER_MAP
from .views_quotation import QuotationPDFTemplate, styles


# Map usernames -> the exact salesman_name values they are allowed to see.
# Use lowercase keys for usernames.
# This is the same map used for quotations
SALES_USER_MAP_SO = SALES_USER_MAP


def salesman_scope_q_salesorder(user: "User") -> Q:
    """Return a Q filter limiting SAPSalesorder by salesman_name for non-staff users."""
    if user.is_superuser or (hasattr(user, 'role') and user.role.role == "Admin"):
        return Q()  # no restriction

    uname = (user.username or "").strip().lower()
    names = SALES_USER_MAP_SO.get(uname)
    if names:
        q = Q()  # cleaner: start empty
        for n in names:
            q |= Q(salesman_name__iexact=n)
        return q

    # Sensible fallback if no explicit mapping:
    # match username token inside salesman_name (case-insensitive)
    token = uname.replace(".", " ").strip()
    if token:
        return Q(salesman_name__icontains=token)
    # If nothing to match, return an always-false Q to avoid leaking data
    return Q(pk__in=[])


@login_required
def upload_salesorders(request):
    messages_list = []
    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages_list.append('Please upload an Excel file.')
        else:
            try:
                df = pd.read_excel(excel_file)

                # Ensure expected columns exist
                required_cols = [
                    'Document Internal ID', 'Document Number', 'Posting Date',
                    'Customer/Supplier No.', 'Customer/Supplier Name', 'Sales Employee Name',
                    'Manufacturer Name', 'BP Reference No.', 'Item No.', 'Item/Service Description',
                    'Quantity', 'Price', 'Row Total', 'Document Total', 'Status', 'Bill To'
                ]
                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    messages_list.append(f"Missing columns: {', '.join(missing)}")
                else:
                    # Normalize numeric/text columns
                    def as_str(x):
                        try:
                            # preserve as string (e.g., to keep leading zeros)
                            return str(x).strip()
                        except Exception:
                            return ''

                    def to_decimal(x):
                        if pd.isna(x):
                            return None
                        try:
                            return Decimal(str(x).replace(',', '').strip())
                        except Exception:
                            return None

                    # Convert posting date
                    def parse_date(val):
                        if pd.isna(val):
                            return None
                        if isinstance(val, datetime):
                            return val.date()
                        s = str(val).strip()
                        for fmt in ["%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]:
                            try:
                                return datetime.strptime(s, fmt).date()
                            except ValueError:
                                continue
                        return None

                    # Group by Document Number to create header + items
                    for doc_no, grp in df.groupby('Document Number'):
                        so_number = as_str(doc_no)
                        first = grp.iloc[0]

                        salesorder, _ = SAPSalesorder.objects.update_or_create(
                            so_number=so_number,
                            defaults={
                                'internal_number': as_str(first['Document Internal ID']),
                                'posting_date': parse_date(first['Posting Date']),
                                'customer_code': as_str(first['Customer/Supplier No.']),
                                'customer_name': as_str(first['Customer/Supplier Name']),
                                'salesman_name': as_str(first['Sales Employee Name']),
                                'brand': as_str(first['Manufacturer Name']),
                                'bp_reference_no': as_str(first['BP Reference No.']),
                                'document_total': to_decimal(first['Document Total']),
                                'status': as_str(first['Status']),
                                'bill_to': as_str(first['Bill To']),
                            }
                        )

                        # Refresh items: remove old, add new
                        salesorder.items.all().delete()
                        items_to_create = []
                        for _, row in grp.iterrows():
                            items_to_create.append(SAPSalesorderItem(
                                salesorder=salesorder,
                                item_no=as_str(row['Item No.']),
                                description=as_str(row['Item/Service Description']),
                                quantity=to_decimal(row['Quantity']) or Decimal('0'),
                                price=to_decimal(row['Price']) or Decimal('0'),
                                row_total=to_decimal(row['Row Total'])
                            ))
                        if items_to_create:
                            SAPSalesorderItem.objects.bulk_create(items_to_create)

                    return redirect('salesorder_list')

            except Exception as e:
                messages_list.append(f"Error processing Excel file: {str(e)}")

    return render(request, 'salesorders/upload_salesorders.html', {
        'messages': messages_list
    })


# =====================
# Salesorder: List
# =====================
@login_required
def salesorder_list(request):
    # Scope by logged-in user
    qs = SAPSalesorder.objects.all().filter(salesman_scope_q_salesorder(request.user))

    # Filters
    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')  # Gets ['Name1', 'Name2']

    # Apply List Filter
    if salesmen_filter:
        # Filter out empty strings
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            qs = qs.filter(salesman_name__in=clean_salesmen)
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    status = request.GET.get('status', '').strip()
    total_range = request.GET.get('total', '').strip()
    remarks_filter = request.GET.get('remarks', '').strip()

    if total_range:
        if total_range == "0-5000":
            qs = qs.filter(document_total__gte=0, document_total__lte=5000)
        elif total_range == "5001-10000":
            qs = qs.filter(document_total__gte=5001, document_total__lte=10000)
        elif total_range == "10001-25000":
            qs = qs.filter(document_total__gte=10001, document_total__lte=25000)
        elif total_range == "25001-50000":
            qs = qs.filter(document_total__gte=25001, document_total__lte=50000)
        elif total_range == "50001-100000":
            qs = qs.filter(document_total__gte=50001, document_total__lte=100000)
        elif total_range == "100000+":
            qs = qs.filter(document_total__gt=100000)

    if remarks_filter == "YES":
        qs = qs.filter(remarks__isnull=False).exclude(remarks__exact="")
    elif remarks_filter == "NO":
        qs = qs.filter(Q(remarks__isnull=True) | Q(remarks__exact=""))

    if q:
        if q.isdigit():
            qs = qs.filter(so_number__istartswith=q)
        elif len(q) < 3:
            qs = qs.filter(
                Q(customer_name__istartswith=q) |
                Q(salesman_name__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(so_number__icontains=q) |
                Q(customer_name__icontains=q) |
                Q(salesman_name__icontains=q)
            )

    # Status filter
    if status:
        qs = qs.filter(status__iexact=status)

    # Parse dates (YYYY-MM or YYYY-MM-DD)
    def parse_date(s):
        if not s:
            return None
        try:
            if len(s) == 7:  # YYYY-MM
                return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None
    qs_for_years = qs.all()
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    # Calculate totals
    grand_total_agg = qs.aggregate(
        total=Coalesce(Sum('document_total'), Value(0, output_field=DecimalField()))
    )
    total_value = grand_total_agg['total']

    # Calculate Years from 'qs_for_years' (Respects Salesman/Status, IGNORES Date)
    yearly_agg = qs_for_years.aggregate(
        total_2025=Coalesce(Sum('document_total', filter=Q(posting_date__year=2025)), Value(0, output_field=DecimalField())),
        total_2026=Coalesce(Sum('document_total', filter=Q(posting_date__year=2026)), Value(0, output_field=DecimalField())),
    )
    total_2025 = yearly_agg['total_2025']
    total_2026 = yearly_agg['total_2026']

    qs = qs.order_by('-posting_date', '-created_at')

    # Pagination
    try:
        page_size = int(request.GET.get('page_size', 100))
    except ValueError:
        page_size = 20
    page_size = max(5, min(page_size, 100))
    paginator = Paginator(qs, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Distinct salesmen list (restricted to the same scope)
    salesmen = (
        SAPSalesorder.objects.filter(salesman_scope_q_salesorder(request.user))
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
        .order_by('salesman_name')
    )

    return render(request, 'salesorders/salesorder_list.html', {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'total_2025': total_2025,
        'total_2026': total_2026,
        'salesmen': salesmen,
        'total_value': total_value,
        'filters': {
            'q': q,
            'salesmen_filter': salesmen_filter,
            'status': status,
            'start': start,
            'end': end,
            'page_size': page_size,
            'total': total_range,
            'remarks': remarks_filter,
        }
    })


@login_required
def salesorder_detail(request, so_number):
    salesorder = get_object_or_404(SAPSalesorder, so_number=so_number)

    # Enforce scope for non-staff users
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Salesorder not found")

    # Get items
    items = salesorder.items.all().order_by('id')

    # Calculate estimated cost and profit
    stock_map = get_stock_costs()
    total_estimated_cost = 0.0

    # We iterate over items to calculate cost based on the API map
    for item in items:
        # Match item.item_no with the API's item_code
        item_code = str(item.item_no).strip()

        # Get unit cost from map, default to 0.0 if not found
        unit_cost = stock_map.get(item_code, 0.0)

        # Calculate row cost (Unit Cost * Quantity)
        # Convert Decimal quantity to float for calculation
        qty = float(item.quantity)
        total_estimated_cost += (unit_cost * qty)

    # Calculate Profit/Margin
    # Convert document_total to float for math
    doc_total = float(salesorder.document_total or 0)
    total_profit = doc_total - total_estimated_cost

    context = {
        'salesorder': salesorder,
        'items': items,
        'total_cost': total_estimated_cost,
        'total_profit': total_profit,
    }

    return render(request, 'salesorders/salesorder_detail.html', context)


# =====================
# Salesorder: AJAX Search (rows + pagination HTML)
# =====================
@login_required
def salesorder_search(request):
    # Scope by logged-in user
    qs = SAPSalesorder.objects.all().filter(salesman_scope_q_salesorder(request.user))

    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')

    # Logic
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            qs = qs.filter(salesman_name__in=clean_salesmen)

    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    status = request.GET.get('status', '').strip()
    total_range = request.GET.get('total', '').strip()
    remarks_filter = request.GET.get('remarks', '').strip()

    # Existing filters
    if q:
        if q.isdigit():
            qs = qs.filter(so_number__istartswith=q)
        elif len(q) < 3:
            qs = qs.filter(
                Q(customer_name__istartswith=q) |
                Q(salesman_name__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(so_number__icontains=q) |
                Q(customer_name__icontains=q) |
                Q(salesman_name__icontains=q)
            )

    if status:
        qs = qs.filter(status__iexact=status)

    # Remarks filter
    if remarks_filter == "YES":
        qs = qs.filter(remarks__isnull=False).exclude(remarks__exact="")
    elif remarks_filter == "NO":
        qs = qs.filter(Q(remarks__isnull=True) | Q(remarks__exact=""))

    # Date filter
    def parse_date(s):
        if not s:
            return None
        try:
            if len(s) == 7:  # YYYY-MM
                return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    # Total range filter
    if total_range:
        if total_range == "0-5000":
            qs = qs.filter(document_total__gte=0, document_total__lte=5000)
        elif total_range == "5001-10000":
            qs = qs.filter(document_total__gte=5001, document_total__lte=10000)
        elif total_range == "10001-25000":
            qs = qs.filter(document_total__gte=10001, document_total__lte=25000)
        elif total_range == "25001-50000":
            qs = qs.filter(document_total__gte=25001, document_total__lte=50000)
        elif total_range == "50001-100000":
            qs = qs.filter(document_total__gte=50001, document_total__lte=100000)
        elif total_range == "100000+":
            qs = qs.filter(document_total__gt=100000)

    # Total value (sum of document_total on FILTERED qs)
    total_value = qs.aggregate(
        total=Coalesce(Sum('document_total'), Value(0, output_field=DecimalField()))
    )['total']

    # Order + Pagination
    qs = qs.order_by('-posting_date', '-created_at')

    try:
        page_size = int(request.GET.get('page_size', 20))
    except ValueError:
        page_size = 20
    page_size = max(5, min(page_size, 100))
    paginator = Paginator(qs, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    rows_html = render_to_string('salesorders/_salesorder_rows.html', {
        'page_obj': page_obj
    }, request=request)

    pagination_html = render_to_string('salesorders/_pagination.html', {
        'page_obj': page_obj
    }, request=request)

    return JsonResponse({
        'rows_html': rows_html,
        'pagination_html': pagination_html,
        'count': paginator.count,
        'total_value': float(total_value or 0),
    })


@login_required
def export_sap_salesorder_pdf(request, so_number):
    """
    Generate a PDF for SAPSalesorder using the same template as quotations.
    """
    # Fetch salesorder
    salesorder = get_object_or_404(SAPSalesorder, so_number=so_number)
    
    # Enforce scope for non-staff users
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Salesorder not found")
    
    items_qs = salesorder.items.all().order_by('id')

    # Prepare HTTP response
    response = HttpResponse(content_type='application/pdf')
    date_str = salesorder.posting_date.strftime('%Y%m%d') if salesorder.posting_date else 'NA'
    filename = f"SAP_Salesorder_{salesorder.so_number}_{date_str}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    buffer = BytesIO()

    # Define default config (Junaid Settings)
    company_config = {
        'name': "Junaid Sanitary & Electrical Trading LLC",
        'address': "Dubai Investment Parks 2, Dubai, UAE",
        'contact': "Email: sales@junaid.ae | Phone: +97142367723",
        'logo_url': "https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp",
        'local_logo_path': os.path.join(settings.BASE_DIR, 'static', 'images', 'footer-logo.png.webp')
    }

    # Default Green Theme
    theme_config = {'primary': HexColor('#2C5530')}

    # Initialize template with config
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

    # Title
    elements.append(Spacer(1, -1.3*inch))

    title_table = Table(
        [[Paragraph('SALES ORDER', styles['MainTitle'])]],
        colWidths=[7.5*inch]
    )
    title_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TEXTCOLOR', (0, 0), (-1, -1), theme_config['primary']),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 0.1*inch))

    # Two-column info (Salesorder / Customer)
    main_table_width = 7.2 * inch

    salesorder_data = [
        [Paragraph('Salesorder Details', styles['SectionHeader'])],
        [Paragraph(f"<b>Number:</b> {salesorder.so_number}", styles['Normal'])],
        [Paragraph(f"<b>Date:</b> {salesorder.posting_date or '-'}", styles['Normal'])],
        [Paragraph(f"<b>BP Ref No:</b> {salesorder.bp_reference_no or '—'}", styles['Normal'])],
    ]

    bg_color = theme_config['primary']

    salesorder_info_table = Table(salesorder_data, colWidths=[main_table_width / 2])
    salesorder_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), bg_color),
        ('TEXTCOLOR', (0, 0), (0, 0), white),
    ]))

    customer_data = [
        [Paragraph('Customer Information', styles['SectionHeader'])],
        [Paragraph(f"<b>Name:</b> {salesorder.customer_name or '—'}", styles['Normal'])],
        [Paragraph(f"<b>Code:</b> {salesorder.customer_code or '—'}", styles['Normal'])],
        [Paragraph(f"<b>Salesman:</b> {salesorder.salesman_name or '—'}", styles['Normal'])],
    ]

    customer_info_table = Table(customer_data, colWidths=[main_table_width / 2])
    customer_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), bg_color),
        ('TEXTCOLOR', (0, 0), (0, 0), white),
    ]))

    info_table = Table([[salesorder_info_table, customer_info_table]],
                       colWidths=[main_table_width / 2, main_table_width / 2])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.2 * inch))

    # Items table
    items_header = ['#', 'Item No.', 'Description', 'Qty', 'Unit Price', 'Total']
    items_data = [items_header]

    def _to_decimal(x):
        if x is None:
            return Decimal('0')
        if isinstance(x, Decimal):
            return x
        try:
            return Decimal(str(x))
        except Exception:
            return Decimal('0')

    subtotal = Decimal('0')
    for idx, it in enumerate(items_qs, 1):
        qty = _to_decimal(it.quantity)
        price = _to_decimal(it.price)
        row_total = _to_decimal(it.row_total) if it.row_total is not None else (qty * price)
        subtotal += row_total

        desc_para = Paragraph(it.description or '—', styles['ItemDescription'])

        items_data.append([
            str(idx),
            it.item_no or '—',
            desc_para,
            f"{qty.normalize():f}".rstrip('0').rstrip('.') if qty else "0",
            f"AED {price:,.2f}",
            f"AED {row_total:,.2f}",
        ])

    items_table = Table(
        items_data,
        colWidths=[
            main_table_width * 0.05,
            main_table_width * 0.15,
            main_table_width * 0.43,
            main_table_width * 0.07,
            main_table_width * 0.15,
            main_table_width * 0.15
        ],
        repeatRows=1
    )
    items_table.setStyle(TableStyle([
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
        ('ALIGN', (4, 1), (-1, -1), 'RIGHT'),
    ]))

    elements.append(items_table)
    elements.append(Spacer(1, 0.1 * inch))

    # Summary (VAT 5%)
    tax_rate = Decimal('0.05')
    tax_amount = (subtotal * tax_rate).quantize(Decimal('0.01'))
    doc_total = _to_decimal(salesorder.document_total)
    grand_total = doc_total if doc_total else (subtotal + tax_amount)

    summary_data = [
        ['Subtotal:', f"AED {subtotal:,.2f}"],
        [f'VAT ({(tax_rate*100):.0f}%):', f"AED {tax_amount:,.2f}"],
        ['Grand Total:', f"AED {grand_total:,.2f}"],
    ]
    summary_table = Table(summary_data, colWidths=[main_table_width * 0.5, main_table_width * 0.5])
    summary_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('FONTNAME', (0, 2), (-1, 2), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 2), (-1, 2), 12),
        ('BACKGROUND', (0, 2), (-1, 2), bg_color),
        ('TEXTCOLOR', (0, 2), (-1, 2), white),
    ]))

    summary_wrapper = Table([[summary_table]], colWidths=[main_table_width])
    summary_wrapper.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(KeepTogether(summary_wrapper))
    elements.append(Spacer(1, 0.3 * inch))

    # Optional: remarks / terms
    if getattr(salesorder, 'remarks', None):
        elements.extend([
            Paragraph("Remarks:", styles['h3']),
            Paragraph(salesorder.remarks, styles['Normal']),
            Spacer(1, 0.2 * inch)
        ])

    elements.extend([
        Paragraph("Terms & Conditions:", styles['h3']),
        Paragraph("1. This sales order is valid for 30 days from the date of issue.", styles['Normal']),
        Paragraph("2. Prices are subject to change after the validity period.", styles['Normal']),
        Paragraph("3. Delivery timelines to be confirmed upon order confirmation.", styles['Normal']),
        Paragraph("4. System-generated document.", styles['Normal']),
    ])

    # Build + return
    doc.multiBuild(elements)
    pdf = buffer.getvalue()
    buffer.close()
    response.write(pdf)
    return response


@login_required
def export_salesorder_list_pdf(request):
    """
    Exports the filtered list of salesorders to a PDF report.
    Respects: q, salesman, start/end date, status, total range, remarks.
    """
    # 1. APPLY FILTERS (Exact copy from salesorder_list)
    qs = SAPSalesorder.objects.all().filter(salesman_scope_q_salesorder(request.user))
    
    q = request.GET.get('q', '').strip()
    salesman = request.GET.get('salesman', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    status = request.GET.get('status', '').strip()
    total_range = request.GET.get('total', '').strip()
    remarks_filter = request.GET.get('remarks', '').strip()

    # Apply Total Range Filter
    if total_range:
        if total_range == "0-5000": qs = qs.filter(document_total__gte=0, document_total__lte=5000)
        elif total_range == "5001-10000": qs = qs.filter(document_total__gte=5001, document_total__lte=10000)
        elif total_range == "10001-25000": qs = qs.filter(document_total__gte=10001, document_total__lte=25000)
        elif total_range == "25001-50000": qs = qs.filter(document_total__gte=25001, document_total__lte=50000)
        elif total_range == "50001-100000": qs = qs.filter(document_total__gte=50001, document_total__lte=100000)
        elif total_range == "100000+": qs = qs.filter(document_total__gt=100000)

    # Apply Remarks Filter
    if remarks_filter == "YES":
        qs = qs.filter(remarks__isnull=False).exclude(remarks__exact="")
    elif remarks_filter == "NO":
        qs = qs.filter(Q(remarks__isnull=True) | Q(remarks__exact=""))

    # Apply Search (q)
    if q:
        if q.isdigit():
            qs = qs.filter(so_number__istartswith=q)
        elif len(q) < 3:
            qs = qs.filter(Q(customer_name__istartswith=q) | Q(salesman_name__istartswith=q))
        else:
            qs = qs.filter(Q(so_number__icontains=q) | Q(customer_name__icontains=q) | Q(salesman_name__icontains=q))

    if salesman:
        qs = qs.filter(salesman_name__iexact=salesman)
    if status:
        qs = qs.filter(status__iexact=status)

    # Apply Dates
    def parse_date(s):
        if not s: return None
        try:
            if len(s) == 7: return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError: return None

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date: qs = qs.filter(posting_date__gte=start_date)
    if end_date: qs = qs.filter(posting_date__lte=end_date)

    # Ordering
    qs = qs.order_by('-posting_date', '-created_at')

    # Calculate Total Value of Report
    total_value = qs.aggregate(
        total=Coalesce(Sum('document_total'), Value(0, output_field=DecimalField()))
    )['total']

    # --- 2. GENERATE PDF ---
    response = HttpResponse(content_type='application/pdf')
    filename = f"Salesorder_Report_{datetime.now().strftime('%Y%m%d')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    # Landscape A4 because lists are wide
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    
    doc = SimpleDocTemplate(response, pagesize=landscape(A4), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()

    # Title
    title_text = "Salesorder Sales Report"
    if start and end:
        title_text += f" ({start} to {end})"
    elements.append(Paragraph(title_text, styles['Title']))
    elements.append(Spacer(1, 20))

    # Table Header
    headers = ['Date', 'SO #', 'Customer Name', 'Salesman', 'Status', 'Total (AED)']
    data = [headers]

    # Table Rows
    for item in qs:
        doc_total = item.document_total if item.document_total else 0
        date_str = item.posting_date.strftime('%Y-%m-%d') if item.posting_date else "-"
        
        row = [
            date_str,
            item.so_number,
            Paragraph(item.customer_name[:35] + '...' if len(item.customer_name or '') > 35 else (item.customer_name or ''), styles['Normal']),
            Paragraph(item.salesman_name or '-', styles['Normal']),
            item.status or '-',
            f"{doc_total:,.2f}"
        ]
        data.append(row)

    # Grand Total Row
    data.append(['', '', '', '', 'GRAND TOTAL:', f"{total_value:,.2f}"])

    # Table Styling
    # Calculate column widths (Landscape A4 width approx 840 points)
    col_widths = [70, 70, 280, 150, 80, 80]
    
    table = Table(data, colWidths=col_widths, repeatRows=1)
    
    style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2C5530')), # Header Color
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        
        # Data Rows
        ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (-1, 1), (-1, -1), 'RIGHT'), # Right align totals
        
        # Grand Total Row styling
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ('TEXTCOLOR', (0, -1), (-1, -1), colors.black),
    ])
    
    table.setStyle(style)
    elements.append(table)

    # Footer/Summary
    elements.append(Spacer(1, 20))
    elements.append(Paragraph(f"Total Records: {qs.count()}", styles['Normal']))

    doc.build(elements)
    return response


@login_required
@require_POST
def salesorder_update_remarks(request, so_number):
    salesorder = get_object_or_404(SAPSalesorder, so_number=so_number)

    # Enforce the same scope rules as detail view
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Salesorder not found")

    # Update remarks
    new_remarks = (request.POST.get("remarks") or "").strip()
    salesorder.remarks = new_remarks
    salesorder.save(update_fields=["remarks"])

    messages.success(request, "Remarks updated.")
    return redirect("salesorder_detail", so_number=salesorder.so_number)
