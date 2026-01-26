from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404, JsonResponse
from django.db.models import Q, Sum, Value, DecimalField, Exists, OuterRef, Case, When, CharField
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
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, KeepTogether, SimpleDocTemplate, Image, BaseDocTemplate, Frame, PageTemplate
from reportlab.graphics.shapes import Drawing, Line
from reportlab.lib.styles import getSampleStyleSheet
from django.conf import settings
import requests
import logging
import json

logger = logging.getLogger(__name__)

# Import models
from .models import SAPSalesorder, SAPSalesorderItem, SAPProformaInvoice, SAPProformaInvoiceLine, ProformaInvoiceLog, SAPQuotation

# Import shared utilities
from .views import get_stock_costs, SALES_USER_MAP, salesman_scope_q
from .views_quotation import QuotationPDFTemplate, styles
from .utils import get_client_ip, label_network, parse_device_info
from .api_client import SAPAPIClient


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


def _open_row_status_q(prefix: str = "") -> Q:
    """
    Return a Q object matching "open" line statuses.
    Accepts both SAP styles: "Open" and "O" (case-insensitive).
    """
    field = f"{prefix}row_status"
    return (
        Q(**{f"{field}__iexact": "open"})
        | Q(**{f"{field}__iexact": "o"})
        | Q(**{f"{field}__iexact": "OPEN"})
        | Q(**{f"{field}__iexact": "O"})
    )


@login_required
def export_sap_salesorder_open_items_pdf(request, so_number):
    """
    Customer-ready PDF: export ONLY OPEN line items for a single SAP Salesorder.
    LPO is BP Reference No.
    Columns: Date, Doc No., LPO, Customer, Item No, Description, Total SO, Open, Avail, DIP
    """
    salesorder = get_object_or_404(SAPSalesorder, so_number=so_number)

    # Enforce same scope rules as detail view
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Salesorder not found")

    items_qs = (
        salesorder.items.all()
        .filter(_open_row_status_q())
        .order_by("id")
    )

    response = HttpResponse(content_type="application/pdf")
    date_str = salesorder.posting_date.strftime("%Y%m%d") if salesorder.posting_date else "NA"
    filename = f"Open_SalesOrder_{salesorder.so_number}_{date_str}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    # Use stable margins + scale column widths to available width
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

    # Header: logo + title
    logo_url = "https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp"
    logo_flowable = None
    try:
        r = requests.get(logo_url, timeout=6)
        r.raise_for_status()
        logo_flowable = Image(BytesIO(r.content), width=120, height=38)
    except Exception:
        logo_flowable = None

    title = Paragraph("Open Sales Orders - so", styles_local["Title"])
    header_tbl = Table(
        [[logo_flowable or "", title]],
        colWidths=[140, None],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 10))

    # Build table rows
    headers = ["Date", "Doc No.", "LPO", "Customer", "Item No", "Description", "Total SO", "Open", "Avail", "DIP"]
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

    total_qty = Decimal("0")
    total_open = Decimal("0")
    total_avail = Decimal("0")
    total_dip = Decimal("0")

    for it in items_qs:
        qty = _d(it.quantity)
        open_qty = _d(it.remaining_open_quantity)
        avail = _d(it.total_available_stock)
        dip = _d(it.dip_warehouse_stock)

        total_qty += qty
        total_open += open_qty
        total_avail += avail
        total_dip += dip

        cust_cell = Paragraph((salesorder.customer_name or "-")[:45], normal_style)
        desc_cell = Paragraph((it.description or "-")[:55], normal_style)
        lpo_cell = Paragraph((salesorder.bp_reference_no or "-")[:25], normal_style)

        data.append([
            _fmt_date(salesorder.posting_date),
            salesorder.so_number,
            lpo_cell,
            cust_cell,
            it.item_no or "-",
            desc_cell,
            f"{qty:,.0f}",
            f"{open_qty:,.0f}",
            f"{avail:,.0f}",
            f"{dip:,.0f}",
        ])

    # Totals row (matches sample screenshot style)
    data.append(["", "", "", "", "", "TOTALS:", f"{total_qty:,.0f}", f"{total_open:,.0f}", f"{total_avail:,.0f}", f"{total_dip:,.0f}"])

    # Column widths based on the old working PDF, scaled to the available page width.
    base_widths = [60, 70, 80, 140, 65, 180, 70, 70, 60, 60]  # give numeric cols extra space
    page_w, _page_h = landscape(A4)
    avail_w = page_w - doc.leftMargin - doc.rightMargin
    scale = avail_w / float(sum(base_widths))
    col_widths = [w * scale for w in base_widths]
    # Guard against float rounding overflow
    col_widths[-1] = avail_w - sum(col_widths[:-1])

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.hAlign = "LEFT"

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),

        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),

        ("ALIGN", (6, 1), (-1, -1), "RIGHT"),

        # Totals row
        ("BACKGROUND", (0, -1), (-1, -1), colors.lightgrey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (5, -1), (5, -1), "RIGHT"),
    ]))

    elements.append(table)
    doc.build(elements)
    return response


@login_required
def upload_salesorders(request):
    messages_list = []
    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages_list.append('Please upload a file.')
        else:
            try:
                # Ensure expected columns exist
                required_cols = [
                    'Document Number', 'Posting Date', 'BP Reference No.',
                    'Customer/Supplier No.', 'Customer/Supplier Name',
                    'Row Status', 'Job Type',
                    'Item No.', 'Item/Service Description', 'Manufacture',
                    'Quantity', 'Row Total',
                    'Remaining Open Quantity', 'Pending Amount',
                    'Sales Employee',
                    'Total available Stock', 'Dip warehouse stock',
                ]
                
                # Discount is optional
                optional_cols = ['Discount']

                # Excel upload only (as requested)
                df = pd.read_excel(excel_file)

                # Filter to only process HO customers (customer code starts with "HO")
                if 'Customer/Supplier No.' in df.columns:
                    # Normalize customer code for filtering
                    df['Customer/Supplier No.'] = df['Customer/Supplier No.'].astype(str).str.strip().str.upper()
                    df = df[df['Customer/Supplier No.'].str.startswith('HO', na=False)].reset_index(drop=True)
                    
                    if len(df) == 0:
                        messages_list.append("No sales orders found with customer codes starting with 'HO'.")
                        return render(request, 'salesorders/upload_salesorders.html', {
                            'messages': messages_list
                        })

                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    messages_list.append(f"Missing columns: {', '.join(missing)}")
                else:
                    # Keep required cols + optional Discount and VAT Number columns if they exist
                    cols_to_keep = required_cols.copy()
                    if "Discount" in df.columns:
                        cols_to_keep.append("Discount")
                    if "VAT Number" in df.columns:
                        cols_to_keep.append("VAT Number")
                    df = df[cols_to_keep].copy()

                    # Normalize strings
                    def _clean_str_series(s: pd.Series) -> pd.Series:
                        s = s.astype("string")
                        s = s.fillna("")
                        s = s.str.strip()
                        # common Excel artifact when numeric IDs become floats
                        s = s.str.replace(r"\.0$", "", regex=True)
                        return s

                    df["so_number"] = _clean_str_series(df["Document Number"])
                    df["customer_code"] = _clean_str_series(df["Customer/Supplier No."])
                    df["customer_name"] = _clean_str_series(df["Customer/Supplier Name"])
                    df["bp_reference_no"] = _clean_str_series(df["BP Reference No."])
                    df["salesman_name"] = _clean_str_series(df["Sales Employee"])
                    df["row_status_norm"] = _clean_str_series(df["Row Status"]).str.upper()
                    df["item_no"] = _clean_str_series(df["Item No."])
                    df["description"] = _clean_str_series(df["Item/Service Description"])
                    df["job_type"] = _clean_str_series(df["Job Type"])
                    df["manufacture"] = _clean_str_series(df["Manufacture"])

                    # Dates
                    df["posting_date"] = pd.to_datetime(df["Posting Date"], errors="coerce", dayfirst=True).dt.date

                    # Numerics
                    def _num(col: str) -> pd.Series:
                        return pd.to_numeric(df[col], errors="coerce")

                    df["quantity_n"] = _num("Quantity").fillna(0)
                    df["row_total_n"] = _num("Row Total").fillna(0)
                    df["remaining_open_quantity_n"] = _num("Remaining Open Quantity")
                    df["pending_amount_n"] = _num("Pending Amount")
                    df["total_available_stock_n"] = _num("Total available Stock")
                    df["dip_warehouse_stock_n"] = _num("Dip warehouse stock")
                    
                    # Discount (optional column) - create before aggregation
                    if "Discount" in df.columns:
                        df["discount_n"] = _num("Discount").fillna(0)
                    else:
                        df["discount_n"] = pd.Series([0] * len(df), dtype=float)  # Create proper Series with zeros

                    df["is_open_line"] = df["row_status_norm"].isin(["OPEN", "O"])

                    # Assign line_no within each SO (1-based, stable order)
                    df["line_no"] = df.groupby("so_number", sort=False).cumcount() + 1

                    # Header aggregation
                    agg_dict = {
                        "posting_date": ("posting_date", "first"),
                        "customer_code": ("customer_code", "first"),
                        "customer_name": ("customer_name", "first"),
                        "bp_reference_no": ("bp_reference_no", "first"),
                        "salesman_name": ("salesman_name", "first"),
                        "discount_percentage": ("discount_n", "first"),  # Get discount from first row (same for all items in SO)
                        "pending_total": ("pending_amount_n", "sum"),
                        "row_total_sum": ("row_total_n", "sum"),
                        "has_open": ("is_open_line", "any"),
                    }
                    
                    # Add VAT Number if it exists in the Excel
                    if "VAT Number" in df.columns:
                        # Normalize VAT Number column (clean strings, handle NaN, remove .0 suffix)
                        df["vat_number_clean"] = _clean_str_series(df["VAT Number"])
                        agg_dict["vat_number"] = ("vat_number_clean", "first")
                    
                    header_df = (
                        df.groupby("so_number", dropna=False)
                        .agg(**agg_dict)
                        .reset_index()
                    )

                    # Convert to Decimal safely
                    def _dec2(x) -> Decimal:
                        try:
                            if x is None or (isinstance(x, float) and pd.isna(x)):
                                return Decimal("0.00")
                            return Decimal(str(x)).quantize(Decimal("0.01"))
                        except Exception:
                            return Decimal("0.00")

                    so_numbers = header_df["so_number"].tolist()

                    with transaction.atomic():
                        # Fetch existing orders by so_number
                        try:
                            existing_map = SAPSalesorder.objects.in_bulk(so_numbers, field_name="so_number")
                        except TypeError:
                            existing_map = {o.so_number: o for o in SAPSalesorder.objects.filter(so_number__in=so_numbers)}

                        to_create = []
                        to_update = []

                        for row in header_df.itertuples(index=False):
                            so_no = row.so_number
                            status_val = "O" if bool(row.has_open) else "C"
                            defaults = {
                                "posting_date": row.posting_date,
                                "customer_code": row.customer_code or "",
                                "customer_name": row.customer_name or "",
                                "bp_reference_no": row.bp_reference_no or "",
                                "salesman_name": row.salesman_name or "",
                                "discount_percentage": _dec2(row.discount_percentage),
                                "document_total": _dec2(row.pending_total),
                                "row_total_sum": _dec2(row.row_total_sum),
                                "status": status_val,
                            }
                            
                            # Add VAT Number if it exists in the row
                            if hasattr(row, 'vat_number') and row.vat_number:
                                vat_str = str(row.vat_number).strip()
                                # Remove "nan" string and empty values
                                if vat_str.lower() in ('nan', 'none', ''):
                                    defaults["vat_number"] = ""
                                else:
                                    # Remove .0 suffix if present
                                    if vat_str.endswith('.0'):
                                        vat_str = vat_str[:-2]
                                    defaults["vat_number"] = vat_str
                            else:
                                defaults["vat_number"] = ""

                            obj = existing_map.get(so_no)
                            if obj is None:
                                to_create.append(SAPSalesorder(so_number=so_no, **defaults))
                            else:
                                for k, v in defaults.items():
                                    setattr(obj, k, v)
                                to_update.append(obj)

                        if to_create:
                            SAPSalesorder.objects.bulk_create(to_create, batch_size=2000)
                        if to_update:
                            update_fields = [
                                "posting_date",
                                "customer_code",
                                "customer_name",
                                "bp_reference_no",
                                "salesman_name",
                                "discount_percentage",
                                "document_total",
                                "row_total_sum",
                                "status",
                            ]
                            # Add vat_number if it exists in the Excel
                            if "VAT Number" in df.columns:
                                update_fields.append("vat_number")
                            
                            SAPSalesorder.objects.bulk_update(
                                to_update,
                                fields=update_fields,
                                batch_size=2000,
                            )

                        # Re-fetch ids for FK mapping (covers bulk_create id assignment variations)
                        order_id_map = dict(
                            SAPSalesorder.objects.filter(so_number__in=so_numbers).values_list("so_number", "id")
                        )

                        # Delete existing items for these salesorders in ONE query
                        SAPSalesorderItem.objects.filter(salesorder__so_number__in=so_numbers).delete()

                        # Build items list + bulk insert
                        items_to_create = []

                        def _dec_any(x) -> Decimal:
                            try:
                                if x is None or (isinstance(x, float) and pd.isna(x)):
                                    return Decimal("0")
                                return Decimal(str(x))
                            except Exception:
                                return Decimal("0")

                        for r in df.itertuples(index=False):
                            so_no = r.so_number
                            so_id = order_id_map.get(so_no)
                            if not so_id:
                                continue
                            items_to_create.append(
                                SAPSalesorderItem(
                                    salesorder_id=so_id,
                                    line_no=int(getattr(r, 'line_no', 1)),
                                    item_no=r.item_no or "",
                                    description=r.description or "",
                                    quantity=_dec_any(r.quantity_n),
                                    price=Decimal("0"),
                                    row_total=_dec_any(r.row_total_n),
                                    row_status=(r.row_status_norm or ""),
                                    job_type=r.job_type or "",
                                    manufacture=r.manufacture or "",
                                    remaining_open_quantity=_dec_any(r.remaining_open_quantity_n),
                                    pending_amount=_dec_any(r.pending_amount_n),
                                    total_available_stock=_dec_any(r.total_available_stock_n),
                                    dip_warehouse_stock=_dec_any(r.dip_warehouse_stock_n),
                                )
                            )

                            if len(items_to_create) >= 10000:
                                SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=20000)
                                items_to_create = []

                        if items_to_create:
                            SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=10000)

                    messages.success(
                        request,
                        f"Imported {len(so_numbers)} sales orders and {len(df)} lines successfully."
                    )
                    return redirect('salesorder_list')

            except Exception as e:
                messages_list.append(f"Error processing Excel file: {str(e)}")

    return render(request, 'salesorders/upload_salesorders.html', {
        'messages': messages_list
    })


# =====================
# Salesorder: API Sync
# =====================
@login_required
def sync_salesorders_from_api(request):
    """Sync sales orders from SAP API"""
    messages_list = []
    sync_stats = {
        'created': 0,
        'updated': 0,
        'closed': 0,
        'total_orders': 0,
        'total_items': 0,
        'api_calls': 0,
        'errors': []
    }
    
    if request.method == 'POST':
        try:
            client = SAPAPIClient()
            days_back = int(request.POST.get('days_back', getattr(settings, 'SAP_SYNC_DAYS_BACK', 3)))
            specific_date = request.POST.get('specific_date', '').strip()
            docnum = request.POST.get('docnum', '').strip()
            
            # Fetch data from API
            all_orders = []
            
            if docnum:
                # Single DocNum query
                orders = client.fetch_salesorders_by_docnum(int(docnum))
                all_orders.extend(orders)
                sync_stats['api_calls'] = 1
            elif specific_date:
                # Single date query
                orders = client.fetch_salesorders_by_date(specific_date)
                all_orders.extend(orders)
                sync_stats['api_calls'] = 1
            else:
                # Default: sync all (open orders + last N days)
                all_orders = client.sync_all_salesorders(days_back=days_back)
                sync_stats['api_calls'] = 1 + days_back  # 1 for open orders + N for days
            
            # Filter by HO customers
            all_orders = client._filter_ho_customers(all_orders)
            
            if not all_orders:
                messages.warning(request, "No sales orders found (after filtering by HO customers).")
                return render(request, 'salesorders/upload_salesorders.html', {
                    'messages': messages_list,
                    'sync_stats': sync_stats
                })
            
            # Map API responses to model format
            mapped_orders = []
            for api_order in all_orders:
                try:
                    mapped = client._map_api_response_to_model(api_order)
                    mapped_orders.append(mapped)
                except Exception as e:
                    logger.error(f"Error mapping order {api_order.get('DocNum')}: {e}")
                    sync_stats['errors'].append(f"Error mapping order {api_order.get('DocNum')}: {str(e)}")
            
            if not mapped_orders:
                messages.error(request, "No orders could be mapped successfully.")
                return render(request, 'salesorders/upload_salesorders.html', {
                    'messages': messages_list,
                    'sync_stats': sync_stats
                })
            
            # Get list of SO numbers from API response (for closing missing orders)
            api_so_numbers = set(mapped['so_number'] for mapped in mapped_orders if mapped.get('so_number'))
            
            # Prepare data for bulk operations
            so_numbers = [m['so_number'] for m in mapped_orders if m.get('so_number')]
            sync_stats['total_orders'] = len(so_numbers)
            
            with transaction.atomic():
                # Fetch existing orders
                try:
                    existing_map = SAPSalesorder.objects.in_bulk(so_numbers, field_name="so_number")
                except TypeError:
                    existing_map = {o.so_number: o for o in SAPSalesorder.objects.filter(so_number__in=so_numbers)}
                
                to_create = []
                to_update = []
                
                def _dec2(x) -> Decimal:
                    try:
                        if x is None or (isinstance(x, float) and pd.isna(x)):
                            return Decimal("0.00")
                        return Decimal(str(x)).quantize(Decimal("0.01"))
                    except Exception:
                        return Decimal("0.00")
                
                # Process each mapped order
                for mapped in mapped_orders:
                    so_no = mapped.get('so_number')
                    if not so_no:
                        continue
                    
                    defaults = {
                        "posting_date": mapped.get('posting_date'),
                        "customer_code": mapped.get('customer_code', ''),
                        "customer_name": mapped.get('customer_name', ''),
                        "bp_reference_no": mapped.get('bp_reference_no', ''),
                        "salesman_name": mapped.get('salesman_name', ''),
                        "discount_percentage": _dec2(mapped.get('discount_percentage', 0)),  # Exact value from API
                        "document_total": _dec2(mapped.get('document_total', 0)),
                        "row_total_sum": _dec2(mapped.get('row_total_sum', 0)),
                        "status": mapped.get('status', 'C'),
                        "vat_number": mapped.get('vat_number', '') or '',  # VAT Number from BusinessPartner.FederalTaxID
                        "customer_address": mapped.get('customer_address', '') or '',  # Address from main API response
                        "customer_phone": mapped.get('customer_phone', '') or '',  # Phone1 from BusinessPartner
                        "is_sap_pi": mapped.get('is_sap_pi', False),  # True if U_PROFORMAINVOICE=Y
                        "last_synced_at": datetime.now(),  # Track sync time
                    }
                    
                    if mapped.get('internal_number'):
                        defaults["internal_number"] = mapped.get('internal_number')
                    
                    obj = existing_map.get(so_no)
                    if obj is None:
                        to_create.append(SAPSalesorder(so_number=so_no, **defaults))
                        sync_stats['created'] += 1
                    else:
                        for k, v in defaults.items():
                            setattr(obj, k, v)
                        to_update.append(obj)
                        sync_stats['updated'] += 1
                
                # Bulk create/update
                if to_create:
                    SAPSalesorder.objects.bulk_create(to_create, batch_size=5000)
                
                if to_update:
                    update_fields = [
                        "posting_date", "customer_code", "customer_name", "bp_reference_no",
                        "salesman_name", "discount_percentage", "document_total", "row_total_sum",
                        "status", "vat_number", "internal_number", "last_synced_at"
                    ]
                    SAPSalesorder.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)
                
                # Re-fetch ids for FK mapping
                order_id_map = dict(
                    SAPSalesorder.objects.filter(so_number__in=so_numbers).values_list("so_number", "id")
                )
                
                # Delete existing items for these salesorders
                SAPSalesorderItem.objects.filter(salesorder__so_number__in=so_numbers).delete()
                
                # Build items list + bulk insert
                items_to_create = []
                
                def _dec_any(x) -> Decimal:
                    try:
                        if x is None or (isinstance(x, float) and pd.isna(x)):
                            return Decimal("0")
                        return Decimal(str(x))
                    except Exception:
                        return Decimal("0")
                
                for mapped in mapped_orders:
                    so_no = mapped.get('so_number')
                    so_id = order_id_map.get(so_no)
                    if not so_id:
                        continue
                    
                    for item_data in mapped.get('items', []):
                        items_to_create.append(
                            SAPSalesorderItem(
                                salesorder_id=so_id,
                                line_no=item_data.get('line_no', 1),
                                item_no=item_data.get('item_no', ''),
                                description=item_data.get('description', ''),
                                quantity=_dec_any(item_data.get('quantity', 0)),
                                price=_dec_any(item_data.get('price', 0)),
                                row_total=_dec_any(item_data.get('row_total', 0)),
                                row_status=item_data.get('row_status', 'C'),
                                job_type=item_data.get('job_type', ''),
                                manufacture=item_data.get('manufacture', ''),
                                remaining_open_quantity=_dec_any(item_data.get('remaining_open_quantity', 0)),
                                pending_amount=_dec_any(item_data.get('pending_amount', 0)),
                                total_available_stock=_dec_any(item_data.get('total_available_stock', 0)),
                                dip_warehouse_stock=_dec_any(item_data.get('dip_warehouse_stock', 0)),
                            )
                        )
                        
                        if len(items_to_create) >= 10000:
                            SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=10000)
                            items_to_create = []
                
                if items_to_create:
                    SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=20000)
                
                sync_stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_orders)
                
                # Close missing orders (orders that were open but not in API response)
                # Find orders that were previously open but are NOT in the API response
                previously_open_orders = SAPSalesorder.objects.filter(
                    status__in=['O', 'OPEN'],
                    so_number__isnull=False
                ).exclude(so_number__in=api_so_numbers)
                
                closed_count = 0
                for order in previously_open_orders:
                    order.status = 'C'
                    order.save(update_fields=['status'])
                    
                    # Close all items
                    order.items.all().update(
                        row_status='C',
                        remaining_open_quantity=Decimal('0'),
                        pending_amount=Decimal('0')
                    )
                    closed_count += 1
                
                sync_stats['closed'] = closed_count
                
                # Create/Update SAP PIs for orders where is_sap_pi=True
                from so.models import SAPProformaInvoice, SAPProformaInvoiceLine
                sap_pis_created = 0
                sap_pis_updated = 0
                
                for mapped in mapped_orders:
                    so_no = mapped.get('so_number')
                    is_sap_pi = mapped.get('is_sap_pi', False)
                    sap_pi_lpo_date = mapped.get('sap_pi_lpo_date')
                    
                    if not is_sap_pi or not so_no:
                        continue
                    
                    try:
                        salesorder = SAPSalesorder.objects.get(so_number=so_no)
                        
                        # SAP PI numbering requirement: use the SAME number as the Sales Order.
                        # Backwards-compat: if an old "-SAP" PI exists, rename it to the SO number.
                        desired_pi_number = f"{so_no}"
                        legacy_pi_number = f"{so_no}-SAP"
                        
                        # Check if SAP PI already exists (new or legacy number)
                        sap_pi = SAPProformaInvoice.objects.filter(pi_number=desired_pi_number).first()
                        created = False
                        if sap_pi is None:
                            sap_pi = SAPProformaInvoice.objects.filter(pi_number=legacy_pi_number).first()
                            if sap_pi is not None:
                                # Rename legacy PI number to desired one (if free)
                                if not SAPProformaInvoice.objects.filter(pi_number=desired_pi_number).exists():
                                    sap_pi.pi_number = desired_pi_number
                                    sap_pi.save(update_fields=["pi_number"])
                        if sap_pi is None:
                            sap_pi = SAPProformaInvoice.objects.create(
                                pi_number=desired_pi_number,
                                salesorder=salesorder,
                                sequence=0,  # SAP PIs use sequence 0
                                status='ACTIVE',  # Keep PI status as ACTIVE (SO status shown in UI)
                                is_sap_pi=True,
                                pi_date=salesorder.posting_date,  # Use SO date for SAP PI
                                lpo_date=sap_pi_lpo_date,
                            )
                            created = True
                            sap_pis_created += 1
                        
                        if not created:
                            # Update existing SAP PI (don't change status - SO status shown in UI)
                            sap_pi.salesorder = salesorder
                            sap_pi.is_sap_pi = True
                            sap_pi.pi_date = salesorder.posting_date  # Always sync PI date with SO date
                            if sap_pi_lpo_date:
                                sap_pi.lpo_date = sap_pi_lpo_date
                            sap_pi.save()
                            sap_pis_updated += 1
                        
                        # Delete existing lines and recreate from SO items
                        sap_pi.lines.all().delete()
                        
                        # Create PI lines from all SO items
                        so_items = salesorder.items.all().order_by('line_no')
                        pi_lines_to_create = []
                        
                        for so_item in so_items:
                            pi_lines_to_create.append(
                                SAPProformaInvoiceLine(
                                    pi=sap_pi,
                                    so_item=so_item,
                                    so_number=so_no,
                                    line_no=so_item.line_no,
                                    item_no=so_item.item_no or '',
                                    description=so_item.description or '',
                                    manufacture=so_item.manufacture or '',
                                    job_type=so_item.job_type or '',
                                    quantity=so_item.quantity or Decimal('0'),
                                )
                            )
                        
                        if pi_lines_to_create:
                            SAPProformaInvoiceLine.objects.bulk_create(pi_lines_to_create, batch_size=1000)
                        
                    except SAPSalesorder.DoesNotExist:
                        logger.warning(f"Salesorder {so_no} not found when creating SAP PI")
                        continue
                    except Exception as e:
                        logger.error(f"Error creating SAP PI for {so_no}: {e}")
                        continue
                
                # Update Customer model with address and phone from API (outside atomic block to avoid transaction issues)
                from so.models import Customer
                for mapped in mapped_orders:
                    customer_code = mapped.get('customer_code', '').strip()
                    customer_address = mapped.get('customer_address', '').strip()
                    customer_phone = mapped.get('customer_phone', '').strip()
                    
                    if customer_code:
                        try:
                            # Truncate phone to current field max_length (safety check - until migration is run)
                            if customer_phone:
                                max_phone_len = Customer._meta.get_field('phone_number').max_length
                                if len(customer_phone) > max_phone_len:
                                    customer_phone = customer_phone[:max_phone_len]
                                    logger.warning(f"Truncated phone number for customer {customer_code} to {max_phone_len} chars")
                            
                            customer, created = Customer.objects.get_or_create(
                                customer_code=customer_code,
                                defaults={'customer_name': mapped.get('customer_name', '').strip() or customer_code}
                            )
                            # Update address and phone if provided
                            if customer_address:
                                customer.address = customer_address
                            if customer_phone:
                                customer.phone_number = customer_phone
                            # Update VAT number if provided
                            vat_num = mapped.get('vat_number', '').strip()
                            if vat_num:
                                customer.vat_number = vat_num
                            customer.save()
                        except Exception as e:
                            logger.warning(f"Error updating Customer {customer_code}: {e}")
                
                messages.success(
                    request,
                    f"Synced {sync_stats['total_orders']} sales orders: "
                    f"{sync_stats['created']} created, {sync_stats['updated']} updated, "
                    f"{sync_stats['closed']} closed. Total items: {sync_stats['total_items']}. "
                    f"SAP PIs: {sap_pis_created} created, {sap_pis_updated} updated."
                )
                return redirect('salesorder_list')
                
        except Exception as e:
            logger.exception("Error syncing sales orders from API")
            messages.error(request, f"Error syncing from API: {str(e)}")
            sync_stats['errors'].append(str(e))
    
    return render(request, 'salesorders/upload_salesorders.html', {
        'messages': messages_list,
        'sync_stats': sync_stats
    })


# =====================
# Salesorder: API Sync Receive (from PC script)
# =====================
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
@require_POST
def sync_salesorders_api_receive(request):
    """
    Receive sales orders data from PC script via HTTP API
    This endpoint is called by the PC sync script
    """
    from django.http import JsonResponse
    
    try:
        # Get data from request (JSON)
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body)
        else:
            # Try to parse as JSON anyway
            try:
                data = json.loads(request.body)
            except:
                data = request.POST.dict()
        
        # Verify API key
        api_key = data.get('api_key')
        expected_key = getattr(settings, 'VPS_API_KEY', 'your-secret-api-key')
        
        if not api_key or api_key != expected_key:
            return JsonResponse({
                'success': False,
                'error': 'Invalid API key'
            }, status=401)
        
        orders = data.get('orders', [])
        api_so_numbers = data.get('api_so_numbers', [])
        
        if not orders:
            return JsonResponse({
                'success': False,
                'error': 'No orders provided'
            })
        
        # Process orders (reuse existing sync logic)
        stats = {
            'created': 0,
            'updated': 0,
            'closed': 0,
            'total_items': 0
        }
        
        so_numbers = [m['so_number'] for m in orders if m.get('so_number')]
        api_so_numbers_set = set(api_so_numbers)
        
        with transaction.atomic():
            # Fetch existing orders
            try:
                existing_map = {o.so_number: o for o in SAPSalesorder.objects.filter(so_number__in=so_numbers)}
            except Exception:
                existing_map = {}
            
            to_create = []
            to_update = []
            
            def _dec2(x) -> Decimal:
                try:
                    if x is None or (isinstance(x, float) and pd.isna(x)):
                        return Decimal("0.00")
                    return Decimal(str(x)).quantize(Decimal("0.01"))
                except Exception:
                    return Decimal("0.00")
            
            # Process each mapped order
            for mapped in orders:
                so_no = mapped.get('so_number')
                if not so_no:
                    continue
                
                # Parse posting_date if it's a string
                posting_date = mapped.get('posting_date')
                if isinstance(posting_date, str):
                    try:
                        posting_date = datetime.strptime(posting_date, '%Y-%m-%d').date()
                    except (ValueError, TypeError):
                        posting_date = None
                elif posting_date and hasattr(posting_date, 'date'):
                    posting_date = posting_date.date() if hasattr(posting_date, 'date') else posting_date

                # Parse SAP PI LPO date (U_Lpdate) if it's a string
                sap_pi_lpo_date = mapped.get('sap_pi_lpo_date')
                if isinstance(sap_pi_lpo_date, str):
                    try:
                        sap_pi_lpo_date = datetime.strptime(sap_pi_lpo_date, '%Y-%m-%d').date()
                    except (ValueError, TypeError):
                        sap_pi_lpo_date = None
                elif sap_pi_lpo_date and hasattr(sap_pi_lpo_date, 'date'):
                    sap_pi_lpo_date = sap_pi_lpo_date.date() if hasattr(sap_pi_lpo_date, 'date') else sap_pi_lpo_date
                
                defaults = {
                    "posting_date": posting_date,
                    "customer_code": mapped.get('customer_code', ''),
                    "customer_name": mapped.get('customer_name', ''),
                    "bp_reference_no": mapped.get('bp_reference_no', ''),
                    "salesman_name": mapped.get('salesman_name', ''),
                    "discount_percentage": _dec2(mapped.get('discount_percentage', 0)),  # Exact value from API
                    "document_total": _dec2(mapped.get('document_total', 0)),
                    "row_total_sum": _dec2(mapped.get('row_total_sum', 0)),
                    "status": mapped.get('status', 'C'),
                    "vat_number": mapped.get('vat_number', '') or '',  # VAT Number from BusinessPartner.FederalTaxID
                    "customer_address": mapped.get('customer_address', '') or '',  # Address from main API response
                    "customer_phone": mapped.get('customer_phone', '') or '',  # Phone1 from BusinessPartner
                    "is_sap_pi": mapped.get('is_sap_pi', False),  # True if U_PROFORMAINVOICE=Y
                }
                
                # Add last_synced_at only if field exists in model (after migration)
                if 'last_synced_at' in [f.name for f in SAPSalesorder._meta.get_fields()]:
                    defaults["last_synced_at"] = datetime.now()
                
                if mapped.get('internal_number'):
                    defaults["internal_number"] = mapped.get('internal_number')
                
                obj = existing_map.get(so_no)
                if obj is None:
                    to_create.append(SAPSalesorder(so_number=so_no, **defaults))
                    stats['created'] += 1
                else:
                    for k, v in defaults.items():
                        setattr(obj, k, v)
                    to_update.append(obj)
                    stats['updated'] += 1
            
            # Bulk create/update (optimized batch sizes for better performance)
            if to_create:
                SAPSalesorder.objects.bulk_create(to_create, batch_size=5000)
            
            if to_update:
                update_fields = [
                    "posting_date", "customer_code", "customer_name", "bp_reference_no",
                    "salesman_name", "discount_percentage", "document_total", "row_total_sum",
                    "status", "vat_number", "customer_address", "customer_phone", "internal_number", "is_sap_pi"
                ]
                # Add last_synced_at only if field exists in model (after migration)
                if 'last_synced_at' in [f.name for f in SAPSalesorder._meta.get_fields()]:
                    update_fields.append("last_synced_at")
                    # Update last_synced_at for all objects being updated
                    for obj in to_update:
                        obj.last_synced_at = datetime.now()
                
                SAPSalesorder.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)
            
            # Re-fetch ids for FK mapping
            order_id_map = dict(
                SAPSalesorder.objects.filter(so_number__in=so_numbers).values_list("so_number", "id")
            )
            
            # Delete existing items for these salesorders
            SAPSalesorderItem.objects.filter(salesorder__so_number__in=so_numbers).delete()
            
            # Build items list + bulk insert
            items_to_create = []
            
            def _dec_any(x) -> Decimal:
                try:
                    if x is None or (isinstance(x, float) and pd.isna(x)):
                        return Decimal("0")
                    return Decimal(str(x))
                except Exception:
                    return Decimal("0")
            
            for mapped in orders:
                so_no = mapped.get('so_number')
                so_id = order_id_map.get(so_no)
                if not so_id:
                    continue
                
                for item_data in mapped.get('items', []):
                    items_to_create.append(
                        SAPSalesorderItem(
                            salesorder_id=so_id,
                            line_no=item_data.get('line_no', 1),
                            item_no=item_data.get('item_no', ''),
                            description=item_data.get('description', ''),
                            quantity=_dec_any(item_data.get('quantity', 0)),
                            price=_dec_any(item_data.get('price', 0)),
                            row_total=_dec_any(item_data.get('row_total', 0)),
                            row_status=item_data.get('row_status', 'C'),
                            job_type=item_data.get('job_type', ''),
                            manufacture=item_data.get('manufacture', ''),
                            remaining_open_quantity=_dec_any(item_data.get('remaining_open_quantity', 0)),
                            pending_amount=_dec_any(item_data.get('pending_amount', 0)),
                            total_available_stock=_dec_any(item_data.get('total_available_stock', 0)),
                            dip_warehouse_stock=_dec_any(item_data.get('dip_warehouse_stock', 0)),
                        )
                    )
                    
                    if len(items_to_create) >= 20000:
                        SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=20000)
                        items_to_create = []
            
            if items_to_create:
                SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=20000)
            
            stats['total_items'] = sum(len(m.get('items', [])) for m in orders)
            
            # Close missing orders
            previously_open_orders = SAPSalesorder.objects.filter(
                status__in=['O', 'OPEN'],
                so_number__isnull=False
            ).exclude(so_number__in=api_so_numbers_set)
            
            closed_count = 0
            for order in previously_open_orders:
                order.status = 'C'
                order.save(update_fields=['status'])
                
                SAPSalesorderItem.objects.filter(salesorder=order).update(
                    row_status='C',
                    remaining_open_quantity=Decimal('0'),
                    pending_amount=Decimal('0')
                )
                closed_count += 1
            
            stats['closed'] = closed_count
            
            # Create/Update SAP PIs for orders where is_sap_pi=True
            from so.models import SAPProformaInvoice, SAPProformaInvoiceLine
            stats['sap_pis_created'] = 0
            stats['sap_pis_updated'] = 0
            
            for mapped in orders:
                so_no = mapped.get('so_number')
                is_sap_pi = mapped.get('is_sap_pi', False)
                sap_pi_lpo_date = mapped.get('sap_pi_lpo_date')
                
                if not is_sap_pi or not so_no:
                    continue
                
                try:
                    salesorder = SAPSalesorder.objects.get(so_number=so_no)
                    
                    # SAP PI numbering requirement: use the SAME number as the Sales Order.
                    # Backwards-compat: if an old "-SAP" PI exists, rename it to the SO number.
                    desired_pi_number = f"{so_no}"
                    legacy_pi_number = f"{so_no}-SAP"
                    
                    # Check if SAP PI already exists (new or legacy number)
                    sap_pi = SAPProformaInvoice.objects.filter(pi_number=desired_pi_number).first()
                    created = False
                    if sap_pi is None:
                        sap_pi = SAPProformaInvoice.objects.filter(pi_number=legacy_pi_number).first()
                        if sap_pi is not None:
                            if not SAPProformaInvoice.objects.filter(pi_number=desired_pi_number).exists():
                                sap_pi.pi_number = desired_pi_number
                                sap_pi.save(update_fields=["pi_number"])
                    if sap_pi is None:
                        sap_pi = SAPProformaInvoice.objects.create(
                            pi_number=desired_pi_number,
                            salesorder=salesorder,
                            sequence=0,  # SAP PIs use sequence 0
                            status='ACTIVE',  # Keep PI status as ACTIVE (SO status shown in UI)
                            is_sap_pi=True,
                            pi_date=salesorder.posting_date,  # Use SO date for SAP PI
                            lpo_date=sap_pi_lpo_date,
                        )
                        created = True
                        stats['sap_pis_created'] += 1
                    
                    if not created:
                        # Update existing SAP PI (don't change status - SO status shown in UI)
                        sap_pi.salesorder = salesorder
                        sap_pi.is_sap_pi = True
                        sap_pi.pi_date = salesorder.posting_date  # Always sync PI date with SO date
                        if sap_pi_lpo_date:
                            sap_pi.lpo_date = sap_pi_lpo_date
                        sap_pi.save()
                        stats['sap_pis_updated'] += 1
                    
                    # Delete existing lines and recreate from SO items
                    sap_pi.lines.all().delete()
                    
                    # Create PI lines from all SO items
                    so_items = salesorder.items.all().order_by('line_no')
                    pi_lines_to_create = []
                    
                    for so_item in so_items:
                        pi_lines_to_create.append(
                            SAPProformaInvoiceLine(
                                pi=sap_pi,
                                so_item=so_item,
                                so_number=so_no,
                                line_no=so_item.line_no,
                                item_no=so_item.item_no or '',
                                description=so_item.description or '',
                                manufacture=so_item.manufacture or '',
                                job_type=so_item.job_type or '',
                                quantity=so_item.quantity or Decimal('0'),
                            )
                        )
                    
                    if pi_lines_to_create:
                        SAPProformaInvoiceLine.objects.bulk_create(pi_lines_to_create, batch_size=1000)
                    
                except SAPSalesorder.DoesNotExist:
                    logger.warning(f"Salesorder {so_no} not found when creating SAP PI")
                    continue
                except Exception as e:
                    logger.error(f"Error creating SAP PI for {so_no}: {e}")
                    continue
        
        # Update Customer model with address and phone from API (outside atomic block to avoid transaction issues)
        from so.models import Customer
        for mapped in orders:
            customer_code = mapped.get('customer_code', '').strip()
            customer_address = mapped.get('customer_address', '').strip()
            customer_phone = mapped.get('customer_phone', '').strip()
            
            if customer_code:
                try:
                    # Truncate phone to max 50 chars (safety check - until migration is run, also check for old 15 char limit)
                    if customer_phone:
                        # Check current field max_length from database
                        max_phone_len = Customer._meta.get_field('phone_number').max_length
                        if len(customer_phone) > max_phone_len:
                            customer_phone = customer_phone[:max_phone_len]
                            logger.warning(f"Truncated phone number for customer {customer_code} to {max_phone_len} chars")
                    
                    customer, created = Customer.objects.get_or_create(
                        customer_code=customer_code,
                        defaults={'customer_name': mapped.get('customer_name', '').strip() or customer_code}
                    )
                    # Update address and phone if provided
                    if customer_address:
                        customer.address = customer_address
                    if customer_phone:
                        customer.phone_number = customer_phone
                    # Update VAT number if provided
                    vat_num = mapped.get('vat_number', '').strip()
                    if vat_num:
                        customer.vat_number = vat_num
                    customer.save()
                except Exception as e:
                    logger.warning(f"Error updating Customer {customer_code}: {e}")
        
        return JsonResponse({
            'success': True,
            'stats': stats,
            'message': f'Synced {len(orders)} orders successfully'
        })
        
    except Exception as e:
        logger.exception('Error in sync_salesorders_api_receive')
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"Full error traceback: {error_details}")
        return JsonResponse({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }, status=500)


# =====================
# Salesorder: List
# =====================
@login_required
def salesorder_list(request):
    # Scope by logged-in user
    qs = SAPSalesorder.objects.all().filter(salesman_scope_q_salesorder(request.user))

    # Derive header status from items (Open if ANY line is open)
    open_items_sq = SAPSalesorderItem.objects.filter(salesorder=OuterRef("pk")).filter(_open_row_status_q())
    qs = qs.annotate(
        has_open=Exists(open_items_sq),
        display_status=Case(
            When(has_open=True, then=Value("O")),
            default=Value("C"),
            output_field=CharField(),
        ),
        # Calculate pending_total = sum of pending_amount from all items
        pending_total=Coalesce(
            Sum('items__pending_amount'),
            Value(0, output_field=DecimalField())
        ),
    )

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
                Q(salesman_name__istartswith=q) |
                Q(bp_reference_no__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(so_number__icontains=q) |
                Q(customer_name__icontains=q) |
                Q(salesman_name__icontains=q) |
                Q(bp_reference_no__icontains=q)
            )

    # Status filter (based on derived status)
    if status:
        s = status.strip().upper()
        if s in ("OPEN", "O"):
            qs = qs.filter(has_open=True)
        elif s in ("CLOSED", "C"):
            qs = qs.filter(has_open=False)
        else:
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

    qs = qs.order_by('-posting_date', '-so_number')

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
    items = salesorder.items.all().order_by('line_no', 'id')

    # Get PI allocations
    allocated = _get_allocated_quantities(so_number)

    # Get existing PIs
    pis = salesorder.proforma_invoices.all().order_by('-sequence')

    # Derive header status from line Row Status (Open if any Open/O).
    # Also auto-correct stored header status if it's out of sync.
    any_open = items.filter(_open_row_status_q()).exists()
    derived_status = "O" if any_open else "C"
    if (salesorder.status or "").strip().upper() not in (derived_status, "OPEN", "CLOSED"):
        salesorder.status = derived_status
        salesorder.save(update_fields=["status"])

    # Normalize status label for UI
    status_raw = (salesorder.status or "").strip()
    status_key = status_raw.upper()
    if status_key in ("O", "OPEN"):
        status_label = "Open"
    elif status_key in ("C", "CLOSED"):
        status_label = "Closed"
    else:
        status_label = status_raw or "—"

    # Totals
    # Pending Total = Sum of OpenAmount (pending_amount) from all items
    pending_total = items.aggregate(
        total=Coalesce(Sum("pending_amount"), Value(0, output_field=DecimalField()))
    )["total"] or Decimal("0.00")
    
    row_total_sum = getattr(salesorder, "row_total_sum", None)
    row_total_calc_sum = Decimal("0")

    # Batch load live stock from Items model (for all item_no values)
    from so.models import Items
    item_codes = [it.item_no for it in items if it.item_no]
    stock_lookup = {}
    if item_codes:
        for item in Items.objects.filter(item_code__in=item_codes).only('item_code', 'total_available_stock', 'dip_warehouse_stock'):
            stock_lookup[item.item_code] = {
                'total_available_stock': item.total_available_stock or Decimal("0"),
                'dip_warehouse_stock': item.dip_warehouse_stock or Decimal("0"),
            }

    # Attach derived unit price, live stock, and PI allocation info for templates
    for it in items:
        qty = it.quantity or Decimal("0")
        row_total = it.row_total or Decimal("0")
        if qty and qty != 0:
            it.unit_price = (row_total / qty).quantize(Decimal("0.01"))
        else:
            it.unit_price = Decimal("0.00")

        row_total_calc_sum += row_total
        
        # Live stock from Items model (overrides stored values)
        stock_data = stock_lookup.get(it.item_no, {})
        it.total_available_stock = stock_data.get('total_available_stock', Decimal("0"))
        it.dip_warehouse_stock = stock_data.get('dip_warehouse_stock', Decimal("0"))
        
        # Attach PI allocation info (using item ID for accurate matching)
        it.pi_allocated_qty = allocated.get(it.id, Decimal("0"))
        it.pi_remaining_qty = max(Decimal("0"), qty - it.pi_allocated_qty)

    if row_total_sum is None:
        row_total_sum = row_total_calc_sum

    # Use values from API if available, otherwise calculate
    # Subtotal = row_total_sum (sum of all line totals)
    subtotal = row_total_sum or Decimal("0.00")
    
    # Discount: Use from API if available, otherwise calculate
    # For API data: discount_percentage is exact value, but we display rounded to 1 decimal
    discount_percentage_exact = salesorder.discount_percentage or Decimal("0.00")
    discount_percentage_display = round(float(discount_percentage_exact), 1)  # Round to 1 decimal for display
    
    # Use TotalDiscount from API directly (if we had it stored), otherwise calculate from percentage
    # For now, calculate from percentage (we'll store TotalDiscount from API in future if needed)
    discount_amount = (subtotal * discount_percentage_exact / 100).quantize(Decimal("0.01")) if subtotal is not None else Decimal("0.00")
    total_before_tax = (subtotal - discount_amount).quantize(Decimal("0.01")) if subtotal is not None else Decimal("0.00")
    
    # VAT: Use VatSum from API directly (if we had it stored), otherwise calculate at 5%
    # For now, calculate at 5% (we'll store VatSum from API in future if needed)
    vat_rate = Decimal("0.05")
    vat_amount = (total_before_tax * vat_rate).quantize(Decimal("0.01")) if total_before_tax is not None else Decimal("0.00")
    
    # Grand Total calculation: Subtotal - TotalDiscount + VatSum
    # Formula: Grand Total = Subtotal - Discount + VAT
    grand_total = (subtotal - discount_amount + vat_amount).quantize(Decimal("0.01"))
    
    # Pending Total = sum of OpenAmount (already stored in document_total from API sync)
    # This represents the open/pending amount, not the full document total
    
    # Calculate Total PI Amount (sum of all active PI line totals)
    total_pi_amount = Decimal("0.00")
    if pis.exists():
        for pi in pis.filter(status='ACTIVE'):
            for pi_line in pi.lines.all():
                so_item = pi_line.so_item
                if so_item:
                    # Calculate unit price from SO item
                    qty_so = so_item.quantity or Decimal("0")
                    row_total_so = so_item.row_total or Decimal("0")
                    if qty_so and qty_so != 0:
                        unit_price_pi = (row_total_so / qty_so).quantize(Decimal("0.01"))
                    else:
                        unit_price_pi = Decimal("0.00")
                    # PI line total = unit_price * PI quantity
                    pi_line_total = (unit_price_pi * pi_line.quantity).quantize(Decimal("0.01"))
                    total_pi_amount += pi_line_total
    
    # Balance Amount = Row Total Sum - Total PI Amount
    balance_amount = (row_total_sum - total_pi_amount).quantize(Decimal("0.01")) if row_total_sum is not None else Decimal("0.00")

    context = {
        'salesorder': salesorder,
        'items': items,
        'pis': pis,
        'status_label': status_label,
        'pending_total': pending_total,
        'row_total_sum': row_total_sum,
        'subtotal': subtotal,
        'discount_percentage': discount_percentage_display,  # Display rounded to 1 decimal
        'discount_percentage_exact': discount_percentage_exact,  # Exact value for calculations
        'discount_amount': discount_amount,
        'total_before_tax': total_before_tax,
        'vat_amount': vat_amount,
        'grand_total': grand_total,
        'total_pi_amount': total_pi_amount,
        'balance_amount': balance_amount,
    }

    return render(request, 'salesorders/salesorder_detail.html', context)


# =====================
# Salesorder: AJAX Search (rows + pagination HTML)
# =====================
@login_required
def salesorder_search(request):
    # Scope by logged-in user
    qs = SAPSalesorder.objects.all().filter(salesman_scope_q_salesorder(request.user))

    # Derive header status from items (Open if ANY line is open)
    open_items_sq = SAPSalesorderItem.objects.filter(salesorder=OuterRef("pk")).filter(_open_row_status_q())
    qs = qs.annotate(
        has_open=Exists(open_items_sq),
        display_status=Case(
            When(has_open=True, then=Value("O")),
            default=Value("C"),
            output_field=CharField(),
        ),
        # Calculate pending_total = sum of pending_amount from all items
        pending_total=Coalesce(
            Sum('items__pending_amount'),
            Value(0, output_field=DecimalField())
        ),
    )

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
                Q(salesman_name__istartswith=q) |
                Q(bp_reference_no__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(so_number__icontains=q) |
                Q(customer_name__icontains=q) |
                Q(salesman_name__icontains=q) |
                Q(bp_reference_no__icontains=q)
            )

    # Status filter (based on derived status)
    if status:
        s = status.strip().upper()
        if s in ("OPEN", "O"):
            qs = qs.filter(has_open=True)
        elif s in ("CLOSED", "C"):
            qs = qs.filter(has_open=False)
        else:
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
    qs = qs.order_by('-posting_date', '-so_number')

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
        [Paragraph(f"<b>Status:</b> {salesorder.status or '—'}", styles['Normal'])],
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

        # New Excel doesn't include unit price; derive from row_total/qty when possible
        if (price == 0 or price is None) and qty:
            try:
                price = (row_total / qty).quantize(Decimal("0.01"))
            except Exception:
                price = Decimal("0")
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

    # Summary
    # - Subtotal is computed from line Row Total (fallback qty*price)
    # - row_total_sum is stored from Excel SUM(Row Total) (Document Total)
    stored_row_total_sum = _to_decimal(getattr(salesorder, 'row_total_sum', None))
    doc_total = (stored_row_total_sum if stored_row_total_sum else subtotal).quantize(Decimal("0.01"))
    vat_rate = Decimal("0.05")
    vat_amount = (doc_total * vat_rate).quantize(Decimal("0.01"))
    grand_total = (doc_total + vat_amount).quantize(Decimal("0.01"))

    summary_data = [
        ['Document Total:', f"AED {doc_total:,.2f}"],
        ['VAT (5%):', f"AED {vat_amount:,.2f}"],
        ['Grand Total:', f"AED {grand_total:,.2f}"],
    ]
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

    # Derive header status from items (Open if ANY line is open)
    open_items_sq = SAPSalesorderItem.objects.filter(salesorder=OuterRef("pk")).filter(_open_row_status_q())
    qs = qs.annotate(
        has_open=Exists(open_items_sq),
        display_status=Case(
            When(has_open=True, then=Value("O")),
            default=Value("C"),
            output_field=CharField(),
        ),
        # Calculate pending_total = sum of pending_amount from all items
        pending_total=Coalesce(
            Sum('items__pending_amount'),
            Value(0, output_field=DecimalField())
        ),
    )

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
    # Status filter (based on derived status)
    if status:
        s = status.strip().upper()
        if s in ("OPEN", "O"):
            qs = qs.filter(has_open=True)
        elif s in ("CLOSED", "C"):
            qs = qs.filter(has_open=False)
        else:
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


# =====================
# Proforma Invoice (PI) Views
# =====================

def _get_allocated_quantities(so_number):
    """
    Returns a dict mapping item_id -> allocated quantity (from ACTIVE PIs only).
    Uses item_id for accurate per-item allocation tracking.
    """
    from django.db.models import Sum
    from collections import defaultdict
    
    allocated = defaultdict(lambda: Decimal("0"))
    
    # First, try to get allocations by item_id (most accurate)
    pi_lines_by_item = SAPProformaInvoiceLine.objects.filter(
        so_number=so_number,
        pi__status='ACTIVE',
        so_item__isnull=False
    ).values('so_item_id').annotate(
        total_qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    )
    
    for line in pi_lines_by_item:
        item_id = line['so_item_id']
        allocated[item_id] = Decimal(str(line['total_qty']))
    
    # Fallback: for PI lines without item_id (old data), try to match by item_no/description
    # This handles PIs created before we added the so_item field
    salesorder = SAPSalesorder.objects.filter(so_number=so_number).first()
    if salesorder:
        pi_lines_old = SAPProformaInvoiceLine.objects.filter(
            so_number=so_number,
            pi__status='ACTIVE',
            so_item__isnull=True
        ).select_related('pi')
        
        # Create a map of item_no -> item_id for quick lookup
        item_no_map = {}
        description_map = {}
        for item in salesorder.items.all():
            if item.item_no:
                if item.item_no not in item_no_map:
                    item_no_map[item.item_no] = []
                item_no_map[item.item_no].append(item.id)
            # Also index by description (first 50 chars for matching)
            desc_key = (item.description or "")[:50].strip().upper()
            if desc_key:
                if desc_key not in description_map:
                    description_map[desc_key] = []
                description_map[desc_key].append(item.id)
        
        # Try to match old PI lines to items
        for pi_line in pi_lines_old:
            matched_item_ids = []
            
            # Try matching by item_no first
            if pi_line.item_no:
                matched_item_ids = item_no_map.get(pi_line.item_no.strip(), [])
            
            # If no match, try by description
            if not matched_item_ids and pi_line.description:
                desc_key = pi_line.description[:50].strip().upper()
                matched_item_ids = description_map.get(desc_key, [])
            
            # If we found matches, allocate the quantity to the first match
            # (or distribute if multiple matches - but usually should be one)
            if matched_item_ids:
                qty = pi_line.quantity or Decimal("0")
                # For multiple matches, distribute evenly (shouldn't happen often)
                qty_per_item = qty / len(matched_item_ids) if matched_item_ids else Decimal("0")
                for item_id in matched_item_ids:
                    if item_id not in allocated:  # Only add if not already allocated by item_id method
                        allocated[item_id] = allocated.get(item_id, Decimal("0")) + qty_per_item
    
    return allocated


@login_required
def create_pi(request, so_number):
    """
    Create a Proforma Invoice from a Sales Order.
    GET: Show form with selectable items and remaining quantities.
    POST: Create PI with selected items and quantities.
    """
    salesorder = get_object_or_404(SAPSalesorder, so_number=so_number)
    
    # Enforce scope
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Salesorder not found")
    
    items = salesorder.items.all().order_by('line_no', 'id')
    
    # Calculate allocated and remaining quantities per line
    allocated = _get_allocated_quantities(so_number)
    
    # Prepare items with remaining quantities
    items_with_remaining = []
    for item in items:
        allocated_qty = allocated.get(item.id, Decimal("0"))
        remaining_qty = max(Decimal("0"), item.quantity - allocated_qty)
        
        # Derive unit price from row_total / quantity
        qty = item.quantity or Decimal("0")
        row_total = item.row_total or Decimal("0")
        unit_price = (row_total / qty).quantize(Decimal("0.01")) if qty and qty != 0 else Decimal("0.00")
        
        items_with_remaining.append({
            'item': item,
            'allocated_qty': allocated_qty,
            'remaining_qty': remaining_qty,
            'unit_price': unit_price,
        })
    
    if request.method == 'POST':
        # Validate and create PI
        selected_lines = request.POST.getlist('line_ids')
        quantities = {}
        
        errors = []
        
        for line_id in selected_lines:
            qty_str = request.POST.get(f'qty_{line_id}', '0').strip()
            try:
                qty = Decimal(qty_str)
                if qty <= 0:
                    continue
                quantities[int(line_id)] = qty
            except (ValueError, TypeError):
                errors.append(f"Invalid quantity for line {line_id}")
        
        if errors:
            messages.error(request, "; ".join(errors))
            return render(request, 'salesorders/pi_create.html', {
                'salesorder': salesorder,
                'items_with_remaining': items_with_remaining,
            })
        
        # Get next sequence number for PI numbering and validate quantities after locking
        with transaction.atomic():
            # Lock the SO to prevent race conditions
            salesorder = SAPSalesorder.objects.select_for_update().get(so_number=so_number)
            
            # Recalculate allocated quantities after locking (to avoid race conditions)
            allocated = _get_allocated_quantities(so_number)
            
            # Validate quantities don't exceed remaining (using fresh allocations)
            items_map = {item.id: item for item in salesorder.items.all()}
            for line_id, qty in quantities.items():
                item = items_map.get(line_id)
                if not item:
                    errors.append(f"Line {line_id} not found")
                    continue
                allocated_qty = allocated.get(item.id, Decimal("0"))
                remaining_qty = max(Decimal("0"), item.quantity - allocated_qty)
                if qty > remaining_qty:
                    errors.append(f"Quantity {qty} exceeds remaining {remaining_qty} for line {item.line_no}")
            
            if errors:
                # Recalculate items_with_remaining for error display
                items = salesorder.items.all().order_by('line_no', 'id')
                items_with_remaining = []
                for item in items:
                    allocated_qty = allocated.get(item.id, Decimal("0"))
                    remaining_qty = max(Decimal("0"), item.quantity - allocated_qty)
                    qty = item.quantity or Decimal("0")
                    row_total = item.row_total or Decimal("0")
                    unit_price = (row_total / qty).quantize(Decimal("0.01")) if qty and qty != 0 else Decimal("0.00")
                    items_with_remaining.append({
                        'item': item,
                        'allocated_qty': allocated_qty,
                        'remaining_qty': remaining_qty,
                        'unit_price': unit_price,
                    })
                messages.error(request, "; ".join(errors))
                return render(request, 'salesorders/pi_create.html', {
                    'salesorder': salesorder,
                    'items_with_remaining': items_with_remaining,
                })
            
            # Get next sequence number (check all PIs to avoid reusing numbers)
            last_pi = SAPProformaInvoice.objects.filter(
                salesorder=salesorder
            ).order_by('-sequence').first()
            
            next_seq = last_pi.sequence + 1 if last_pi else 1
            
            pi_number = f"{so_number}-P{next_seq}"
            
            # Get remarks (with default standard format)
            remarks = request.POST.get('remarks', '').strip()
            if not remarks:
                # Default standard format
                remarks = "Note: Cheque to be prepared in favor of: \n1) JUNAID SANITARY & ELECTRICAL MAT. TRDG. LLC \nTax Registration Number 100225006400003\n2) PAYMENT : CDC Against Delivery\n3)  DELIVERY: Ex-Stock Subject to Receipt of cheque copy against this Proforma Invoice within 4 working days"
            
            # Get LPO Date
            lpo_date_str = request.POST.get('lpo_date', '').strip()
            lpo_date = None
            if lpo_date_str:
                try:
                    from datetime import datetime
                    lpo_date = datetime.strptime(lpo_date_str, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    lpo_date = None
            
            # Create PI
            from datetime import date
            pi = SAPProformaInvoice.objects.create(
                salesorder=salesorder,
                pi_number=pi_number,
                sequence=next_seq,
                status='ACTIVE',
                remarks=remarks,
                pi_date=date.today(),  # App PIs use today's date
                lpo_date=lpo_date,
                created_by=request.user if request.user.is_authenticated else None,
            )
            
            # Create log entry for PI creation
            ip = get_client_ip(request)
            network_label = label_network(ip)
            ua_string = request.META.get('HTTP_USER_AGENT', '')[:500]
            device_type, device_os, device_browser = parse_device_info(ua_string)
            
            try:
                lat = request.POST.get("location_lat")
                lng = request.POST.get("location_lng")
                lat_val = float(lat) if lat not in (None, "",) else None
                lng_val = float(lng) if lng not in (None, "",) else None
            except (ValueError, TypeError):
                lat_val = None
                lng_val = None
            
            ProformaInvoiceLog.objects.create(
                pi=pi,
                user=request.user if request.user.is_authenticated else None,
                ip_address=ip,
                user_agent=ua_string,
                device_type=device_type,
                device_os=device_os,
                device_browser=device_browser,
                location_lat=lat_val,
                location_lng=lng_val,
                network_label=network_label,
                device=getattr(request, 'device_obj', None),
                action="created",
            )
            
            # Create PI lines
            pi_lines = []
            for line_id, qty in quantities.items():
                item = SAPSalesorderItem.objects.get(id=line_id, salesorder=salesorder)
                pi_lines.append(
                    SAPProformaInvoiceLine(
                        pi=pi,
                        so_item=item,  # Direct reference for accurate allocation
                        so_number=so_number,
                        line_no=item.line_no,
                        item_no=item.item_no or "",
                        description=item.description,
                        manufacture=item.manufacture or "",
                        job_type=item.job_type or "",
                        quantity=qty,
                    )
                )
            
            SAPProformaInvoiceLine.objects.bulk_create(pi_lines)
        
        messages.success(request, f"Proforma Invoice {pi_number} created successfully.")
        return redirect("salesorder_detail", so_number=so_number)
    
    # GET: Show form
    return render(request, 'salesorders/pi_create.html', {
        'salesorder': salesorder,
        'items_with_remaining': items_with_remaining,
    })


import os
from io import BytesIO
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
    PageBreak, BaseDocTemplate, PageTemplate, Frame
)
from reportlab.platypus.flowables import Flowable


class HeaderFooterCanvas:
    """Helper class to draw headers and footers on each page"""
    
    def __init__(self, pi_number, posting_date, total_pages):
        self.pi_number = pi_number
        self.posting_date = posting_date
        self.total_pages = total_pages
        self.page_count = 0
        
        # Colors
        self.DARK_BLUE = HexColor('#1E3A5F')
        self.GRAY_TEXT = HexColor('#808080')
        self.LIGHT_BLUE_LINE = HexColor('#4A90D9')
    
    def __call__(self, canvas, doc):
        self.page_count += 1
        canvas.saveState()
        
        # Only draw continuation header on pages after the first
        if self.page_count > 1:
            self._draw_continuation_header(canvas, doc)
        
        canvas.restoreState()
    
    def _draw_continuation_header(self, canvas, doc):
        """Draw the simplified header for continuation pages"""
        page_width = A4[0]
        top_margin = A4[1] - 0.3*inch
        
        # Left side - Company name
        canvas.setFont('Helvetica-Bold', 11)
        canvas.setFillColor(self.DARK_BLUE)
        canvas.drawString(0.3*inch, top_margin, "Junaid Sanitary & Electrical")
        canvas.drawString(0.3*inch, top_margin - 14, "Materials Trading (L.L.C)")
        
        # Address below company name
        canvas.setFont('Helvetica', 9)
        canvas.setFillColor(colors.black)
        canvas.drawString(0.3*inch, top_margin - 32, "Dubai Investment Park-2, Dubai")
        
        # Right side - "Original" text
        canvas.setFont('Helvetica-Bold', 14)
        canvas.setFillColor(colors.black)
        canvas.drawString(4.5*inch, top_margin, "Original")
        
        # Document info labels (gray)
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(self.GRAY_TEXT)
        canvas.drawString(4*inch, top_margin - 25, "Document Number")
        canvas.drawString(5.5*inch, top_margin - 25, "Document Date")
        canvas.drawString(7*inch, top_margin - 25, "Page")
        
        # Document info values (bold black)
        canvas.setFont('Helvetica-Bold', 9)
        canvas.setFillColor(colors.black)
        canvas.drawString(4*inch, top_margin - 38, self.pi_number)
        canvas.drawString(5.5*inch, top_margin - 38, self.posting_date)
        canvas.drawString(7*inch, top_margin - 38, f"{self.page_count}/{self.total_pages}")
        
        # Blue horizontal line below header
        canvas.setStrokeColor(self.LIGHT_BLUE_LINE)
        canvas.setLineWidth(1)
        canvas.line(0.3*inch, top_margin - 50, page_width - 0.3*inch, top_margin - 50)


@login_required
def export_pi_pdf(request, pi_number):
    """
    Export Proforma Invoice as PDF matching the exact SAP format.
    Uses live pricing: unit_price = row_total / quantity from SO item.
    """
    pi = get_object_or_404(SAPProformaInvoice, pi_number=pi_number)
    
    # Enforce scope
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=pi.salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Proforma Invoice not found")
    
    salesorder = pi.salesorder
    pi_lines = pi.lines.all().order_by('line_no')
    
    # Build items list with live pricing
    items_data = []
    subtotal = Decimal("0")
    line_counter = 1
    
    for pi_line in pi_lines:
        so_item = pi_line.so_item
        if not so_item:
            # Filter by salesorder (ForeignKey) and line_no, not so_number
            so_item = SAPSalesorderItem.objects.filter(
                salesorder=salesorder,
                line_no=pi_line.line_no
            ).first()
        
        if so_item:
            qty = so_item.quantity or Decimal("0")
            row_total = so_item.row_total or Decimal("0")
            # Always calculate from row_total/qty for accuracy (price field may not be reliable)
            # This matches how it's calculated in salesorder_detail view
            if qty and qty != 0 and row_total:
                unit_price = (row_total / qty).quantize(Decimal("0.01"))
            else:
                unit_price = Decimal("0.00")
        else:
            # If so_item not found, try to get it again with more specific matching
            so_item = SAPSalesorderItem.objects.filter(
                salesorder=salesorder,
                item_no=pi_line.item_no,
                line_no=pi_line.line_no
            ).first()
            if so_item:
                qty = so_item.quantity or Decimal("0")
                row_total = so_item.row_total or Decimal("0")
                if qty and qty != 0 and row_total:
                    unit_price = (row_total / qty).quantize(Decimal("0.01"))
                else:
                    unit_price = Decimal("0.00")
            else:
                unit_price = Decimal("0.00")
        
        line_total = (unit_price * pi_line.quantity).quantize(Decimal("0.01"))
        subtotal += line_total
        
        items_data.append({
            'line_num': f"{line_counter:03d}",
            'item_no': pi_line.item_no or "-",
            'description': pi_line.description,
            'quantity': pi_line.quantity,
            'uom': 'PCS',
            'unit_price': unit_price,
            'tax_rate': Decimal("5.00"),
            'line_total': line_total,
        })
        line_counter += 1
    
    # Calculate totals - get discount from salesorder
    discount_percent = salesorder.discount_percentage or Decimal("0.00")
    discount_amount = (subtotal * discount_percent / 100).quantize(Decimal("0.01"))
    total_before_tax = (subtotal - discount_amount).quantize(Decimal("0.01"))
    vat_rate = Decimal("5.00")
    vat_amount = (total_before_tax * vat_rate / 100).quantize(Decimal("0.01"))
    grand_total = (total_before_tax + vat_amount).quantize(Decimal("0.01"))
    
    # Colors matching the image
    DARK_BLUE = HexColor('#1E3A5F')
    ORANGE = HexColor('#f0ab00')
    LIGHT_GRAY = HexColor('#F5F5F5')
    GRAY_TEXT = HexColor('#808080')
    BLUE_TEXT = HexColor('#2E5090')
    RED_TEXT = HexColor('#C0392B')
    GOLD_BG = HexColor('#FEF3C7')
    BLUE_BAR = HexColor('#4A90D9')
    LIGHT_BLUE = HexColor('#4A90D9')  # For dotted separator lines
    
    # Get data
    customer_name = salesorder.customer_name or ""
    customer_code = salesorder.customer_code or ""
    # Get bp_reference_no - ensure we get the actual value
    bp_reference = str(salesorder.bp_reference_no).strip() if salesorder.bp_reference_no else ""
    posting_date = salesorder.posting_date.strftime('%d.%m.%y') if salesorder.posting_date else ''
    # Get LPO Date from PI (format: DD.MM.YY)
    lpo_date = pi.lpo_date.strftime('%d.%m.%y') if pi.lpo_date else ""
    salesman = salesorder.salesman_name or ""
    
    # Get VAT Number from salesorder (from Excel upload)
    vat_number = ""
    if salesorder.vat_number:
        vat_str = str(salesorder.vat_number).strip()
        # Remove "nan" string and .0 suffix
        if vat_str.lower() not in ('nan', 'none', ''):
            # Remove .0 suffix if present
            if vat_str.endswith('.0'):
                vat_str = vat_str[:-2]
            vat_number = vat_str
    
    # Get customer address and phone from SAPSalesorder (from API)
    customer_address = salesorder.customer_address or ""
    customer_tel = salesorder.customer_phone or ""
    
    # Also try to get from Customer model as fallback
    customer_email = ""
    customer_po_box = ""
    customer_fax = ""
    if customer_code:
        try:
            from .models import Customer
            customer = Customer.objects.filter(customer_code=customer_code).first()
            if customer:
                # Use Customer model data if not available in SAPSalesorder
                if not customer_address and customer.address:
                    customer_address = customer.address
                if not customer_tel and customer.phone_number:
                    customer_tel = customer.phone_number
                # These fields may not exist in Customer model, but check anyway
                customer_email = getattr(customer, 'email', '') or ""
                customer_po_box = getattr(customer, 'po_box', '') or ""
                customer_fax = getattr(customer, 'fax', '') or ""
        except Exception:
            pass
    
    # Estimate total pages (rough calculation)
    items_per_first_page = 10
    items_per_continuation_page = 20
    total_items = len(items_data)
    
    if total_items <= items_per_first_page:
        total_pages = 1
    else:
        remaining_items = total_items - items_per_first_page
        additional_pages = (remaining_items + items_per_continuation_page - 1) // items_per_continuation_page
        total_pages = 1 + additional_pages
    
    # Generate PDF
    response = HttpResponse(content_type="application/pdf")
    response['Content-Disposition'] = f'attachment; filename="PI_{pi_number}.pdf"'
    
    buffer = BytesIO()
    
    # Create custom document with page templates
    # A4 size: 8.27 x 11.69 inches
    # Use consistent margins for proper alignment
    margin = 0.3*inch
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )
    
    # Calculate available width for content (A4 width - left margin - right margin)
    available_width = A4[0] - (2 * margin)  # 8.27 - 0.6 = 7.67 inches
    
    # Define frames for first page and subsequent pages
    first_page_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id='first_page_frame'
    )
    
    # Subsequent pages have smaller content area due to header
    later_page_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height - 0.8*inch,  # Reduced height for continuation header
        id='later_page_frame',
        topPadding=0.8*inch  # Add padding at top for header
    )
    
    # Create header/footer handler
    header_handler = HeaderFooterCanvas(pi_number, posting_date, total_pages)
    
    # Create page templates
    first_page_template = PageTemplate(
        id='first_page',
        frames=[first_page_frame],
        onPage=lambda canvas, doc: None  # No header on first page
    )
    
    later_page_template = PageTemplate(
        id='later_pages',
        frames=[later_page_frame],
        onPage=header_handler
    )
    
    doc.addPageTemplates([first_page_template, later_page_template])
    
    elements = []
    pdf_styles = getSampleStyleSheet()
    
    # Custom styles - All font sizes reduced to match items table
    company_style = ParagraphStyle(
        'CompanyName',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,  # Reduced from 11
        textColor=DARK_BLUE,
        leading=11,
    )
    
    address_style = ParagraphStyle(
        'Address',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=7,  # Reduced from 9
        textColor=colors.black,
        leading=9,
    )
    
    normal_style = ParagraphStyle(
        'NormalPI',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=8,  # Reduced from 9
        textColor=colors.black,
    )
    
    bold_style = ParagraphStyle(
        'BoldPI',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,  # Reduced from 9
        textColor=colors.black,
    )
    
    small_style = ParagraphStyle(
        'SmallPI',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        textColor=colors.black,
    )
    
    gray_label = ParagraphStyle(
        'GrayLabel',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=7,  # Reduced from 8
        textColor=GRAY_TEXT,
    )
    
    title_style = ParagraphStyle(
        'PITitle',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,  # Reduced from 14
        textColor=DARK_BLUE,
    )
    
    customer_bold = ParagraphStyle(
        'CustomerBold',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,  # Reduced from 10
        textColor=colors.black,
    )
    
    email_style = ParagraphStyle(
        'EmailStyle',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=7,  # Reduced from 8
        textColor=HexColor('#0066CC'),
    )
    
    # ============ HELPER FUNCTION FOR SECTION HEADERS WITH LEFT BAR ============
    def create_section_header(text, bar_color=ORANGE, width=3*inch):
        """Creates a header with colored vertical bar on the left"""
        header_table = Table([
            ["", Paragraph(f"<b>{text}</b>", bold_style)]
        ], colWidths=[0.08*inch, width - 0.08*inch])
        header_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), bar_color),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (0, 0), (0, 0), 0),
            ('LEFTPADDING', (1, 0), (1, 0), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        return header_table
    
    # ============ LOAD LOGO ============
    logo_path = os.path.join(settings.BASE_DIR, 'media', 'footer-logo1.png')
    logo_img = None
    if os.path.exists(logo_path):
        try:
            # Bigger logo as per template
            logo_img = Image(logo_path, width=2.3*inch, height=0.9*inch)
        except Exception:
            logo_img = None
    
    # ============ FIRST PAGE HEADER SECTION ============
    # Left side - Logo and company info
    left_elements = []
    
    if logo_img:
        left_elements.append([logo_img])
    else:
        left_elements.append([Paragraph("<b>JUNAID</b>", company_style)])
    
    left_elements.append([Spacer(1, 0.1*inch)])
    left_elements.append([Paragraph("<b>Junaid Sanitary & Electrical Materials Trading (L.L.C)</b>", company_style)])
    left_elements.append([Spacer(1, 0.05*inch)])
    left_elements.append([Paragraph("Dubai Investment Park-2, Dubai", address_style)])
    left_elements.append([Paragraph("04-2367723", address_style)])
    left_elements.append([Paragraph("100225006400003", address_style)])
    
    left_table = Table(left_elements, colWidths=[3*inch])
    left_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    
    # Right side - Title with orange bar on left
    title_with_bar = Table([
        ["", Paragraph("<b>PROFORMA INVOICE</b>", title_style)]
    ], colWidths=[0.1*inch, 3.9*inch])
    title_with_bar.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), ORANGE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),
        ('RIGHTPADDING', (0, 0), (0, 0), 0),
        ('LEFTPADDING', (1, 0), (1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    
    # Document info rows
    doc_info_header = Table([
        [Paragraph("Document Number", gray_label), 
         Paragraph("Document Date", gray_label), 
         Paragraph("Page", gray_label)],
    ], colWidths=[1.5*inch, 1.3*inch, 0.7*inch])
    
    doc_info_values = Table([
        [Paragraph(f"<b>{pi_number}</b>", bold_style), 
         Paragraph(f"<b>{posting_date}</b>", bold_style), 
         Paragraph(f"<b>1/{total_pages}</b>", bold_style)],
    ], colWidths=[1.5*inch, 1.3*inch, 0.7*inch])
    
    # Customer No & VAT Number row
    cust_vat_header = Table([
        [Paragraph("Customer No.", gray_label), 
         Paragraph("VAT Number - Business Partner", gray_label)],
    ], colWidths=[1.3*inch, 2.2*inch])
    
    vat_box_style = ParagraphStyle(
        'VATBox',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,  # Reduced from 9
        textColor=BLUE_TEXT,
    )
    
    arrow_cell = Table([
        [Paragraph("<font color='#E67E22'>➡</font>", ParagraphStyle('Arrow', fontSize=8)),  # Reduced from 10
         Paragraph(f"<b>{customer_code}</b>", bold_style)]
    ], colWidths=[0.2*inch, 1*inch])
    arrow_cell.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    
    vat_box = Table([
        [Paragraph(f"<b>{vat_number}</b>", vat_box_style)]
    ], colWidths=[2*inch])
    vat_box.setStyle(TableStyle([
        ('BOX', (0, 0), (0, 0), 1, BLUE_TEXT),
        ('LEFTPADDING', (0, 0), (0, 0), 3),
        ('TOPPADDING', (0, 0), (0, 0), 2),
        ('BOTTOMPADDING', (0, 0), (0, 0), 2),
    ]))
    
    cust_vat_values = Table([
        [arrow_cell, vat_box],
    ], colWidths=[1.3*inch, 2.2*inch])
    cust_vat_values.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    
    # LPO Date and LPO Ref row (removed Your Reference)
    ref_header = Table([
        [Paragraph("LPO DATE", gray_label), 
         Paragraph("LPO Ref", gray_label)],
    ], colWidths=[1.5*inch, 2.5*inch])
    
    ref_values = Table([
        [Paragraph(f"<b>{lpo_date}</b>", bold_style), 
         Paragraph(f"<b>{bp_reference}</b>", bold_style)],  # BP Reference in LPO Ref
    ], colWidths=[1.5*inch, 2.5*inch])
    
    # Your Contact row
    contact_header = Table([
        [Paragraph("Your Contact", gray_label)],
    ], colWidths=[3.5*inch])
    
    contact_values = Table([
        [Paragraph(f"<b>{salesman}</b>", bold_style)],
    ], colWidths=[3.5*inch])
    
    # Combine right column
    right_elements = [
        [title_with_bar],
        [Spacer(1, 0.05*inch)],
        [doc_info_header],
        [doc_info_values],
        [Spacer(1, 0.1*inch)],
        [cust_vat_header],
        [cust_vat_values],
        [Spacer(1, 0.1*inch)],
        [ref_header],
        [ref_values],
        [Spacer(1, 0.1*inch)],
        [contact_header],
        [contact_values],
    ]
    
    right_table = Table(right_elements, colWidths=[4*inch])
    right_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    # Combine header - use available width for proper alignment
    header_row = Table([[left_table, right_table]], colWidths=[3.2*inch, available_width - 3.2*inch])
    header_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    elements.append(header_row)
    elements.append(Spacer(1, 0.05*inch))  # Reduced from 0.1
    
    # ============ BLUE DOTTED SEPARATOR LINE ============
    # Create a blue dotted horizontal line matching available width
    separator_line = Drawing(available_width, 2)
    separator_line.add(Line(0, 1, available_width, 1, strokeColor=LIGHT_BLUE, strokeWidth=1, strokeDashArray=[2, 2]))
    elements.append(separator_line)
    elements.append(Spacer(1, 0.05*inch))  # Reduced from 0.1
    
    # ============ CUSTOMER DETAILS SECTION ============
    cust_left = []
    cust_left.append([Paragraph(f"<b>{customer_name}</b>", customer_bold)])
    cust_left.append([Spacer(1, 0.05*inch)])
    # Add address if available (from API)
    if customer_address:
        cust_left.append([Paragraph(f"{customer_address}", address_style)])
        cust_left.append([Spacer(1, 0.05*inch)])
    # Add PO BOX if available
    if customer_po_box:
        cust_left.append([Paragraph(f"PO BOX {customer_po_box}", address_style)])
        cust_left.append([Spacer(1, 0.05*inch)])
    # Add TEL if available (from API)
    if customer_tel:
        cust_left.append([Paragraph(f"TEL: {customer_tel}", address_style)])
    # Add FAX if available
    if customer_fax:
        cust_left.append([Paragraph(f"FAX: {customer_fax}", address_style)])
    # Add email if available
    if customer_email:
        cust_left.append([Spacer(1, 0.05*inch)])
        cust_left.append([Paragraph(f"{customer_email}", email_style)])
    
    cust_left_table = Table(cust_left, colWidths=[3.2*inch])
    cust_left_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    
    cust_right = []
    cust_right.append([Paragraph("Delivery Address", gray_label)])
    cust_right.append([Spacer(1, 0.05*inch)])
    cust_right.append([Paragraph(f"<b>{customer_name}</b>", customer_bold)])
    
    cust_right_table = Table(cust_right, colWidths=[4*inch])
    cust_right_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    
    cust_row = Table([[cust_left_table, cust_right_table]], colWidths=[3.2*inch, available_width - 3.2*inch])
    cust_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    elements.append(cust_row)
    elements.append(Spacer(1, 0.08*inch))  # Reduced from 0.15
    
    # ============ BLUE DOTTED SEPARATOR LINE ============
    separator_line2 = Drawing(available_width, 2)
    separator_line2.add(Line(0, 1, available_width, 1, strokeColor=LIGHT_BLUE, strokeWidth=1, strokeDashArray=[2, 2]))
    elements.append(separator_line2)
    elements.append(Spacer(1, 0.05*inch))  # Reduced from 0.1
    
    # ============ CURRENCY LABEL ============
    currency_style_right = ParagraphStyle('Currency', fontSize=8, alignment=TA_RIGHT)  # Reduced from 9
    currency_label = Table([
        ["", Paragraph("<b>Currency:</b>AED", currency_style_right)]
    ], colWidths=[available_width - 1.5*inch, 1.5*inch])
    currency_label.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(currency_label)
    elements.append(Spacer(1, 0.05*inch))
    
    # ============ LINE ITEMS TABLE ============
    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7,  # Reduced from 8
        textColor=colors.black,
        alignment=TA_CENTER,
    )
    
    arrow_style = ParagraphStyle(
        'ArrowStyle',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=7,  # Reduced from 9
        textColor=ORANGE,
    )
    
    red_style = ParagraphStyle(
        'RedStyle',
        parent=pdf_styles['Normal'],
        fontName='Helvetica',
        fontSize=7,  # Reduced from 8 to match items table
        textColor=RED_TEXT,
        alignment=TA_CENTER,
    )
    
    line_num_style = ParagraphStyle(
        'LineNumStyle',
        parent=pdf_styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        textColor=ORANGE,
        alignment=TA_CENTER,
    )
    
    # Build table data
    table_data = [[
        "",
        Paragraph("<b>Description</b>", table_header_style),
        Paragraph("<b>Item Code</b>", table_header_style),
        Paragraph("<b>Quantity</b>", table_header_style),
        Paragraph("<b>UoM</b>", table_header_style),
        Paragraph("<b>Price</b>", table_header_style),
        Paragraph("<b>Tax %</b>", table_header_style),
        Paragraph("<b>Total</b>", table_header_style),
    ]]
    
    for item in items_data:
        arrow_code = Table([
            [Paragraph("➡", arrow_style), Paragraph(item['item_no'], small_style)]
        ], colWidths=[0.15*inch, 0.65*inch])
        arrow_code.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        
        table_data.append([
            Paragraph(f"<b>{item['line_num']}</b>", line_num_style),
            Paragraph(item['description'], small_style),
            arrow_code,
            Paragraph(f"{item['quantity']:,.0f}", ParagraphStyle('Qty', fontSize=7, alignment=TA_CENTER)),  # Reduced from 8
            Paragraph(item['uom'], ParagraphStyle('UoM', fontSize=7, alignment=TA_CENTER)),  # Reduced from 8
            Paragraph(f"{item['unit_price']:,.2f}", ParagraphStyle('Price', fontSize=7, alignment=TA_RIGHT)),  # Reduced from 8
            Paragraph(f"{item['tax_rate']:.2f}", red_style),
            Paragraph(f"{item['line_total']:,.2f}", ParagraphStyle('Total', fontSize=7, alignment=TA_RIGHT)),  # Reduced from 8
        ])
    
    # Calculate column widths to fit available width
    # Line, Description, Item Code, Quantity, UoM, Price, Tax %, Total
    # Reduced Tax % and Total widths, increased Description width
    base_widths = [0.35*inch, 2.65*inch, 0.85*inch, 0.7*inch, 0.5*inch, 0.7*inch, 0.4*inch, 0.85*inch]
    total_base_width = sum(base_widths)
    if total_base_width > available_width:
        # Scale down proportionally if needed
        scale = available_width / total_base_width
        col_widths = [w * scale for w in base_widths]
    else:
        # Adjust last column to fill remaining space
        col_widths = base_widths.copy()
        col_widths[-1] = available_width - sum(col_widths[:-1])
    
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_GRAY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),  # Reduced from 8 to fit more items
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, HexColor('#CCCCCC')),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, HexColor('#EEEEEE')),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 1), (-1, -1), 7),  # Reduced from 8 to fit more items
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),  # Reduced from 3
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),  # Reduced from 3
        ("TOPPADDING", (0, 0), (-1, -1), 3),  # Reduced from 6 to fit more rows
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),  # Reduced from 6 to fit more rows
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (3, 1), (3, -1), "CENTER"),
        ("ALIGN", (4, 1), (4, -1), "CENTER"),
        ("ALIGN", (5, 1), (5, -1), "RIGHT"),
        ("ALIGN", (6, 1), (6, -1), "CENTER"),
        ("ALIGN", (7, 1), (7, -1), "RIGHT"),
    ]))
    
    elements.append(items_table)
    elements.append(Spacer(1, 0.1*inch))  # Reduced spacing from 0.2
    
    # ============ TAX DETAILS & TOTALS SECTION ============
    # Tax Details header with left bar
    tax_header = create_section_header("Tax Details", ORANGE, 1.5*inch)
    
    # Tax Details table with borders
    tax_table_header = [
        Paragraph("<b>Tax %</b>", ParagraphStyle('TaxTH', fontSize=7, alignment=TA_CENTER)),  # Reduced from 8
        Paragraph("<b>Base Amount</b>", ParagraphStyle('TaxTH', fontSize=7, alignment=TA_CENTER)),  # Reduced from 8
        Paragraph("<b>Tax</b>", ParagraphStyle('TaxTH', fontSize=7, alignment=TA_CENTER)),  # Reduced from 8
        Paragraph("<b>Gross</b>", ParagraphStyle('TaxTH', fontSize=7, alignment=TA_CENTER)),  # Reduced from 8
    ]
    tax_table_values = [
        f"{vat_rate:.2f}",
        f"{total_before_tax:,.2f}",
        f"{vat_amount:,.2f}",
        f"{grand_total:,.2f}",
    ]
    
    tax_table = Table([tax_table_header, tax_table_values], colWidths=[0.6*inch, 1.2*inch, 0.9*inch, 1.1*inch])
    tax_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7),  # Reduced from 8
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 1, HexColor('#CCCCCC')),  # Outer border
        ("LINEBELOW", (0, 0), (-1, 0), 1, HexColor('#CCCCCC')),  # Line below header
        ("LINEBEFORE", (1, 0), (1, -1), 0.5, HexColor('#CCCCCC')),  # Vertical lines
        ("LINEBEFORE", (2, 0), (2, -1), 0.5, HexColor('#CCCCCC')),
        ("LINEBEFORE", (3, 0), (3, -1), 0.5, HexColor('#CCCCCC')),
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    
    # Totals on right side with blue dotted lines
    totals_label_style = ParagraphStyle('TotalsLabel', fontSize=8, fontName='Helvetica-Bold', textColor=HexColor('#333333'))  # Reduced from 9
    totals_value_style = ParagraphStyle('TotalsValue', fontSize=8, fontName='Helvetica-Bold', alignment=TA_RIGHT, textColor=HexColor('#1E3A5F'))  # Reduced from 9
    
    # Create totals with dotted line separators between label and value
    # Show discount percentage if discount exists (positive or negative)
    if discount_percent != 0:
        discount_label = f"<b>Discount Subtotal:</b> {discount_percent:.2f}%"
    else:
        discount_label = "<b>Discount Subtotal:</b>"
    
    # Format discount amount with proper sign (negative discounts will show as negative)
    discount_display = f"<b>AED {discount_amount:,.2f}</b>"
    
    totals_data = [
        [Paragraph("<b>Order Subtotal:</b>", totals_label_style), Paragraph(f"<b>AED {subtotal:,.2f}</b>", totals_value_style)],
        [Paragraph(discount_label, totals_label_style), Paragraph(discount_display, totals_value_style)],
        [Paragraph("<b>Total Before Tax:</b>", totals_label_style), Paragraph(f"<b>AED {total_before_tax:,.2f}</b>", totals_value_style)],
        [Paragraph("<b>Total Tax Amount:</b>", totals_label_style), Paragraph(f"<b>AED {vat_amount:,.2f}</b>", totals_value_style)],
        [Paragraph("<b>Total Amount:</b>", totals_label_style), Paragraph(f"<b>AED {grand_total:,.2f}</b>", totals_value_style)],
    ]
    
    totals_table = Table(totals_data, colWidths=[1.5*inch, 1.5*inch])
    totals_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),  # Reduced from 9
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        # Blue dotted lines between rows
        ("LINEBELOW", (0, 0), (-1, 0), 1, LIGHT_BLUE, 1, (2, 2)),  # dotted line
        ("LINEBELOW", (0, 1), (-1, 1), 1, LIGHT_BLUE, 1, (2, 2)),
        ("LINEBELOW", (0, 2), (-1, 2), 1, LIGHT_BLUE, 1, (2, 2)),
        ("LINEBELOW", (0, 3), (-1, 3), 1, LIGHT_BLUE, 1, (2, 2)),
        # Yellow/gold background for Total Amount row
        ("BACKGROUND", (0, -1), (-1, -1), GOLD_BG),
    ]))
    
    # Combine tax section
    left_section = Table([
        [tax_header],
        [Spacer(1, 0.08*inch)],
        [tax_table]
    ], colWidths=[3.8*inch])
    left_section.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    # Adjust summary row to use available width with proper spacing
    left_section_width = available_width * 0.55  # 55% for tax details
    right_section_width = available_width * 0.45  # 45% for totals
    summary_row = Table([[left_section, totals_table]], colWidths=[left_section_width, right_section_width])
    summary_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),  # Align totals to right
    ]))
    elements.append(summary_row)
    elements.append(Spacer(1, 0.2*inch))
    
    # ============ ADDITIONAL EXPENSES ============
    exp_header = create_section_header("Additional Expenses", ORANGE, 1.8*inch)
    
    # Additional expenses row with proper spacing
    shipping_label = Table([
        [exp_header, "", Paragraph("<b>Shipping Type:</b>", bold_style), ""]
    ], colWidths=[2*inch, available_width - 4.5*inch, 1.2*inch, 1.3*inch])
    shipping_label.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('ALIGN', (2, 0), (2, 0), 'RIGHT'),
    ]))
    elements.append(shipping_label)
    elements.append(Spacer(1, 0.15*inch))
    
    # ============ TERMS & CONDITIONS ============
    terms_text = pi.remarks if pi.remarks else ""
    
    # Terms header with left bar (blue) - use available width
    terms_header = Table([
        ["", Paragraph("<b>TERMS & CONDITIONS:-</b>", bold_style)]
    ], colWidths=[0.08*inch, available_width - 0.08*inch])
    terms_header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), BLUE_BAR),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),
        ('RIGHTPADDING', (0, 0), (0, 0), 0),
        ('LEFTPADDING', (1, 0), (1, 0), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(terms_header)
    
    if terms_text:
        # Split text by newlines and format each line separately
        lines = terms_text.split('\n')
        terms_rows = []
        for line in lines:
            line = line.strip()
            if line:  # Only add non-empty lines
                terms_rows.append([Paragraph(line, normal_style)])
            else:
                # Add empty row for spacing
                terms_rows.append([Paragraph("&nbsp;", normal_style)])
        
        if terms_rows:
            terms_content = Table(terms_rows, colWidths=[available_width])
            terms_content.setStyle(TableStyle([
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            elements.append(terms_content)
    
    elements.append(Spacer(1, 0.3*inch))
    
    # ============ FOOTER ============
    footer_text = Table([
        [Paragraph("<i>With Best Regards,</i>", normal_style)],
        [Spacer(1, 0.15*inch)],
        [Paragraph("Prepared By <b>ANISH/ADIL</b>- sales support email <b>sales@junaid.ae</b>", normal_style)],
    ], colWidths=[available_width])
    footer_text.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(footer_text)
    
    # Build PDF with page template switching
    def on_first_page(canvas, doc):
        pass  # No special header on first page
    
    def on_later_pages(canvas, doc):
        header_handler(canvas, doc)
    
    # Use multiBuild to handle page template switching
    from reportlab.platypus.doctemplate import NextPageTemplate
    
    # Insert template switch after first page worth of content
    # This is a simplified approach - the actual page break will be handled automatically
    
    doc.build(elements)
    
    pdf_content = buffer.getvalue()
    buffer.close()
    response.write(pdf_content)
    return response
    


@login_required
def pi_list(request):
    """
    List all Proforma Invoices with filtering and search capabilities.
    """
    # Scope by logged-in user - filter PIs by their salesorder's salesman scope
    qs = SAPProformaInvoice.objects.filter(
        salesorder__in=SAPSalesorder.objects.filter(salesman_scope_q_salesorder(request.user))
    ).select_related('salesorder')

    # Filters
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    so_number_filter = request.GET.get('so_number', '').strip()
    salesman_filter = request.GET.getlist('salesman')  # Gets ['Name1', 'Name2']

    # Apply Salesman Filter
    if salesman_filter:
        clean_salesmen = [s for s in salesman_filter if s.strip()]
        if clean_salesmen:
            qs = qs.filter(salesorder__salesman_name__in=clean_salesmen)

    # Search filter
    if q:
        if q.isdigit():
            # Search by PI number or SO number
            qs = qs.filter(
                Q(pi_number__icontains=q) |
                Q(salesorder__so_number__icontains=q)
            )
        elif len(q) < 3:
            qs = qs.filter(
                Q(salesorder__customer_name__istartswith=q) |
                Q(salesorder__salesman_name__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(pi_number__icontains=q) |
                Q(salesorder__so_number__icontains=q) |
                Q(salesorder__customer_name__icontains=q) |
                Q(salesorder__salesman_name__icontains=q)
            )

    # Status filter (use SO status, not PI status)
    if status:
        s = status.strip().upper()
        if s in ("OPEN", "O"):
            qs = qs.filter(salesorder__status__in=['O', 'OPEN'])
        elif s in ("CLOSED", "C"):
            qs = qs.filter(salesorder__status__in=['C', 'CLOSED'])
        else:
            qs = qs.filter(salesorder__status__iexact=status)

    # SO Number filter
    if so_number_filter:
        qs = qs.filter(salesorder__so_number__icontains=so_number_filter)

    # Date filters
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
        qs = qs.filter(pi_date__gte=start_date)
    if end_date:
        qs = qs.filter(pi_date__lte=end_date)

    # Order by most recent first (by pi_date, then created_at for fallback)
    qs = qs.order_by('-pi_date', '-created_at', '-sequence')

    # Pagination
    try:
        page_size = int(request.GET.get('page_size', 100))
    except ValueError:
        page_size = 20
    page_size = max(5, min(page_size, 100))
    paginator = Paginator(qs, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Calculate total count
    total_count = paginator.count

    # Distinct salesmen list (restricted to the same scope)
    salesmen = (
        SAPSalesorder.objects.filter(salesman_scope_q_salesorder(request.user))
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
        .order_by('salesman_name')
    )

    return render(request, 'salesorders/pi_list.html', {
        'page_obj': page_obj,
        'total_count': total_count,
        'salesmen': salesmen,
        'filters': {
            'q': q,
            'status': status,
            'start': start,
            'end': end,
            'so_number': so_number_filter,
            'salesman': salesman_filter,
            'page_size': page_size,
        }
    })


@login_required
def old_pi_list(request):
    """
    List old PIs - SAP Quotations where salesman_name='PI' and status='Open'.
    These are the old PIs that were previously done in quotations.
    """
    # Scope by logged-in user - filter quotations by salesman scope
    qs = SAPQuotation.objects.filter(
        salesman_scope_q(request.user)
    ).filter(
        salesman_name__iexact='PI',
        status__iexact='Open'
    ).select_related()

    # Filters
    q = request.GET.get('q', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()

    # Search filter
    if q:
        if q.isdigit():
            # Search by quotation number
            qs = qs.filter(q_number__icontains=q)
        elif len(q) < 3:
            qs = qs.filter(customer_name__istartswith=q)
        else:
            qs = qs.filter(
                Q(q_number__icontains=q) |
                Q(customer_name__icontains=q)
            )

    # Date filters
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

    # Order by most recent first
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

    # Calculate total count
    total_count = paginator.count

    return render(request, 'salesorders/old_pi_list.html', {
        'page_obj': page_obj,
        'total_count': total_count,
        'filters': {
            'q': q,
            'start': start,
            'end': end,
            'page_size': page_size,
        }
    })


@login_required
def pi_detail(request, pi_number):
    """
    View Proforma Invoice details (read-only).
    """
    pi = get_object_or_404(SAPProformaInvoice, pi_number=pi_number)
    
    # Enforce scope
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=pi.salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Proforma Invoice not found")
    
    salesorder = pi.salesorder
    
    # Get PI lines with calculated unit prices
    pi_lines = []
    for line in pi.lines.all().order_by('line_no', 'id'):
        so_item = line.so_item
        if not so_item:
            # Fallback for old PIs: try to find by salesorder and line_no
            so_item = SAPSalesorderItem.objects.filter(
                salesorder=salesorder,
                line_no=line.line_no
            ).first()
        
        if not so_item:
            # Second fallback: try by item_no and line_no
            so_item = SAPSalesorderItem.objects.filter(
                salesorder=salesorder,
                item_no=line.item_no,
                line_no=line.line_no
            ).first()
        
        if so_item:
            qty = so_item.quantity or Decimal("0")
            row_total = so_item.row_total or Decimal("0")
            # Always calculate from row_total/qty for accuracy (price field may not be reliable)
            if qty and qty != 0 and row_total:
                unit_price = (row_total / qty).quantize(Decimal("0.01"))
            else:
                unit_price = Decimal("0.00")
        else:
            unit_price = Decimal("0.00")
        
        line_total = (unit_price * line.quantity).quantize(Decimal("0.01"))
        pi_lines.append({
            'line': line,
            'unit_price': unit_price,
            'line_total': line_total,
        })
    
    # Calculate totals (keep same logic for PI)
    subtotal = sum(item['line_total'] for item in pi_lines)
    # Use exact discount_percentage for calculations (from API)
    discount_percentage_exact = salesorder.discount_percentage or Decimal("0.00")
    discount_percentage_display = round(float(discount_percentage_exact), 1)  # Round to 1 decimal for display
    discount_amount = (subtotal * discount_percentage_exact / 100).quantize(Decimal("0.01"))
    total_before_tax = (subtotal - discount_amount).quantize(Decimal("0.01"))
    vat_rate = Decimal("0.05")
    vat_amount = (total_before_tax * vat_rate).quantize(Decimal("0.01"))
    grand_total = (total_before_tax + vat_amount).quantize(Decimal("0.01"))
    
    return render(request, 'salesorders/pi_detail.html', {
        'pi': pi,
        'salesorder': salesorder,
        'pi_lines': pi_lines,
        'subtotal': subtotal,
        'discount_percentage': discount_percentage_display,  # Display rounded to 1 decimal
        'discount_percentage_exact': discount_percentage_exact,  # Exact value for calculations
        'discount_amount': discount_amount,
        'total_before_tax': total_before_tax,
        'vat_amount': vat_amount,
        'grand_total': grand_total,
    })


@login_required
@require_POST
def cancel_pi(request, pi_number):
    """
    Cancel a Proforma Invoice (mark as CANCELLED).
    This releases the allocated quantities back to available balance.
    """
    pi = get_object_or_404(SAPProformaInvoice, pi_number=pi_number)
    
    # Enforce scope
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=pi.salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Proforma Invoice not found")
    
    if pi.status == 'CANCELLED':
        messages.warning(request, f"PI {pi_number} is already cancelled.")
    else:
        pi.status = 'CANCELLED'
        pi.save(update_fields=['status'])
        
        # Create log entry for cancellation
        ip = get_client_ip(request)
        network_label = label_network(ip)
        ua_string = request.META.get('HTTP_USER_AGENT', '')[:500]
        device_type, device_os, device_browser = parse_device_info(ua_string)
        
        try:
            lat = request.POST.get("location_lat")
            lng = request.POST.get("location_lng")
            lat_val = float(lat) if lat not in (None, "",) else None
            lng_val = float(lng) if lng not in (None, "",) else None
        except (ValueError, TypeError):
            lat_val = None
            lng_val = None
        
        ProformaInvoiceLog.objects.create(
            pi=pi,
            user=request.user if request.user.is_authenticated else None,
            ip_address=ip,
            user_agent=ua_string,
            device_type=device_type,
            device_os=device_os,
            device_browser=device_browser,
            location_lat=lat_val,
            location_lng=lng_val,
            network_label=network_label,
            device=getattr(request, 'device_obj', None),
            action="cancelled",
        )
        
        messages.success(request, f"PI {pi_number} has been cancelled. Quantities are now available.")
    
    return redirect("salesorder_detail", so_number=pi.salesorder.so_number)


@login_required
def edit_pi(request, pi_number):
    """
    Edit a Proforma Invoice.
    GET: Show edit form with current PI data.
    POST: Update PI (items, quantities, remarks) and create log entry.
    """
    pi = get_object_or_404(SAPProformaInvoice, pi_number=pi_number)
    
    # Enforce scope
    if not (request.user.is_superuser or request.user.is_staff):
        allowed = SAPSalesorder.objects.filter(
            Q(pk=pi.salesorder.pk) & salesman_scope_q_salesorder(request.user)
        ).exists()
        if not allowed:
            raise Http404("Proforma Invoice not found")
    
    # Only allow editing PIs for OPEN Sales Orders (use SO status)
    so_status = (pi.salesorder.status or '').strip().upper()
    if so_status not in ('O', 'OPEN'):
        messages.error(request, f"Cannot edit {pi_number} - Sales Order is Closed.")
        return redirect("salesorder_detail", so_number=pi.salesorder.so_number)
    
    salesorder = pi.salesorder
    items = salesorder.items.all().order_by('line_no', 'id')
    
    # Get current PI lines
    current_pi_lines = {line.so_item_id: line for line in pi.lines.all() if line.so_item_id}
    
    # Calculate allocated and remaining quantities per line (excluding current PI)
    allocated = _get_allocated_quantities(salesorder.so_number)
    # Subtract current PI quantities from allocated to show what's available for editing
    for line in pi.lines.all():
        if line.so_item_id:
            item_id = line.so_item_id
            if item_id in allocated:
                allocated[item_id] = max(Decimal("0"), allocated[item_id] - line.quantity)
    
    # Prepare items with remaining quantities and current PI quantities
    items_with_data = []
    for item in items:
        allocated_qty = allocated.get(item.id, Decimal("0"))
        # Remaining after OTHER PIs (excluding current PI)
        remaining_qty = max(Decimal("0"), item.quantity - allocated_qty)
        
        # Get current PI quantity for this item
        current_pi_line = current_pi_lines.get(item.id)
        current_pi_qty = current_pi_line.quantity if current_pi_line else Decimal("0")
        
        # Available for editing = SO Qty - (allocated from other PIs)
        # This is the maximum we can set, including the current PI qty
        # So if SO=12, other PIs=0, we can set up to 12 (including current PI's 1)
        available_for_edit = item.quantity - allocated_qty
        
        # Derive unit price
        qty = item.quantity or Decimal("0")
        row_total = item.row_total or Decimal("0")
        unit_price = (row_total / qty).quantize(Decimal("0.01")) if qty and qty != 0 else Decimal("0.00")
        
        items_with_data.append({
            'item': item,
            'allocated_qty': allocated_qty,
            'remaining_qty': remaining_qty,
            'current_pi_qty': current_pi_qty,
            'available_for_edit': available_for_edit,
            'unit_price': unit_price,
            'is_in_pi': current_pi_line is not None,
        })
    
    if request.method == 'POST':
        # Validate and update PI
        selected_lines = request.POST.getlist('line_ids')
        quantities = {}
        
        errors = []
        
        for line_id in selected_lines:
            qty_str = request.POST.get(f'qty_{line_id}', '0').strip()
            try:
                qty = Decimal(qty_str)
                if qty <= 0:
                    continue
                quantities[int(line_id)] = qty
            except (ValueError, TypeError):
                errors.append(f"Invalid quantity for line {line_id}")
        
        if errors:
            messages.error(request, "; ".join(errors))
            return render(request, 'salesorders/pi_edit.html', {
                'pi': pi,
                'salesorder': salesorder,
                'items_with_data': items_with_data,
            })
        
        # Get remarks
        remarks = request.POST.get('remarks', '').strip()
        if not remarks:
            remarks = "Note: Cheque to be prepared in favor of: \n1) JUNAID SANITARY & ELECTRICAL MAT. TRDG. LLC \nTax Registration Number 100225006400003\n2) PAYMENT : CDC Against Delivery\n3)  DELIVERY: Ex-Stock Subject to Receipt of cheque copy against this Proforma Invoice within 4 working days"
        
        # Validate quantities don't exceed available
        with transaction.atomic():
            # Lock the SO to prevent race conditions
            salesorder = SAPSalesorder.objects.select_for_update().get(so_number=salesorder.so_number)
            
            # Recalculate allocated quantities after locking
            allocated = _get_allocated_quantities(salesorder.so_number)
            # Subtract current PI quantities
            for line in pi.lines.all():
                if line.so_item_id:
                    item_id = line.so_item_id
                    if item_id in allocated:
                        allocated[item_id] = max(Decimal("0"), allocated[item_id] - line.quantity)
            
            items_map = {item.id: item for item in salesorder.items.all()}
            for line_id, qty in quantities.items():
                item = items_map.get(line_id)
                if not item:
                    errors.append(f"Line {line_id} not found")
                    continue
                # allocated already excludes current PI, so this is the max we can allocate
                allocated_qty = allocated.get(item.id, Decimal("0"))
                max_available = item.quantity - allocated_qty
                if qty > max_available:
                    errors.append(f"Quantity {qty} exceeds maximum available {max_available} for line {item.line_no} (SO Qty: {item.quantity}, Other PIs: {allocated_qty})")
            
            if errors:
                messages.error(request, "; ".join(errors))
                return render(request, 'salesorders/pi_edit.html', {
                    'pi': pi,
                    'salesorder': salesorder,
                    'items_with_data': items_with_data,
                })
            
            # Get LPO Date
            lpo_date_str = request.POST.get('lpo_date', '').strip()
            lpo_date = None
            if lpo_date_str:
                try:
                    from datetime import datetime
                    lpo_date = datetime.strptime(lpo_date_str, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    lpo_date = None
            
            # Update PI remarks and lpo_date
            pi.remarks = remarks
            pi.lpo_date = lpo_date
            pi.save(update_fields=['remarks', 'lpo_date'])
            
            # Delete existing PI lines
            pi.lines.all().delete()
            
            # Create new PI lines
            pi_lines = []
            for line_id, qty in quantities.items():
                item = SAPSalesorderItem.objects.get(id=line_id, salesorder=salesorder)
                pi_lines.append(
                    SAPProformaInvoiceLine(
                        pi=pi,
                        so_item=item,
                        so_number=salesorder.so_number,
                        line_no=item.line_no,
                        item_no=item.item_no or "",
                        description=item.description,
                        manufacture=item.manufacture or "",
                        job_type=item.job_type or "",
                        quantity=qty,
                    )
                )
            
            SAPProformaInvoiceLine.objects.bulk_create(pi_lines)
            
            # Create log entry for update
            ip = get_client_ip(request)
            network_label = label_network(ip)
            ua_string = request.META.get('HTTP_USER_AGENT', '')[:500]
            device_type, device_os, device_browser = parse_device_info(ua_string)
            
            try:
                lat = request.POST.get("location_lat")
                lng = request.POST.get("location_lng")
                lat_val = float(lat) if lat not in (None, "",) else None
                lng_val = float(lng) if lng not in (None, "",) else None
            except (ValueError, TypeError):
                lat_val = None
                lng_val = None
            
            ProformaInvoiceLog.objects.create(
                pi=pi,
                user=request.user if request.user.is_authenticated else None,
                ip_address=ip,
                user_agent=ua_string,
                device_type=device_type,
                device_os=device_os,
                device_browser=device_browser,
                location_lat=lat_val,
                location_lng=lng_val,
                network_label=network_label,
                device=getattr(request, 'device_obj', None),
                action="updated",
            )
        
        messages.success(request, f"Proforma Invoice {pi_number} updated successfully.")
        return redirect("salesorder_detail", so_number=salesorder.so_number)
    
    # GET: Show edit form
    # Normalize remarks: replace old default with new default if it matches
    old_default = "Thank you for your order. Please find the Proforma Invoice attached."
    old_default2 = "Note: Cheque to be prepared in favor of: \n1) JUNAID SANITARY & ELECTRICAL MAT. TRDG. LLC \nTax Registration Number 100225006400003\n2) PAYMENT : cash/CDC Against Delivery"
    new_default = "Note: Cheque to be prepared in favor of: \n1) JUNAID SANITARY & ELECTRICAL MAT. TRDG. LLC \nTax Registration Number 100225006400003\n2) PAYMENT : CDC Against Delivery\n3)  DELIVERY: Ex-Stock Subject to Receipt of cheque copy against this Proforma Invoice within 4 working days"
    
    if pi.remarks and (pi.remarks.strip() == old_default or pi.remarks.strip() == old_default2):
        # Replace old default with new default for display (not saved yet)
        pi.remarks = new_default
    
    return render(request, 'salesorders/pi_edit.html', {
        'pi': pi,
        'salesorder': salesorder,
        'items_with_data': items_with_data,
    })
