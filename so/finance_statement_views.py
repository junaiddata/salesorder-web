"""
Finance Statement Views - Customer Finance Summary
"""
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Sum, Value, FloatField, Max
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseForbidden
from decimal import Decimal
from datetime import datetime, timedelta
from calendar import monthrange
import pandas as pd
from io import BytesIO
from so.models import Customer, Salesman, FinanceCreditEditLog


@login_required
def finance_statement_list(request):
    """
    Finance Statement List - Shows all customers with finance details
    Filters out customers where both BalanceDue and ChecksBal are 0
    """
    # Get filter parameters
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    show_detail_columns = request.GET.get('detail', '').strip().lower() in ('1', 'true', 'yes', 'on')
    salesmen_filter = [s.strip() for s in salesmen_filter if s and s.strip()]
    store_filter = request.GET.get('store', '').strip()  # HO or Others
    sort_by = request.GET.get('sort', 'total_outstanding')  # Default sort by highest balance
    sort_order = request.GET.get('order', 'desc')  # Descending order (highest first)
    
    # Base queryset - only customers with finance data (non-zero balance or PDC)
    # This excludes customers where BOTH balance and PDC are 0
    customers = Customer.objects.filter(
        Q(total_outstanding__gt=0) | Q(pdc_received__gt=0)
    ).select_related('salesman')
    
    # Apply search filter
    if search_query:
        customers = customers.filter(
            Q(customer_code__icontains=search_query) |
            Q(customer_name__icontains=search_query)
        )
    
    # Apply salesman filter
    if salesmen_filter:
        customers = customers.filter(salesman__id__in=salesmen_filter)
    
    # Apply store filter (HO or Others)
    if store_filter == 'HO':
        customers = customers.filter(customer_code__startswith='HO')
    elif store_filter == 'Others':
        customers = customers.exclude(customer_code__startswith='HO')
    
    # Apply sorting
    if sort_order == 'desc':
        sort_by = f'-{sort_by}'
    customers = customers.order_by(sort_by, 'customer_name')
    
    # Get total count before pagination
    total_count_before_pagination = customers.count()
    
    # Pagination
    paginator = Paginator(customers, 250)  # 100 per page for better visibility
    page_number = request.GET.get('page', 1)
    try:
        page_number = int(page_number)
        if page_number < 1:
            page_number = 1
    except (ValueError, TypeError):
        page_number = 1
    
    try:
        page_obj = paginator.get_page(page_number)
    except:
        page_obj = paginator.get_page(1)
    
    # Get all salesmen for filter dropdown
    salesmen = Salesman.objects.all().order_by('salesman_name')
    
    # Calculate totals
    totals = customers.aggregate(
        total_outstanding=Coalesce(Sum('total_outstanding'), Value(0.0, output_field=FloatField())),
        total_pdc=Coalesce(Sum('pdc_received'), Value(0.0, output_field=FloatField())),
        total_with_pdc=Coalesce(Sum('total_outstanding_with_pdc'), Value(0.0, output_field=FloatField())),
        total_month_1=Coalesce(Sum('month_pending_1'), Value(0.0, output_field=FloatField())),
        total_month_2=Coalesce(Sum('month_pending_2'), Value(0.0, output_field=FloatField())),
        total_month_3=Coalesce(Sum('month_pending_3'), Value(0.0, output_field=FloatField())),
        total_month_4=Coalesce(Sum('month_pending_4'), Value(0.0, output_field=FloatField())),
        total_month_5=Coalesce(Sum('month_pending_5'), Value(0.0, output_field=FloatField())),
        total_month_6=Coalesce(Sum('month_pending_6'), Value(0.0, output_field=FloatField())),
        total_old_months=Coalesce(Sum('old_months_pending'), Value(0.0, output_field=FloatField())),
        total_very_old_months=Coalesce(Sum('very_old_months_pending'), Value(0.0, output_field=FloatField())),
    )
    
    # Prepare monthly labels (Month 6 = current, going back to Month 1)
    today = datetime.now().date()
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    monthly_labels = []
    for i in range(6):
        months_ago = 5 - i  # Month 1 = 5 months ago, Month 6 = 0 months ago (current)
        month_date = today - timedelta(days=30 * months_ago)
        monthly_labels.append({
            'label': month_names[month_date.month - 1],  # Show month name like "Feb", "Jan"
            'full_label': f"{month_names[month_date.month - 1]} {month_date.year}",
            'field': f'month_pending_{i+1}'
        })
    
    context = {
        'customers': page_obj,  # Pass page_obj for pagination
        'page_obj': page_obj,  # Also pass as page_obj for template
        'salesmen': salesmen,
        'is_manager': request.user.username == 'manager',
        'show_detail_columns': show_detail_columns,
        'filters': {
            'q': search_query,
            'salesmen_filter': salesmen_filter,
            'store': store_filter,
            'detail': '1' if show_detail_columns else '',
        },
        'sort': sort_by.replace('-', ''),
        'order': sort_order,
        'totals': totals,
        'monthly_labels': monthly_labels,
        'total_count': total_count_before_pagination,
    }
    
    return render(request, 'finance_statement/finance_statement_list.html', context)


@login_required
def finance_statement_detail(request, customer_id):
    """
    Finance Statement Detail - Shows detailed finance breakdown for a customer
    """
    customer = get_object_or_404(Customer.objects.select_related('salesman'), id=customer_id)
    
    # Prepare monthly pending data (Month 6 = current, going back to Month 1)
    today = datetime.now().date()
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    
    monthly_data = []
    month_amounts = [
        customer.month_pending_1,  # Month 1 = 5 months ago
        customer.month_pending_2,  # Month 2 = 4 months ago
        customer.month_pending_3,  # Month 3 = 3 months ago
        customer.month_pending_4,  # Month 4 = 2 months ago
        customer.month_pending_5,  # Month 5 = 1 month ago
        customer.month_pending_6,  # Month 6 = current month
    ]
    
    # Generate month labels with date ranges (Month 1 = oldest, Month 6 = current)
    from calendar import monthrange
    for i in range(6):
        months_ago = 5 - i  # Month 1 = 5 months ago, Month 6 = 0 months ago (current)
        # Calculate the actual month date (first day of that month)
        if months_ago == 0:
            month_start = today.replace(day=1)
        else:
            # Go back months_ago months
            year = today.year
            month = today.month - months_ago
            while month <= 0:
                month += 12
                year -= 1
            month_start = datetime(year, month, 1).date()
        
        # Get last day of that month
        last_day = monthrange(month_start.year, month_start.month)[1]
        month_end = datetime(month_start.year, month_start.month, last_day).date()
        
        # Format dates as DD-MM-YY
        start_str = month_start.strftime('%d-%m-%y')
        end_str = month_end.strftime('%d-%m-%y')
        month_name = month_names[month_start.month - 1]
        
        monthly_data.append({
            'month': f"{month_name} {month_start.year}",
            'month_label': f"Month {i+1}",
            'month_name': month_name,
            'date_range': f"({start_str} to {end_str})",
            'start_date': month_start,
            'end_date': month_end,
            'amount': month_amounts[i],
            'field': f'month_pending_{i+1}'
        })
    
    # Calculate 6+ months end date (end of month before Month 1)
    if monthly_data:
        month_1_start = monthly_data[0]['start_date']
        # Go back one month from Month 1 start
        year = month_1_start.year
        month = month_1_start.month - 1
        if month <= 0:
            month = 12
            year -= 1
        last_day_6plus = monthrange(year, month)[1]
        six_plus_end = datetime(year, month, last_day_6plus).date()
        six_plus_end_str = six_plus_end.strftime('%d-%m-%y')
    else:
        six_plus_end_str = ""
    
    # Calculate totals
    total_monthly = sum(m['amount'] for m in monthly_data)
    total_outstanding = customer.total_outstanding or 0
    pdc_received = customer.pdc_received or 0
    total_with_pdc = customer.total_outstanding_with_pdc or 0
    old_months_pending = customer.old_months_pending or 0  # 180+ days (6+)
    very_old_months_pending = getattr(customer, 'very_old_months_pending', 0) or 0  # 360+ days (6++)
    
    # Calculate 90+ and 120+ aging buckets
    # 90+ = before last 3 months (so if current is Feb, last 3 months are Feb, Jan, Dec, so 90+ = till Nov 30)
    # But user says: if current is Feb, 90+ should be till Oct 31 (so before last 4 months)
    # Actually: 90+ = months 1, 2, 3, 4 (oldest 4 months, excluding current and last month)
    # 120+ = months 1, 2, 3 (oldest 3 months, excluding current, last month, and 2 months ago)
    
    # If current is Feb (month 6), last month is Jan (month 5)
    # 90+ should be till Oct 31 = end of month that is 3 months before current
    # Current = Feb, 3 months before = Nov, but they want Oct 31, so 4 months before current
    # Let me recalculate: if current is Feb, 90+ till Oct 31 means before Nov 1
    # So 90+ = everything before Nov = months before Nov = months 1, 2, 3, 4 (Sep, Oct, Nov, Dec? No)
    
    # Re-reading: "90+ means current month should be from last month"
    # If current is Feb, last month is Jan
    # 90+ till Oct 31 means: everything before Nov 1
    # So 90+ = months that are Nov and older = months 1, 2, 3, 4? No wait
    
    # Let me think: Month 6 = Feb (current), Month 5 = Jan, Month 4 = Dec, Month 3 = Nov, Month 2 = Oct, Month 1 = Sep
    # If 90+ should be till Oct 31, that means everything before Nov 1
    # So 90+ = Nov, Dec, Jan, Feb? No that doesn't make sense
    
    # Actually: 90+ = months 1, 2, 3, 4 (Sep, Oct, Nov, Dec) = till Oct 31? No
    
    # Let me recalculate based on "before last 3 months":
    # If current is Feb, last 3 months are: Feb, Jan, Dec
    # So 90+ = before Dec 1 = till Nov 30
    # But user wants till Oct 31, which is before Nov 1
    
    # So: 90+ = before last 4 months
    # If current is Feb, last 4 months are: Feb, Jan, Dec, Nov
    # So 90+ = before Nov 1 = till Oct 31 ✓
    
    # 90+ = months 1, 2 (Sep, Oct) = till Oct 31 ✓
    # 120+ = months 1 (Sep) = till Sep 30? Or till Oct 31?
    
    # Based on user's example: 90+ till Oct 31 when current is Feb
    # Month 6 = Feb, Month 5 = Jan, Month 4 = Dec, Month 3 = Nov, Month 2 = Oct, Month 1 = Sep
    # 90+ till Oct 31 = months 1, 2 (Sep, Oct) ✓
    # 120+ till Sep 30 = month 1 (Sep) ✓
    
    pending_90_plus = sum(month_amounts[i] for i in [0, 1])  # months 1, 2 (before last 4 months)
    pending_120_plus = month_amounts[0]  # month 1 only (before last 5 months)
    
    # Calculate end dates for 90+ and 120+
    if monthly_data:
        # 90+ ends at end of month 2 (Oct in the example)
        month_2_end = monthly_data[1]['end_date']
        end_90_str = month_2_end.strftime('%d-%m-%y')
        
        # 120+ ends at end of month 1 (Sep in the example)
        month_1_end = monthly_data[0]['end_date']
        end_120_str = month_1_end.strftime('%d-%m-%y')
    else:
        end_90_str = ""
        end_120_str = ""
    
    # Credit limit check
    has_over_limit = total_with_pdc > customer.credit_limit if customer.credit_limit > 0 else False
    credit_utilization = (total_with_pdc / customer.credit_limit * 100) if customer.credit_limit > 0 else 0
    
    is_manager = request.user.username == 'manager'
    latest_credit_edit = (
        FinanceCreditEditLog.objects
        .filter(customer=customer)
        .select_related('edited_by')
        .order_by('-created_at')
        .first()
    )

    context = {
        'customer': customer,
        'monthly_data': monthly_data,
        'total_monthly': total_monthly,
        'total_outstanding': total_outstanding,
        'pdc_received': pdc_received,
        'total_with_pdc': total_with_pdc,
        'old_months_pending': old_months_pending,  # 180+ days (6+)
        'very_old_months_pending': very_old_months_pending,  # 360+ days (6++)
        'six_plus_end_str': six_plus_end_str,
        'pending_90_plus': pending_90_plus,
        'pending_120_plus': pending_120_plus,
        'end_90_str': end_90_str,
        'end_120_str': end_120_str,
        'credit_limit': customer.credit_limit or 0,
        'credit_days': customer.credit_days or '0',
        'has_over_limit': has_over_limit,
        'credit_utilization': credit_utilization,
        'is_manager': is_manager,
        'latest_credit_edit': latest_credit_edit,
    }
    
    return render(request, 'finance_statement/finance_statement_detail.html', context)


@login_required
def save_finance_credit_edit(request, customer_id):
    """
    Save manager-edited credit limit and payment terms to log table.
    """
    if request.user.username != 'manager':
        return HttpResponseForbidden("Only manager can submit credit edits.")

    if request.method != 'POST':
        return HttpResponseForbidden("POST method required.")

    customer = get_object_or_404(Customer, id=customer_id)

    credit_limit_raw = request.POST.get('edited_credit_limit', '').strip()
    credit_days = request.POST.get('edited_credit_days', '').strip()
    remarks = request.POST.get('remarks', '').strip()

    try:
        edited_credit_limit = float(credit_limit_raw)
    except (TypeError, ValueError):
        messages.error(request, "Please enter a valid credit limit.")
        return redirect('finance_statement_detail', customer_id=customer_id)

    if edited_credit_limit < 0:
        messages.error(request, "Credit limit cannot be negative.")
        return redirect('finance_statement_detail', customer_id=customer_id)

    if not credit_days:
        messages.error(request, "Payment terms cannot be empty.")
        return redirect('finance_statement_detail', customer_id=customer_id)

    FinanceCreditEditLog.objects.create(
        customer=customer,
        edited_credit_limit=edited_credit_limit,
        edited_credit_days=credit_days,
        edited_by=request.user,
        remarks=remarks or None,
    )
    messages.success(request, "Credit edit saved to consolidated list.")
    return redirect('finance_statement_detail', customer_id=customer_id)


@login_required
def save_finance_internal_remarks(request, customer_id):
    """
    Save manager internal remarks for this customer (for salesman).
    Manager only.
    """
    if request.user.username != 'manager':
        return HttpResponseForbidden("Only manager can save internal remarks.")
    if request.method != 'POST':
        return HttpResponseForbidden("POST method required.")
    customer = get_object_or_404(Customer, id=customer_id)
    customer.internal_remarks = (request.POST.get('internal_remarks') or '').strip() or None
    customer.save(update_fields=['internal_remarks'])
    messages.success(request, "Internal remarks saved.")
    return redirect('finance_statement_detail', customer_id=customer_id)


@login_required
def finance_credit_edit_list(request):
    """
    Consolidated manager credit edit list with date range filter.
    """
    if request.user.username != 'manager':
        return HttpResponseForbidden("Only manager can view credit edit list.")

    today = datetime.now().date()
    from_date_str = request.GET.get('from_date', today.strftime('%Y-%m-%d'))
    to_date_str = request.GET.get('to_date', today.strftime('%Y-%m-%d'))

    try:
        from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
    except ValueError:
        from_date = today
        from_date_str = today.strftime('%Y-%m-%d')

    try:
        to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
    except ValueError:
        to_date = today
        to_date_str = today.strftime('%Y-%m-%d')

    if from_date > to_date:
        from_date, to_date = to_date, from_date
        from_date_str, to_date_str = to_date_str, from_date_str

    filtered_edits = FinanceCreditEditLog.objects.filter(
        created_at__date__gte=from_date,
        created_at__date__lte=to_date
    )
    latest_edit_ids = (
        filtered_edits
        .values('customer_id')
        .annotate(latest_id=Max('id'))
        .values_list('latest_id', flat=True)
    )
    edits = (
        FinanceCreditEditLog.objects
        .filter(id__in=latest_edit_ids)
        .select_related('customer__salesman', 'edited_by')
        .order_by('-created_at')
    )

    context = {
        'edits': edits,
        'from_date': from_date_str,
        'to_date': to_date_str,
        'total_edits': edits.count(),
    }
    return render(request, 'finance_statement/finance_credit_edit_list.html', context)


@login_required
def export_finance_statement_list_excel(request):
    """
    Export Finance Statement List to Excel.
    If detail=1 in GET, includes 6 months + 6+ + 6++ columns; otherwise main columns only.
    """
    # Get filter parameters (same as list view)
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    salesmen_filter = [s.strip() for s in salesmen_filter if s and s.strip()]
    store_filter = request.GET.get('store', '').strip()
    include_detail = request.GET.get('detail', '').strip().lower() in ('1', 'true', 'yes', 'on')  # HO or Others
    
    # Base queryset - only customers with finance data (non-zero balance or PDC)
    customers = Customer.objects.filter(
        Q(total_outstanding__gt=0) | Q(pdc_received__gt=0)
    ).select_related('salesman')
    
    # Apply search filter
    if search_query:
        customers = customers.filter(
            Q(customer_code__icontains=search_query) |
            Q(customer_name__icontains=search_query)
        )
    
    # Apply salesman filter
    if salesmen_filter:
        customers = customers.filter(salesman__id__in=salesmen_filter)
    
    # Apply store filter (HO or Others)
    if store_filter == 'HO':
        customers = customers.filter(customer_code__startswith='HO')
    elif store_filter == 'Others':
        customers = customers.exclude(customer_code__startswith='HO')
    
    customers = customers.order_by('customer_name')
    
    # Prepare monthly labels
    today = datetime.now().date()
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    monthly_labels = []
    for i in range(6):
        months_ago = 5 - i  # Month 1 = 5 months ago, Month 6 = 0 months ago (current)
        month_date = today - timedelta(days=30 * months_ago)
        monthly_labels.append({
            'label': f"{month_names[month_date.month - 1]} {month_date.year}",
            'field': f'month_pending_{i+1}'
        })
    
    # Prepare data for Excel
    data = []
    for customer in customers:
        row_data = {
            'Customer Code': customer.customer_code,
            'Customer Name': customer.customer_name,
            'Salesman': customer.salesman.salesman_name if customer.salesman else '',
        }
        if include_detail:
            row_data[monthly_labels[0]['label']] = float(customer.month_pending_1 or 0)
            row_data[monthly_labels[1]['label']] = float(customer.month_pending_2 or 0)
            row_data[monthly_labels[2]['label']] = float(customer.month_pending_3 or 0)
            row_data[monthly_labels[3]['label']] = float(customer.month_pending_4 or 0)
            row_data[monthly_labels[4]['label']] = float(customer.month_pending_5 or 0)
            row_data[monthly_labels[5]['label']] = float(customer.month_pending_6 or 0)
            row_data['6+ (180+ Days)'] = float(customer.old_months_pending or 0)
            row_data['6++ (360+ Days)'] = float(getattr(customer, 'very_old_months_pending', 0) or 0)
        row_data['Balance Due'] = float(customer.total_outstanding or 0)
        row_data['PDC in Hand'] = float(customer.pdc_received or 0)
        row_data['Total with PDC'] = float(customer.total_outstanding_with_pdc or 0)
        row_data['Credit Limit'] = float(customer.credit_limit or 0)
        row_data['Payment Terms'] = customer.credit_days or ''
        data.append(row_data)
    
    # Create Excel file
    df = pd.DataFrame(data)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Finance Statement', index=False)
        
        # Get the workbook and worksheet
        workbook = writer.book
        worksheet = writer.sheets['Finance Statement']
        
        # Auto-adjust column widths
        for idx, col in enumerate(df.columns, 1):
            max_length = max(
                df[col].astype(str).map(len).max(),
                len(str(col))
            )
            worksheet.column_dimensions[chr(64 + idx)].width = min(max_length + 2, 50)
    
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="finance_statement_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    return response


@login_required
def export_finance_statement_detail_excel(request, customer_id):
    """
    Export Finance Statement Detail to Excel
    """
    customer = get_object_or_404(Customer.objects.select_related('salesman'), id=customer_id)
    
    # Prepare monthly pending data
    today = datetime.now().date()
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    
    monthly_data = []
    month_amounts = [
        customer.month_pending_1,
        customer.month_pending_2,
        customer.month_pending_3,
        customer.month_pending_4,
        customer.month_pending_5,
        customer.month_pending_6,
    ]
    
    for i in range(6):
        months_ago = 5 - i
        month_date = today - timedelta(days=30 * months_ago)
        monthly_data.append({
            'Month': f"{month_names[month_date.month - 1]} {month_date.year}",
            'Amount': float(month_amounts[i] or 0)
        })
    
    old_months_pending = customer.old_months_pending or 0
    very_old_months_pending = getattr(customer, 'very_old_months_pending', 0) or 0
    
    # Prepare data for Excel
    data = []
    
    # Customer Information
    data.append({'Field': 'Customer Code', 'Value': customer.customer_code})
    data.append({'Field': 'Customer Name', 'Value': customer.customer_name})
    data.append({'Field': 'Salesman', 'Value': customer.salesman.salesman_name if customer.salesman else ''})
    data.append({'Field': 'Credit Limit', 'Value': float(customer.credit_limit or 0)})
    data.append({'Field': 'Payment Terms', 'Value': customer.credit_days or ''})
    data.append({'Field': '', 'Value': ''})  # Empty row
    
    # Monthly Breakdown
    data.append({'Field': 'Monthly Pending Breakdown', 'Value': ''})
    for month in monthly_data:
        data.append({'Field': month['Month'], 'Value': month['Amount']})
    data.append({'Field': 'Subtotal (6 Months)', 'Value': sum(m['Amount'] for m in monthly_data)})
    data.append({'Field': '', 'Value': ''})  # Empty row
    
    # Aged Pending
    data.append({'Field': '180+ Days Pending (6+ months)', 'Value': float(old_months_pending)})
    data.append({'Field': '360+ Days Pending (6++ months)', 'Value': float(very_old_months_pending)})
    data.append({'Field': '', 'Value': ''})  # Empty row
    
    # Summary
    data.append({'Field': 'Balance Due', 'Value': float(customer.total_outstanding or 0)})
    data.append({'Field': 'PDC Received', 'Value': float(customer.pdc_received or 0)})
    data.append({'Field': 'Total Outstanding (with PDC)', 'Value': float(customer.total_outstanding_with_pdc or 0)})
    
    # Create Excel file
    df = pd.DataFrame(data)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Finance Statement', index=False)
        
        workbook = writer.book
        worksheet = writer.sheets['Finance Statement']
        
        # Auto-adjust column widths
        worksheet.column_dimensions['A'].width = 35
        worksheet.column_dimensions['B'].width = 20
    
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="finance_statement_{customer.customer_code}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    return response
