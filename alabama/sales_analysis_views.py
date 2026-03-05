"""Alabama Sales Analysis Dashboard - same as so app detailed sales analysis."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.db.models import Sum, Count
from django.db.models.functions import Coalesce
from django.db.models import Value, DecimalField
from django.http import JsonResponse
from datetime import date, datetime
from decimal import Decimal

from .models import AlabamaSalesLine
from .views import alabama_salesman_scope_q


@login_required
def sales_analysis_dashboard(request):
    """
    Sales Analysis Dashboard with Today/Month/Year metrics and Top 5 Customers/Items.
    Uses AlabamaSalesLine (Excel upload data). Same layout as so app sales_analysis_dashboard.
    """
    today = date.today()
    current_year = today.year
    current_month = today.month

    lines_qs = AlabamaSalesLine.objects.all()

    # Apply salesman scope for Alabama
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        lines_qs = lines_qs.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    elif not (request.user.is_superuser or request.user.is_staff or (hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin')):
        try:
            from so.models import Role
            role = Role.objects.get(user=request.user)
            if role.role != 'Admin' and (role.company or 'Junaid') == 'Alabama':
                lines_qs = lines_qs.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
        except (Role.DoesNotExist, AttributeError):
            pass

    is_admin = request.user.is_superuser or request.user.is_staff or (
        (request.user.username or '').strip().lower() == 'manager' or
        (hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin')
    )

    # Filters
    salesmen_filter = request.GET.getlist('salesman')
    month_filter = request.GET.getlist('month')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    period = request.GET.get('period', '').strip()
    year_filter = request.GET.get('year', '').strip() or str(current_year)

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

    def apply_common_filters(qs):
        if salesmen_filter:
            clean = [s for s in salesmen_filter if s.strip()]
            if clean:
                qs = qs.filter(sales_employee__in=clean)
        if month_filter:
            try:
                month_nums = [int(m) for m in month_filter if m.strip()]
                if month_nums:
                    qs = qs.filter(posting_date__month__in=month_nums)
            except (ValueError, TypeError):
                pass
        if start_date:
            qs = qs.filter(posting_date__gte=start_date)
        if end_date:
            qs = qs.filter(posting_date__lte=end_date)
        return qs

    def apply_period_filter(qs, p):
        if p == 'today':
            return qs.filter(posting_date=today)
        if p == 'month':
            return qs.filter(posting_date__year=current_year, posting_date__month=current_month)
        if p == 'year':
            return qs.filter(posting_date__year=current_year)
        if p in ('all', '') and year_filter and year_filter != 'Total':
            try:
                return qs.filter(posting_date__year=int(year_filter))
            except (ValueError, TypeError):
                pass
        return qs

    # Today metrics
    today_lines = lines_qs.filter(posting_date=today)
    today_lines = apply_common_filters(today_lines)
    today_sales = today_lines.aggregate(s=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
    today_gp = today_lines.aggregate(s=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')

    # Month metrics
    month_lines = lines_qs.filter(posting_date__year=current_year, posting_date__month=current_month)
    month_lines = apply_common_filters(month_lines)
    month_sales = month_lines.aggregate(s=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
    month_gp = month_lines.aggregate(s=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')

    # Year metrics
    year_lines = lines_qs.filter(posting_date__year=current_year)
    year_lines = apply_common_filters(year_lines)
    year_sales = year_lines.aggregate(s=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
    year_gp = year_lines.aggregate(s=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')

    # Total (filtered by period/year/date range)
    total_lines = apply_common_filters(lines_qs)
    total_lines = apply_period_filter(total_lines, period)
    total_sales = total_lines.aggregate(s=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
    total_gp = total_lines.aggregate(s=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')

    # Top 5 Customers
    top_customer_lines = apply_common_filters(lines_qs)
    top_customer_lines = apply_period_filter(top_customer_lines, period)

    from django.db.models import Max
    customer_agg = (
        top_customer_lines.values('customer__customer_code', 'customer__customer_name')
        .annotate(
            total_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
            document_count=Count('document_number', distinct=True),
            sales_employee=Max('sales_employee'),
        )
        .order_by('-total_sales')[:5]
    )

    top_customers = []
    for row in customer_agg:
        top_customers.append({
            'customer_code': row.get('customer__customer_code') or '—',
            'customer_name': row.get('customer__customer_name') or 'Unknown',
            'salesman_name': row.get('sales_employee') or '—',
            'total_sales': row['total_sales'] or Decimal('0'),
            'total_gp': row['total_gp'] or Decimal('0'),
            'document_count': row['document_count'] or 0,
        })

    # Top 5 Items
    top_item_lines = apply_common_filters(lines_qs)
    top_item_lines = apply_period_filter(top_item_lines, period)

    item_agg = (
        top_item_lines.values('item__item_code', 'item__item_description')
        .annotate(
            total_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
            total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
        )
        .order_by('-total_sales')[:5]
    )

    top_items = []
    for row in item_agg:
        top_items.append({
            'item_code': row.get('item__item_code') or '—',
            'item_description': row.get('item__item_description') or 'Unknown',
            'total_sales': row['total_sales'] or Decimal('0'),
            'total_gp': row['total_gp'] or Decimal('0'),
            'total_quantity': row['total_quantity'] or Decimal('0'),
        })

    # Salesmen for filter
    salesmen = list(
        lines_qs.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )

    # Years for filter: current and last 2 years
    years = [current_year, current_year - 1, current_year - 2]

    context = {
        'today_sales': today_sales,
        'today_gp': today_gp,
        'month_sales': month_sales,
        'month_gp': month_gp,
        'year_sales': year_sales,
        'year_gp': year_gp,
        'total_sales': total_sales,
        'total_gp': total_gp,
        'top_customers': top_customers,
        'top_items': top_items,
        'salesmen': salesmen,
        'is_admin': is_admin,
        'years': years,
        'filters': {
            'salesmen_filter': salesmen_filter,
            'month': month_filter,
            'start': start,
            'end': end,
            'period': period,
            'year': year_filter,
        },
        'current_year': current_year,
        'current_month': current_month,
        'today': today,
        'active_page': 'sales_analysis',
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({
            'metrics': {
                'today_sales': float(today_sales),
                'today_gp': float(today_gp),
                'month_sales': float(month_sales),
                'month_gp': float(month_gp),
                'year_sales': float(year_sales),
                'year_gp': float(year_gp),
                'total_sales': float(total_sales),
                'total_gp': float(total_gp),
            },
            'top_customers': [
                {
                    'customer_code': c['customer_code'],
                    'customer_name': c['customer_name'],
                    'total_sales': float(c['total_sales']),
                    'total_gp': float(c['total_gp']),
                    'document_count': c['document_count'],
                }
                for c in top_customers
            ],
            'top_items': [
                {
                    'item_code': i['item_code'],
                    'item_description': i['item_description'],
                    'total_sales': float(i['total_sales']),
                    'total_gp': float(i['total_gp']),
                    'total_quantity': float(i['total_quantity']),
                }
                for i in top_items
            ],
        })

    return render(request, 'alabama/sales_analysis_dashboard.html', context)
