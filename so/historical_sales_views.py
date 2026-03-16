"""
Historical Sales Views (2020-2023)
Upload, Sales Analysis, Item Analysis, Customer Analysis for uploaded historical data.
"""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Q, Value, DecimalField
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.http import JsonResponse
from datetime import datetime, date
from decimal import Decimal
import pandas as pd
import io
import logging

from .models import HistoricalSalesLine, Customer, Items

logger = logging.getLogger(__name__)

ALLOWED_YEARS = [2020, 2021, 2022, 2023]


def _parse_date(val):
    """Parse date from various formats."""
    if pd.isna(val):
        return None
    if hasattr(val, 'date'):
        return val.date()
    if isinstance(val, (int, float)):
        try:
            return pd.Timestamp(val).date()
        except Exception:
            pass
    s = str(val).strip()
    for fmt in ['%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%d-%m-%y', '%Y%m%d']:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_decimal(x):
    if pd.isna(x):
        return Decimal('0')
    try:
        return Decimal(str(x).replace(',', '').strip())
    except Exception:
        return Decimal('0')


def _to_str(x):
    if pd.isna(x):
        return ''
    s = str(x).strip()
    if s.endswith('.0') and s.replace('.', '').replace('-', '').isdigit():
        s = s[:-2]
    return s


def _resolve_document_type(doc_type_code_raw, doc_type_raw):
    """Resolve document type from DocumentTypeCode or Document Type.
    13 = Invoice; 14, 96010 = Credit Memo.
    """
    code_str = _to_str(doc_type_code_raw)
    if code_str:
        try:
            code = int(float(code_str))
            if code == 13:
                return 'Invoice', code_str
            if code in (14, 96010):
                return 'Credit Memo', code_str
        except (ValueError, TypeError):
            pass
    type_str = _to_str(doc_type_raw)
    if type_str and 'credit' in type_str.lower():
        return 'Credit Memo', code_str or None
    return 'Invoice', code_str or None


@login_required
def historical_sales_upload(request):
    """Upload Excel/CSV file for Historical Sales (2020-2023)."""
    if request.method != 'POST':
        return render(request, 'historical_sales/upload.html', {})

    excel_file = request.FILES.get('excel_file')
    if not excel_file:
        messages.error(request, 'Please upload an Excel or CSV file.')
        return render(request, 'historical_sales/upload.html', {})

    def _col_map(df):
        col_map = {}
        aliases = {
            'document_type_code': ['documenttypecode', 'document type code', 'doc type code'],
            'document_type': ['document type', 'documenttype', 'doc type'],
            'document_number': ['document number', 'documentnumber', 'doc no'],
            'postingdate': ['postingdate', 'posting date'],
            'customer_code': ['customer code', 'customercode'],
            'customer_name': ['customer name', 'customername'],
            'sales_employee': ['sales employee', 'salesemployee', 'salesman'],
            'itemcode': ['itemcode', 'item code'],
            'item_description': ['item description', 'itemdescription'],
            'item_manufacturer': ['item manufacturer', 'itemmanufacturer', 'manufacturer'],
            'quantity': ['quantity', 'qty'],
            'net_sales': ['net sales', 'netsales'],
            'gross_profit': ['gross profit', 'grossprofit', 'gp'],
        }
        for col in df.columns:
            c = str(col).strip().lower().replace('\ufeff', '').replace('\xa0', ' ')
            for canonical, alis in aliases.items():
                if c == canonical or c in alis:
                    col_map[col] = canonical
                    break
        return col_map

    try:
        fname = (excel_file.name or '').lower()
        if fname.endswith('.csv'):
            df = pd.read_csv(excel_file, encoding='utf-8-sig', on_bad_lines='skip')
        else:
            df = pd.read_excel(excel_file)
        df.columns = [str(c).strip().replace('\ufeff', '').replace('\xa0', ' ') for c in df.columns]
        col_map = _col_map(df)
        required = ['document_type', 'document_number', 'postingdate', 'customer_code', 'customer_name',
                    'sales_employee', 'itemcode', 'item_description', 'item_manufacturer',
                    'quantity', 'net_sales', 'gross_profit']
        missing = [r for r in required if r not in col_map.values()]
        if missing:
            return render(request, 'historical_sales/upload.html', {
                'error': f'Missing columns: {", ".join(missing)}. Expected: Document Type, Document Number, PostingDate, Customer Code, Customer Name, Sales Employee, ItemCode, Item Description, Item Manufacturer, Quantity, Net Sales, Gross Profit',
            })

        rev_map = {v: k for k, v in col_map.items() if v in required}
        doc_type_code_col = next((k for k, v in col_map.items() if v == 'document_type_code'), None)
        if doc_type_code_col:
            rev_map['document_type_code'] = doc_type_code_col

        def get_val(row, key):
            col = rev_map.get(key)
            if col is None:
                return ''
            return row.get(col, '')

        docs_to_replace = set()
        rows_to_insert = []
        skipped_outside_year = 0

        for idx, row in df.iterrows():
            doc_type_code_raw = get_val(row, 'document_type_code') if 'document_type_code' in rev_map else ''
            doc_type_raw = get_val(row, 'document_type')
            doc_type, doc_type_code = _resolve_document_type(doc_type_code_raw, doc_type_raw)
            doc_no = _to_str(get_val(row, 'document_number'))
            if not doc_no:
                continue
            posting_d = _parse_date(get_val(row, 'postingdate'))
            if not posting_d:
                continue
            if posting_d.year not in ALLOWED_YEARS:
                skipped_outside_year += 1
                continue
            cust_code = _to_str(get_val(row, 'customer_code'))
            cust_name = _to_str(get_val(row, 'customer_name'))
            if not cust_code or not cust_name:
                continue
            sales_emp = _to_str(get_val(row, 'sales_employee')) or None
            item_code = _to_str(get_val(row, 'itemcode'))
            item_desc = _to_str(get_val(row, 'item_description'))
            item_manu = _to_str(get_val(row, 'item_manufacturer'))
            if not item_code:
                continue

            qty = _to_decimal(get_val(row, 'quantity'))
            net_sales = _to_decimal(get_val(row, 'net_sales'))
            gp = _to_decimal(get_val(row, 'gross_profit'))

            docs_to_replace.add((doc_type, doc_no))

            customer, _ = Customer.objects.get_or_create(
                customer_code=cust_code,
                defaults={'customer_name': cust_name}
            )
            item, _ = Items.objects.get_or_create(
                item_code=item_code,
                defaults={
                    'item_description': item_desc or item_code,
                    'item_firm': item_manu or '',
                }
            )

            rows_to_insert.append({
                'document_type': doc_type,
                'document_type_code': doc_type_code,
                'document_number': doc_no,
                'posting_date': posting_d,
                'customer': customer,
                'customer_code': cust_code,
                'customer_name': cust_name,
                'sales_employee': sales_emp,
                'item': item,
                'item_code': item_code,
                'item_description': item_desc,
                'item_manufacturer': item_manu,
                'quantity': qty,
                'net_sales': net_sales,
                'gross_profit': gp,
            })

        if not rows_to_insert:
            msg = f'No valid rows found. Check: (1) Dates must be in 2020-2023, (2) Document Number, Customer Code, Customer Name, Item Code must not be empty. Found {len(df)} rows in file.'
            if skipped_outside_year:
                msg += f' Skipped {skipped_outside_year} rows outside 2020-2023.'
            return render(request, 'historical_sales/upload.html', {'error': msg})

        with transaction.atomic():
            for doc_type, doc_no in docs_to_replace:
                HistoricalSalesLine.objects.filter(
                    document_type=doc_type, document_number=doc_no
                ).delete()
                HistoricalSalesLine.objects.bulk_create([
                    HistoricalSalesLine(**r) for r in rows_to_insert
                ])

        msg = f'Successfully uploaded {len(rows_to_insert)} line items from {len(docs_to_replace)} documents.'
        if skipped_outside_year:
            msg += f' Skipped {skipped_outside_year} rows outside 2020-2023.'
        messages.success(request, msg)
        return redirect('historical_sales_analysis')

    except Exception as e:
        logger.exception('Historical sales upload failed')
        messages.error(request, f'Upload failed: {str(e)}')
        return render(request, 'historical_sales/upload.html', {'error': str(e)})

    return render(request, 'historical_sales/upload.html', {})


@login_required
def historical_sales_analysis_dashboard(request):
    """Sales Analysis Dashboard for 2020-2023 historical data."""
    today = date.today()
    lines_qs = HistoricalSalesLine.objects.all()

    is_admin = request.user.is_superuser or request.user.is_staff or (
        hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin'
    )

    salesmen_filter = request.GET.getlist('salesman')
    month_filter = request.GET.getlist('month')
    store_filter = request.GET.get('store', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    period = request.GET.get('period', '').strip()
    year_filter = request.GET.get('year', 'Total').strip()

    def _store_from_salesman(sales_employee):
        """Derive store from sales_employee: R. or E. prefix = Others, else HO."""
        if not sales_employee or not str(sales_employee).strip():
            return 'HO'
        s = str(sales_employee).strip()
        return 'Others' if (s.startswith('R.') or s.startswith('E.')) else 'HO'

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
        if store_filter and store_filter != 'Total':
            if store_filter == 'HO':
                qs = qs.exclude(sales_employee__istartswith='R.').exclude(sales_employee__istartswith='E.')
            elif store_filter == 'Others':
                qs = qs.filter(Q(sales_employee__istartswith='R.') | Q(sales_employee__istartswith='E.'))
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
            return qs.filter(posting_date__year=today.year, posting_date__month=today.month)
        if p == 'year':
            return qs.filter(posting_date__year=today.year)
        if p in ('all', '') and year_filter and year_filter != 'Total':
            try:
                return qs.filter(posting_date__year=int(year_filter))
            except (ValueError, TypeError):
                pass
        return qs

    # Total metrics (filtered)
    total_lines = apply_common_filters(lines_qs)
    total_lines = apply_period_filter(total_lines, period)
    total_sales = total_lines.aggregate(s=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
    total_gp = total_lines.aggregate(s=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')

    # Top 5 Customers
    from django.db.models import Max
    top_customer_lines = apply_common_filters(lines_qs)
    top_customer_lines = apply_period_filter(top_customer_lines, period)
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
    top_customers = [
        {
            'customer_code': r.get('customer__customer_code') or '—',
            'customer_name': r.get('customer__customer_name') or 'Unknown',
            'salesman_name': r.get('sales_employee') or '—',
            'total_sales': r['total_sales'] or Decimal('0'),
            'total_gp': r['total_gp'] or Decimal('0'),
            'document_count': r['document_count'] or 0,
        }
        for r in customer_agg
    ]

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
    top_items = [
        {
            'item_code': r.get('item__item_code') or '—',
            'item_description': r.get('item__item_description') or 'Unknown',
            'total_sales': r['total_sales'] or Decimal('0'),
            'total_gp': r['total_gp'] or Decimal('0'),
            'total_quantity': r['total_quantity'] or Decimal('0'),
        }
        for r in item_agg
    ]

    salesmen = list(
        lines_qs.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )

    context = {
        'total_sales': total_sales,
        'total_gp': total_gp,
        'top_customers': top_customers,
        'top_items': top_items,
        'salesmen': salesmen,
        'is_admin': is_admin,
        'years': ALLOWED_YEARS,
        'filters': {
            'salesmen_filter': salesmen_filter,
            'month': month_filter,
            'store': store_filter,
            'start': start,
            'end': end,
            'period': period,
            'year': year_filter,
        },
        'today': today,
    }
    return render(request, 'historical_sales/sales_analysis_dashboard.html', context)


def _is_null_item_code(code):
    if not code or not str(code).strip():
        return True
    c = str(code).strip().upper()
    return c in ('NULL', '-NULL-', 'NULL-', '-NULL')


@login_required
def historical_item_analysis(request):
    """Item Analysis for 2020-2023 historical data."""
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    month_filter = request.GET.getlist('month')
    start_str = request.GET.get('start', '').strip()
    end_str = request.GET.get('end', '').strip()

    def parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    qs = HistoricalSalesLine.objects.all().select_related('item')
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
    start_date = parse_date(start_str)
    end_date = parse_date(end_str)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    is_admin = request.user.is_superuser or request.user.is_staff or (
        hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin'
    )

    salesmen = list(
        HistoricalSalesLine.objects.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )
    firms = list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='').values_list('item_firm', flat=True).distinct().order_by('item_firm'))

    item_data = {}
    for year in ALLOWED_YEARS:
        year_qs = qs.filter(posting_date__year=year)
        agg = year_qs.values('item').annotate(
            total_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            total_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
            total_quantity=Coalesce(Sum('quantity'), Value(0, output_field=DecimalField())),
        )
        for row in agg:
            item_id = row['item']
            if not item_id:
                continue
            try:
                item = Items.objects.get(pk=item_id)
            except Items.DoesNotExist:
                continue
            code = item.item_code or ''
            if not code or _is_null_item_code(code):
                continue
            if firm_filter:
                clean_firms = [f for f in firm_filter if f.strip()]
                if clean_firms and (item.item_firm or '') not in clean_firms:
                    continue
            if search_query:
                sq = search_query.lower()
                if sq not in (item.item_code or '').lower() and sq not in (item.item_description or '').lower():
                    continue
            key = code
            if key not in item_data:
                item_data[key] = {
                    'item_code': code,
                    'item_description': item.item_description or 'Unknown',
                    'years': {},
                }
            if year not in item_data[key]['years']:
                item_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'total_quantity': Decimal('0'),
                }
            item_data[key]['years'][year]['total_sales'] += row['total_sales'] or Decimal('0')
            item_data[key]['years'][year]['total_gp'] += row['total_gp'] or Decimal('0')
            item_data[key]['years'][year]['total_quantity'] += row['total_quantity'] or Decimal('0')

    items_list = []
    for key, data in item_data.items():
        item_row = {
            'item_code': data['item_code'],
            'item_description': data['item_description'],
            'years_data': {},
        }
        for year in ALLOWED_YEARS:
            yd = data['years'].get(year, {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'total_quantity': Decimal('0')})
            total_sales = yd['total_sales']
            total_gp = yd['total_gp']
            total_quantity = yd['total_quantity']
            gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
            avg_rate = (total_sales / total_quantity) if total_quantity else Decimal('0')
            item_row['years_data'][year] = {
                'total_sales': total_sales,
                'total_gp': total_gp,
                'gp_percent': gp_percent,
                'avg_rate': avg_rate,
                'total_quantity': total_quantity,
            }
        items_list.append(item_row)

    items_list = [i for i in items_list if i['item_code'] and not _is_null_item_code(i['item_code'])]
    items_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

    year_totals = {}
    for year in ALLOWED_YEARS:
        year_totals[year] = {
            'total_sales': sum(i['years_data'][year]['total_sales'] for i in items_list),
            'total_gp': sum(i['years_data'][year]['total_gp'] for i in items_list),
            'total_quantity': sum(i['years_data'][year]['total_quantity'] for i in items_list),
        }
        yt = year_totals[year]
        yt['total_avg_rate'] = (yt['total_sales'] / yt['total_quantity']) if yt['total_quantity'] else Decimal('0')
        yt['total_gp_percent'] = (yt['total_gp'] / yt['total_sales'] * 100) if yt['total_sales'] else Decimal('0')

    totals_list = [year_totals[y] for y in ALLOWED_YEARS]
    for item in items_list:
        item['year_list'] = [item['years_data'][y] for y in ALLOWED_YEARS]

    page_size = 50
    paginator = Paginator(items_list, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('ajax') == '1'
    if is_ajax:
        try:
            table_html = render_to_string('historical_sales/_item_analysis_table.html', {
                'items': page_obj,
                'years': ALLOWED_YEARS,
                'is_admin': is_admin,
                'totals_list': totals_list,
            }, request=request)
            pagination_html = ''
            if paginator.num_pages > 1:
                pagination_html = render_to_string('historical_sales/_item_analysis_pagination.html', {'page_obj': page_obj}, request=request)
            return JsonResponse({
                'success': True,
                'table_html': table_html,
                'pagination_html': pagination_html,
                'total_count': len(items_list),
            })
        except Exception as e:
            logger.exception('Historical item analysis AJAX error')
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    context = {
        'items': page_obj,
        'page_obj': page_obj,
        'total_count': len(items_list),
        'years': ALLOWED_YEARS,
        'is_admin': is_admin,
        'salesmen': salesmen,
        'firms': firms,
        'totals_list': totals_list,
        'filters': {
            'q': search_query,
            'salesman': salesmen_filter,
            'firm': firm_filter,
            'month': month_filter,
            'start': start_str,
            'end': end_str,
        },
    }
    return render(request, 'historical_sales/item_analysis.html', context)


@login_required
def historical_customer_analysis(request):
    """Customer Analysis for 2020-2023 historical data."""
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    item_filter = request.GET.getlist('item')
    month_filter = request.GET.getlist('month')
    start_str = request.GET.get('start', '').strip()
    end_str = request.GET.get('end', '').strip()

    def parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    qs = HistoricalSalesLine.objects.all().select_related('customer', 'item')
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
    start_date = parse_date(start_str)
    end_date = parse_date(end_str)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)
    if firm_filter:
        clean_firms = [f for f in firm_filter if f.strip()]
        if clean_firms:
            firm_item_ids = set(Items.objects.filter(item_firm__in=clean_firms).values_list('pk', flat=True))
            qs = qs.filter(item_id__in=firm_item_ids)
    if item_filter:
        clean_items = [i for i in item_filter if i.strip()]
        if clean_items:
            item_ids = set(Items.objects.filter(item_code__in=clean_items).values_list('pk', flat=True))
            if item_ids:
                qs = qs.filter(item_id__in=item_ids)
            else:
                qs = qs.none()

    is_admin = request.user.is_superuser or request.user.is_staff or (
        hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin'
    )

    salesmen = list(
        HistoricalSalesLine.objects.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )
    firms = list(Items.objects.exclude(item_firm__isnull=True).exclude(item_firm='').values_list('item_firm', flat=True).distinct().order_by('item_firm'))
    items_from_lines = (
        HistoricalSalesLine.objects.exclude(item__isnull=True)
        .values_list('item__item_code', 'item__item_description')
        .distinct()
    )
    items_list = []
    seen = set()
    for code, desc in items_from_lines:
        if code and code not in seen:
            seen.add(code)
            items_list.append({'code': code, 'description': desc or ''})

    customer_data = {}
    for year in ALLOWED_YEARS:
        year_qs = qs.filter(posting_date__year=year)
        if search_query:
            search_ids = set(
                Customer.objects.filter(
                    Q(customer_code__icontains=search_query) | Q(customer_name__icontains=search_query)
                ).values_list('pk', flat=True)
            )
            if search_ids:
                year_qs = year_qs.filter(customer_id__in=search_ids)
            else:
                year_qs = year_qs.none()

        doc_agg = year_qs.values('customer', 'document_type', 'document_number').annotate(
            doc_sales=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())),
            doc_gp=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())),
        )
        customer_totals = {}
        customer_salesman = {}
        for row in doc_agg:
            cid = row['customer']
            if not cid:
                continue
            if cid not in customer_totals:
                customer_totals[cid] = {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'doc_count': 0}
            customer_totals[cid]['total_sales'] += row['doc_sales'] or Decimal('0')
            customer_totals[cid]['total_gp'] += row['doc_gp'] or Decimal('0')
            customer_totals[cid]['doc_count'] += 1

        for cid in customer_totals:
            latest = year_qs.filter(customer_id=cid).order_by('-posting_date').values('sales_employee').first()
            if latest:
                customer_salesman[cid] = latest.get('sales_employee') or ''

        cust_map = {c.pk: c for c in Customer.objects.filter(pk__in=list(customer_totals.keys()))}

        for cid, totals in customer_totals.items():
            cust = cust_map.get(cid)
            code = cust.customer_code if cust else ''
            name = cust.customer_name if cust else 'Unknown'
            if not code:
                continue
            key = code
            if key not in customer_data:
                customer_data[key] = {
                    'customer_code': code,
                    'customer_name': name,
                    'years': {},
                }
            if year not in customer_data[key]['years']:
                customer_data[key]['years'][year] = {
                    'total_sales': Decimal('0'),
                    'total_gp': Decimal('0'),
                    'doc_count': 0,
                    'salesman': '',
                }
            customer_data[key]['years'][year]['total_sales'] = totals['total_sales']
            customer_data[key]['years'][year]['total_gp'] = totals['total_gp']
            customer_data[key]['years'][year]['doc_count'] = totals['doc_count']
            customer_data[key]['years'][year]['salesman'] = customer_salesman.get(cid, '')

    customers_list = []
    for key, data in customer_data.items():
        cust_row = {
            'customer_code': data['customer_code'],
            'customer_name': data['customer_name'],
            'years_data': {},
        }
        for year in ALLOWED_YEARS:
            yd = data['years'].get(year, {'total_sales': Decimal('0'), 'total_gp': Decimal('0'), 'doc_count': 0, 'salesman': ''})
            total_sales = yd['total_sales']
            total_gp = yd['total_gp']
            gp_percent = (total_gp / total_sales * 100) if total_sales else Decimal('0')
            cust_row['years_data'][year] = {
                'total_sales': total_sales,
                'total_gp': total_gp,
                'gp_percent': gp_percent,
                'doc_count': yd['doc_count'],
                'salesman': yd.get('salesman', ''),
            }
        customers_list.append(cust_row)

    customers_list.sort(key=lambda x: sum(y['total_sales'] for y in x['years_data'].values()), reverse=True)

    year_totals = {}
    for year in ALLOWED_YEARS:
        year_totals[year] = {
            'total_sales': sum(c['years_data'][year]['total_sales'] for c in customers_list),
            'total_gp': sum(c['years_data'][year]['total_gp'] for c in customers_list),
        }
        yt = year_totals[year]
        yt['total_gp_percent'] = (yt['total_gp'] / yt['total_sales'] * 100) if yt['total_sales'] else Decimal('0')

    totals_list = [year_totals[y] for y in ALLOWED_YEARS]
    for c in customers_list:
        c['year_list'] = [c['years_data'][y] for y in ALLOWED_YEARS]

    page_size = 50
    paginator = Paginator(customers_list, page_size)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('ajax') == '1'
    if is_ajax:
        try:
            table_html = render_to_string('historical_sales/_customer_analysis_table.html', {
                'customers': page_obj,
                'years': ALLOWED_YEARS,
                'is_admin': is_admin,
                'totals_list': totals_list,
            }, request=request)
            pagination_html = ''
            if paginator.num_pages > 1:
                pagination_html = render_to_string('historical_sales/_item_analysis_pagination.html', {'page_obj': page_obj}, request=request)
            return JsonResponse({
                'success': True,
                'table_html': table_html,
                'pagination_html': pagination_html,
                'total_count': len(customers_list),
            })
        except Exception as e:
            logger.exception('Historical customer analysis AJAX error')
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    context = {
        'customers': page_obj,
        'page_obj': page_obj,
        'total_count': len(customers_list),
        'years': ALLOWED_YEARS,
        'is_admin': is_admin,
        'salesmen': salesmen,
        'firms': firms,
        'items': items_list,
        'totals_list': totals_list,
        'filters': {
            'q': search_query,
            'salesman': salesmen_filter,
            'firm': firm_filter,
            'item': item_filter,
            'month': month_filter,
            'start': start_str,
            'end': end_str,
        },
    }
    return render(request, 'historical_sales/customer_analysis.html', context)
