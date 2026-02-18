"""
Purchase Stock Requirement Calculation View.
Tabular report grouped by firm, listing items with stock planning metrics.
"""
from datetime import date, timedelta, datetime
from decimal import Decimal
from io import BytesIO

import pandas as pd

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Value, DecimalField, F
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.http import HttpResponse

from .models import (
    Items,
    SAPARInvoice,
    SAPARInvoiceItem,
    SAPARCreditMemo,
    SAPARCreditMemoItem,
    SAPPurchaseOrderItem,
    SAPSalesorderItem,
)
from .sap_salesorder_views import salesman_scope_q_salesorder, get_business_category, _open_row_status_q
from .sap_purchaseorder_views import _open_row_status_q_po


@login_required
def purchase_stock_requirement(request):
    """
    Purchase Stock Requirement Calculation View.
    Shows items for selected firm(s) with stock planning metrics.
    """
    selected_firms = request.GET.getlist('firm')

    # Get all firms for dropdown
    firms = list(
        Items.objects.exclude(item_firm__isnull=True)
        .exclude(item_firm='')
        .values_list('item_firm', flat=True)
        .distinct()
        .order_by('item_firm')
    )

    items_list, _ = _build_items_data(request, selected_firms)

    context = {
        'firms': firms,
        'selected_firms': selected_firms,
        'items': items_list,
        'total_items': len(items_list),
    }
    return render(request, 'salesorders/purchase_stock_requirement.html', context)


def _build_items_data(request, firms):
    """
    Shared logic to build items data for both view and export.
    firms: list of firm names. Returns (items_list, selected_firms) tuple.
    """
    items_list = []
    if not firms:
        return items_list, []

    # Clean and dedupe
    firm_list = list(dict.fromkeys([f.strip() for f in firms if f and str(f).strip()]))
    if not firm_list:
        return items_list, []

    items_qs = Items.objects.filter(item_firm__in=firm_list)
    item_codes = list(items_qs.values_list('item_code', flat=True))

    if not item_codes:
        return items_list, firm_list

    # Base querysets with salesman scope
    invoice_qs = SAPARInvoice.objects.filter(salesman_scope_q_salesorder(request.user))
    creditmemo_qs = SAPARCreditMemo.objects.filter(salesman_scope_q_salesorder(request.user))

    # Filter by item_codes (firm's items only)
    invoice_items = SAPARInvoiceItem.objects.filter(
        invoice__in=invoice_qs,
        item_code__in=item_codes,
    ).exclude(item_code__isnull=True).exclude(item_code='').select_related('invoice')

    creditmemo_items = SAPARCreditMemoItem.objects.filter(
        credit_memo__in=creditmemo_qs,
        item_code__in=item_codes,
    ).exclude(item_code__isnull=True).exclude(item_code='').select_related('credit_memo')

    # Build sale dicts: sold_2024, project_2025, trading_2025, total_2025, last_6m
    six_months_ago = date.today() - timedelta(days=180)

    def safe_float(x):
        if x is None:
            return 0.0
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    sold_2024 = {}
    project_2025 = {}
    trading_2025 = {}
    total_2025 = {}
    last_6m = {}

    # Sold 2024
    inv_2024 = invoice_items.filter(invoice__posting_date__year=2024)
    for row in inv_2024.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        sold_2024[row['item_code']] = safe_float(row['qty'])
    cm_2024 = creditmemo_items.filter(credit_memo__posting_date__year=2024)
    for row in cm_2024.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        sold_2024[row['item_code']] = sold_2024.get(row['item_code'], 0) - safe_float(row['qty'])

    # Project/Trading salesmen by business category
    all_salesmen = set(
        invoice_qs.exclude(salesman_name__isnull=True).exclude(salesman_name='')
        .values_list('salesman_name', flat=True).distinct()
    )
    project_salesmen = [s for s in all_salesmen if get_business_category(s) == 'Project']
    trading_salesmen = [s for s in all_salesmen if get_business_category(s) == 'Trading']

    # Project 2025 - invoices/CMs where salesman is Project
    inv_project_2025 = invoice_items.filter(
        invoice__posting_date__year=2025,
        invoice__salesman_name__in=project_salesmen,
    )
    for row in inv_project_2025.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        project_2025[row['item_code']] = safe_float(row['qty'])
    cm_project_2025 = creditmemo_items.filter(
        credit_memo__posting_date__year=2025,
        credit_memo__salesman_name__in=project_salesmen,
    )
    for row in cm_project_2025.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        project_2025[row['item_code']] = project_2025.get(row['item_code'], 0) - safe_float(row['qty'])

    # Trading 2025
    inv_trading_2025 = invoice_items.filter(
        invoice__posting_date__year=2025,
        invoice__salesman_name__in=trading_salesmen,
    )
    for row in inv_trading_2025.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        trading_2025[row['item_code']] = safe_float(row['qty'])
    cm_trading_2025 = creditmemo_items.filter(
        credit_memo__posting_date__year=2025,
        credit_memo__salesman_name__in=trading_salesmen,
    )
    for row in cm_trading_2025.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        trading_2025[row['item_code']] = trading_2025.get(row['item_code'], 0) - safe_float(row['qty'])

    # Total 2025 (invoice - credit memo)
    inv_2025 = invoice_items.filter(invoice__posting_date__year=2025)
    for row in inv_2025.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        total_2025[row['item_code']] = safe_float(row['qty'])
    cm_2025 = creditmemo_items.filter(credit_memo__posting_date__year=2025)
    for row in cm_2025.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        total_2025[row['item_code']] = total_2025.get(row['item_code'], 0) - safe_float(row['qty'])

    # Last 6 months sale
    inv_6m = invoice_items.filter(invoice__posting_date__gte=six_months_ago)
    for row in inv_6m.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        last_6m[row['item_code']] = safe_float(row['qty'])
    cm_6m = creditmemo_items.filter(credit_memo__posting_date__gte=six_months_ago)
    for row in cm_6m.values('item_code').annotate(
        qty=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField()))
    ):
        last_6m[row['item_code']] = last_6m.get(row['item_code'], 0) - safe_float(row['qty'])

    # LPO given (open POs) - only last 6 months by posting_date
    open_po_qty = SAPPurchaseOrderItem.objects.filter(
        _open_row_status_q_po(),
        item_no__in=item_codes,
        purchaseorder__posting_date__gte=six_months_ago,
    ).exclude(item_no__isnull=True).exclude(item_no='').values('item_no').annotate(
        total_qty=Sum(Coalesce(F('remaining_open_quantity'), F('quantity'), Value(0, output_field=DecimalField())))
    )
    lpo_dict = {row['item_no']: safe_float(row['total_qty']) for row in open_po_qty}

    # Open SO
    open_so_qty = SAPSalesorderItem.objects.filter(
        _open_row_status_q(),
        item_no__in=item_codes,
    ).exclude(item_no__isnull=True).exclude(item_no='').values('item_no').annotate(
        total_qty=Sum(Coalesce(F('remaining_open_quantity'), F('quantity'), Value(0, output_field=DecimalField())))
    )
    open_so_dict = {row['item_no']: safe_float(row['total_qty']) for row in open_so_qty}

    # Build items_list with all calculated columns
    for item in items_qs:
        code = item.item_code
        dip_stock = safe_float(item.dip_warehouse_stock)
        total_stock = safe_float(item.total_available_stock)
        sold_24 = sold_2024.get(code, 0)
        proj_25 = project_2025.get(code, 0)
        trd_25 = trading_2025.get(code, 0)
        tot_25 = total_2025.get(code, 0)
        lpo_given = lpo_dict.get(code, 0)
        open_so = open_so_dict.get(code, 0)
        last_6m_sale = last_6m.get(code, 0)

        # Avg 3 Month Sales = Total Sold 2025 / 3
        avg_3m = tot_25 / 3.0 if tot_25 else 0.0

        # Stock Sufficiency Month = DIP Stock / (Total Sold 2025/5), rounded to int
        divisor = tot_25 / 5.0 if tot_25 else 0.0
        if divisor > 0:
            stock_suff_month = int(round(dip_stock / divisor))
        else:
            stock_suff_month = 0

        # Stock Reqt Calculation = (Avg 3 month sale + Open SO) - (Total Stock + LPO given)
        reqt_calc = (avg_3m + open_so) - (total_stock + lpo_given)

        # Stock Requirement = reqt_calc if +ve else '-'
        stock_reqt = reqt_calc if reqt_calc > 0 else None

        # Stock reqt in 3 months = (Last 6 month sale / 6) * 3
        stock_reqt_3m = (last_6m_sale / 6.0) * 3.0 if last_6m_sale else 0.0

        # Stock After 3 months = Total Stock - Stock reqt in 3 months
        stock_after_3m = total_stock - stock_reqt_3m

        # Final Stock Reqt (6 month) = stock_reqt_3m if stock_after_3m > 0 else 0
        final_stock_reqt_6m = stock_reqt_3m if stock_after_3m > 0 else 0.0

        desc_with_upc = item.item_description or ''
        if item.item_upvc:
            desc_with_upc = f"{desc_with_upc} ({item.item_upvc})"

        items_list.append({
            'item_code': code,
            'item_description_with_upc': desc_with_upc,
            'dip_stock': dip_stock,
            'total_stock': total_stock,
            'sold_qty_2024': sold_24,
            'project_sold_qty_2025': proj_25,
            'trading_sold_qty_2025': trd_25,
            'total_sold_qty_2025': tot_25,
            'avg_3_month_sales': avg_3m,
            'stock_sufficiency_month': stock_suff_month,
            'lpo_given': lpo_given,
            'open_so': open_so,
            'stock_reqt_calculation': reqt_calc,
            'stock_requirement': stock_reqt,
            'last_6_month_sale': last_6m_sale,
            'stock_reqt_in_3_months': stock_reqt_3m,
            'stock_after_3_months': stock_after_3m,
            'final_stock_reqt_6_month': final_stock_reqt_6m,
        })

    # Sort by Final Stock Reqt (6 month) descending
    # Ensure we're sorting by numeric value (handle any edge cases)
    items_list.sort(key=lambda x: float(x.get('final_stock_reqt_6_month', 0) or 0), reverse=True)

    return items_list, firm_list


@login_required
def export_purchase_stock_requirement_excel(request):
    """
    Export Purchase Stock Requirement to Excel.
    """
    firms = request.GET.getlist('firm')

    if not firms:
        return HttpResponse("Please select at least one firm.", status=400)

    items_list, firm_list = _build_items_data(request, firms)

    if not items_list:
        return HttpResponse("No items found for selected firms.", status=404)

    # Prepare data for Excel - round all numeric values to integers
    def round_int(value):
        """Round value to integer, return '-' if None or non-numeric"""
        if value is None:
            return '-'
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return value if value == '-' else 0

    data = []
    for item in items_list:
        row_data = {
            'Item Code': item['item_code'],
            'Item Description (with UPC Code)': item['item_description_with_upc'],
            'DIP Stock': round_int(item['dip_stock']),
            'Total Stock': round_int(item['total_stock']),
            'Sold Qty 2024': round_int(item['sold_qty_2024']),
            'Project Sold Qty 2025': round_int(item['project_sold_qty_2025']),
            'Trading Sold Qty 2025': round_int(item['trading_sold_qty_2025']),
            'Total Sold Qty 2025': round_int(item['total_sold_qty_2025']),
            'Avg 3 Month Sales': round_int(item['avg_3_month_sales']),
            'Stock Sufficiency Month': round_int(item['stock_sufficiency_month']),
            'LPO Given': round_int(item['lpo_given']),
            'Open SO': round_int(item['open_so']),
            'Stock Requirement Calculation': round_int(item['stock_reqt_calculation']),
            'Stock Requirement': round_int(item['stock_requirement']),
            'Last 6 Month Sale': round_int(item['last_6_month_sale']),
            'Stock Reqt in 3 months': round_int(item['stock_reqt_in_3_months']),
            'Stock After 3 months': round_int(item['stock_after_3_months']),
            'Final Stock Reqt as per 6 month': round_int(item['final_stock_reqt_6_month']),
        }
        data.append(row_data)

    # Create Excel file
    df = pd.DataFrame(data)

    # Create Excel writer
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Stock Requirement', index=False)

        # Get the worksheet
        worksheet = writer.sheets['Stock Requirement']

        # Format header row
        from openpyxl.styles import Font, PatternFill, Alignment
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)

        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # Auto-adjust column widths
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column_letter].width = adjusted_width

        # Set row height for header
        worksheet.row_dimensions[1].height = 30

    # Prepare HTTP response
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    firm_label = firm_list[0] if len(firm_list) == 1 else f"{len(firm_list)}_firms"
    filename = f"Purchase_Stock_Requirement_{firm_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    return response
