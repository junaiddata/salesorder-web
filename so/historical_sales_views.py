"""
Historical Sales Views (2020-2023)
Upload, Sales Analysis, Item Analysis, Customer Analysis for uploaded historical data.
"""
from django.shortcuts import render, redirect
from django.http import HttpResponse
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
import calendar
import pandas as pd
import io
import logging

from .models import HistoricalSalesLine, Customer, Items

logger = logging.getLogger(__name__)

ALLOWED_YEARS = [2020, 2021, 2022, 2023]

# Salesman categories: A.=Trading, B.=Project, R.=Retail, E.=Export, else Others
# HO = Project + Trading + Others (excludes Retail R. and Export E.)
SALESMAN_CATEGORIES = [
    ('Trading', 'Trading'),   # A.
    ('Project', 'Project'),   # B.
    ('Retail', 'Retail'),     # R.
    ('Export', 'Export'),     # E.
    ('Others', 'Others'),     # fallback
]


def _salesman_category(name):
    """Return category for salesman: Trading, Project, Retail, Export, or Others."""
    if not name or not str(name).strip():
        return 'Others'
    s = str(name).strip()
    if s.upper().startswith('A.'):
        return 'Trading'
    if s.upper().startswith('B.'):
        return 'Project'
    if s.upper().startswith('R.'):
        return 'Retail'
    if s.upper().startswith('E.'):
        return 'Export'
    return 'Others'


def _is_ho_salesman(name):
    """HO = Project + Trading + Others. Excludes Retail (R.) and Export (E.)."""
    cat = _salesman_category(name)
    return cat in ('Trading', 'Project', 'Others')


def _get_salesmen_by_category(salesmen_list):
    """Group salesmen by category. Returns dict {category: [salesmen]}."""
    by_cat = {c[0]: [] for c in SALESMAN_CATEGORIES}
    for s in salesmen_list:
        cat = _salesman_category(s)
        by_cat.setdefault(cat, []).append(s)
    return by_cat


def _get_salesmen_for_store(salesmen_list, store):
    """When store=HO, return only HO salesmen. When store=Others, return only Retail+Export."""
    if store == 'HO':
        return [s for s in salesmen_list if _is_ho_salesman(s)]
    if store == 'Others':
        return [s for s in salesmen_list if _salesman_category(s) in ('Retail', 'Export')]
    return salesmen_list


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
    # Excel serial number (e.g. 44927 or "44927.0")
    try:
        f = float(s)
        return pd.Timestamp(f).date()
    except (ValueError, TypeError):
        pass
    for fmt in ['%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%d-%m-%y', '%Y%m%d',
                '%d-%b-%Y', '%d-%b-%y', '%d %b %Y', '%b %d %Y', '%Y-%m-%d %H:%M:%S', '%m-%d-%Y', '%m/%d/%y']:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_decimal(x):
    """Parse decimal from string/number. Handles US (1,234.56) and European (1.234,56) formats."""
    if pd.isna(x) or x is None or (isinstance(x, str) and not x.strip()):
        return Decimal('0')
    try:
        s = str(x).strip()
        # European format: 1.234,56 (dot=thousands, comma=decimal)
        if ',' in s and '.' in s:
            # Both present: last one is decimal separator
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '').replace(',', '.')  # 1.234,56 -> 1234.56
            else:
                s = s.replace(',', '')  # 1,234.56 -> 1234.56
        elif ',' in s:
            # Only comma: could be thousands (1,234) or decimal (1,23)
            parts = s.split(',')
            if len(parts) == 2 and len(parts[1]) <= 2:
                s = s.replace(',', '.')  # decimal: 1,23 -> 1.23
            else:
                s = s.replace(',', '')  # thousands: 1,234 -> 1234
        else:
            s = s.replace(',', '')  # US thousands only
        return Decimal(s)
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
def historical_sales_clear_all(request):
    """Delete all Historical Sales data. Requires POST with confirm=yes."""
    if request.method != 'POST':
        return redirect('historical_sales_upload')
    if request.POST.get('confirm') != 'yes':
        messages.error(request, 'Deletion cancelled. Please confirm to delete all data.')
        return redirect('historical_sales_upload')
    count, _ = HistoricalSalesLine.objects.all().delete()
    messages.success(request, f'Deleted all {count} historical sales records. You can now upload fresh data.')
    return redirect('historical_sales_upload')


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
            'net_sales': ['net sales', 'netsales', 'amount', 'net amount', 'line total'],
            'gross_profit': ['gross profit', 'grossprofit', 'gp', 'gross margin', 'margin'],
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

        # Get column indices for itertuples (10-100x faster than iterrows)
        def _col_index(col_name):
            if col_name not in df.columns:
                return -1
            try:
                return list(df.columns).index(col_name)
            except ValueError:
                return -1

        def get_val_tuple(tup, key):
            col = rev_map.get(key)
            if col is None or col not in df.columns:
                return ''
            idx = _col_index(col)
            if idx < 0:
                return ''
            try:
                v = tup[idx]
                return '' if pd.isna(v) else str(v).strip()
            except (IndexError, TypeError):
                return ''

        # First pass: collect unique customers, items, and valid rows
        unique_customers = {}  # cust_code -> cust_name
        unique_items = {}      # item_code -> (item_desc, item_manu)
        docs_to_replace = set()
        raw_rows = []
        skipped_outside_year = 0
        skip_reasons = {'no_doc_no': 0, 'no_date': 0, 'year_outside': 0, 'no_cust': 0, 'no_item': 0}

        for tup in df.itertuples(index=False):
            doc_type_code_raw = get_val_tuple(tup, 'document_type_code') if doc_type_code_col else ''
            doc_type_raw = get_val_tuple(tup, 'document_type')
            doc_type, doc_type_code = _resolve_document_type(doc_type_code_raw, doc_type_raw)
            doc_no = _to_str(get_val_tuple(tup, 'document_number'))
            if not doc_no:
                skip_reasons['no_doc_no'] += 1
                continue
            posting_d = _parse_date(get_val_tuple(tup, 'postingdate'))
            if not posting_d:
                skip_reasons['no_date'] += 1
                continue
            if posting_d.year not in ALLOWED_YEARS:
                skipped_outside_year += 1
                skip_reasons['year_outside'] += 1
                continue
            cust_code = _to_str(get_val_tuple(tup, 'customer_code'))
            cust_name = _to_str(get_val_tuple(tup, 'customer_name'))
            if not cust_code or not cust_name:
                skip_reasons['no_cust'] += 1
                continue
            item_code = _to_str(get_val_tuple(tup, 'itemcode'))
            if not item_code:
                skip_reasons['no_item'] += 1
                continue

            docs_to_replace.add((doc_type, doc_no))
            unique_customers[cust_code] = cust_name
            item_desc = _to_str(get_val_tuple(tup, 'item_description'))
            item_manu = _to_str(get_val_tuple(tup, 'item_manufacturer'))
            if item_code not in unique_items:
                unique_items[item_code] = (item_desc, item_manu)

            raw_rows.append({
                'doc_type': doc_type,
                'doc_type_code': doc_type_code,
                'doc_no': doc_no,
                'posting_d': posting_d,
                'cust_code': cust_code,
                'cust_name': cust_name,
                'sales_emp': _to_str(get_val_tuple(tup, 'sales_employee')) or None,
                'item_code': item_code,
                'item_desc': item_desc,
                'item_manu': item_manu,
                'qty': _to_decimal(get_val_tuple(tup, 'quantity')),
                'net_sales': _to_decimal(get_val_tuple(tup, 'net_sales')),
                'gp': _to_decimal(get_val_tuple(tup, 'gross_profit')),
            })

        # Batch fetch/create customers and items (2-4 queries total instead of 2 per row)
        existing_customers = {c.customer_code: c for c in Customer.objects.filter(customer_code__in=unique_customers.keys())}
        existing_items = {i.item_code: i for i in Items.objects.filter(item_code__in=unique_items.keys())}

        new_customer_codes = [c for c in unique_customers if c not in existing_customers]
        if new_customer_codes:
            Customer.objects.bulk_create([
                Customer(customer_code=code, customer_name=unique_customers[code])
                for code in new_customer_codes
            ])
            for c in Customer.objects.filter(customer_code__in=new_customer_codes):
                existing_customers[c.customer_code] = c

        new_item_codes = [c for c in unique_items if c not in existing_items]
        if new_item_codes:
            Items.objects.bulk_create([
                Items(item_code=code, item_description=unique_items[code][0] or code, item_firm=unique_items[code][1] or '')
                for code in new_item_codes
            ])
            for i in Items.objects.filter(item_code__in=new_item_codes):
                existing_items[i.item_code] = i

        rows_to_insert = []
        for r in raw_rows:
            customer = existing_customers.get(r['cust_code'])
            item = existing_items.get(r['item_code'])
            if not customer or not item:
                continue
            rows_to_insert.append({
                'document_type': r['doc_type'],
                'document_type_code': r['doc_type_code'],
                'document_number': r['doc_no'],
                'posting_date': r['posting_d'],
                'customer': customer,
                'customer_code': r['cust_code'],
                'customer_name': r['cust_name'],
                'sales_employee': r['sales_emp'],
                'item': item,
                'item_code': r['item_code'],
                'item_description': r['item_desc'],
                'item_manufacturer': r['item_manu'],
                'quantity': r['qty'],
                'net_sales': r['net_sales'],
                'gross_profit': r['gp'],
            })

        if not rows_to_insert:
            msg = f'No valid rows found. Found {len(df)} rows in file.'
            if skipped_outside_year:
                msg += f' Skipped {skipped_outside_year} rows outside allowed years ({min(ALLOWED_YEARS)}-{max(ALLOWED_YEARS)}).'
            parts = []
            if skip_reasons['no_doc_no']:
                parts.append(f"{skip_reasons['no_doc_no']} with empty Document Number")
            if skip_reasons['no_date']:
                parts.append(f"{skip_reasons['no_date']} with unparseable date")
            if skip_reasons['year_outside']:
                parts.append(f"{skip_reasons['year_outside']} outside allowed years")
            if skip_reasons['no_cust']:
                parts.append(f"{skip_reasons['no_cust']} with empty Customer Code/Name")
            if skip_reasons['no_item']:
                parts.append(f"{skip_reasons['no_item']} with empty Item Code")
            if parts:
                msg += ' Reasons: ' + '; '.join(parts) + '.'
            return render(request, 'historical_sales/upload.html', {'error': msg})

        with transaction.atomic():
            # Bulk delete: one query instead of one per document
            if docs_to_replace:
                q = Q()
                for doc_type, doc_no in docs_to_replace:
                    q |= Q(document_type=doc_type, document_number=doc_no)
                HistoricalSalesLine.objects.filter(q).delete()
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
    category_filter = request.GET.getlist('category')
    month_filter = request.GET.getlist('month')
    store_filter = request.GET.get('store', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    period = request.GET.get('period', '').strip()
    year_filter = request.GET.get('year', 'Total').strip()

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
        if category_filter:
            clean_cats = [c for c in category_filter if c.strip()]
            if clean_cats:
                cat_conditions = Q()
                for cat in clean_cats:
                    if cat == 'Trading':
                        cat_conditions |= Q(sales_employee__istartswith='A.')
                    elif cat == 'Project':
                        cat_conditions |= Q(sales_employee__istartswith='B.')
                    elif cat == 'Retail':
                        cat_conditions |= Q(sales_employee__istartswith='R.')
                    elif cat == 'Export':
                        cat_conditions |= Q(sales_employee__istartswith='E.')
                    elif cat == 'Others':
                        cat_conditions |= ~Q(sales_employee__istartswith='A.') & ~Q(sales_employee__istartswith='B.') & ~Q(sales_employee__istartswith='R.') & ~Q(sales_employee__istartswith='E.')
                qs = qs.filter(cat_conditions)
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

    all_salesmen = list(
        lines_qs.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )
    salesmen_by_category = _get_salesmen_by_category(all_salesmen)
    # Salesmen tiles: filtered by store (HO = Project+Trading+Others; Others = Retail+Export; All = all)
    salesmen = _get_salesmen_for_store(all_salesmen, store_filter or '')
    # When HO: only Project, Trading, Others. When Others: only Retail, Export. All: show all 5.
    if store_filter == 'HO':
        categories_for_tiles = ['Project', 'Trading', 'Others']
    elif store_filter == 'Others':
        categories_for_tiles = ['Retail', 'Export']
    else:
        categories_for_tiles = [c[0] for c in SALESMAN_CATEGORIES]

    # Calendar: year/month selector (cal_year, cal_month). Default to last allowed year + current month.
    cal_year = request.GET.get('cal_year', '').strip()
    cal_month = request.GET.get('cal_month', '').strip()
    try:
        cal_year = int(cal_year) if cal_year else ALLOWED_YEARS[-1]
        cal_month = int(cal_month) if cal_month else today.month
    except (ValueError, TypeError):
        cal_year = ALLOWED_YEARS[-1]
        cal_month = today.month
    if cal_year not in ALLOWED_YEARS:
        cal_year = ALLOWED_YEARS[-1]
    if cal_month < 1 or cal_month > 12:
        cal_month = 1
    _, last_day = calendar.monthrange(cal_year, cal_month)
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    cal_lines = apply_common_filters(lines_qs)
    month_days = []
    for day_num in range(1, last_day + 1):
        day_date = date(cal_year, cal_month, day_num)
        day_qs = cal_lines.filter(posting_date=day_date)
        day_sales = day_qs.aggregate(s=Coalesce(Sum('net_sales'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
        day_gp = day_qs.aggregate(s=Coalesce(Sum('gross_profit'), Value(0, output_field=DecimalField())))['s'] or Decimal('0')
        day_gp_pct = (float(day_gp) / float(day_sales) * 100) if day_sales else Decimal('0')
        month_days.append({
            'day': day_num,
            'date': day_date,
            'formatted_date': f'{month_names[cal_month - 1]} {day_num}',
            'sales': day_sales,
            'gp': day_gp,
            'gp_pct': round(float(day_gp_pct), 1),
            'has_sales': bool(day_sales and day_sales > Decimal('0')),
        })

    context = {
        'total_sales': total_sales,
        'total_gp': total_gp,
        'top_customers': top_customers,
        'top_items': top_items,
        'salesmen': salesmen,
        'salesmen_by_category': salesmen_by_category,
        'categories_for_tiles': categories_for_tiles,
        'salesman_categories': SALESMAN_CATEGORIES,
        'is_admin': is_admin,
        'years': ALLOWED_YEARS,
        'filters': {
            'salesmen_filter': salesmen_filter,
            'category_filter': category_filter,
            'month': month_filter,
            'store': store_filter,
            'start': start,
            'end': end,
            'period': period,
            'year': year_filter,
        },
        'today': today,
        'month_days': month_days,
        'cal_year': cal_year,
        'cal_month': cal_month,
        'calendar_month_name': month_names[cal_month - 1],
        'calendar_years': ALLOWED_YEARS,
        'calendar_months': [(i, month_names[i - 1]) for i in range(1, 13)],
    }

    # AJAX: return only calendar grid HTML (no full page)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('ajax') == 'calendar':
        calendar_html = render_to_string(
            'historical_sales/_historical_calendar_grid.html',
            {'month_days': month_days, 'today': today, 'is_admin': is_admin},
            request=request
        )
        return HttpResponse(calendar_html, content_type='text/html')

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
    category_filter = request.GET.getlist('category')
    store_filter = request.GET.get('store', '').strip()
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
    if store_filter == 'HO':
        qs = qs.exclude(sales_employee__istartswith='R.').exclude(sales_employee__istartswith='E.')
    elif store_filter == 'Others':
        qs = qs.filter(Q(sales_employee__istartswith='R.') | Q(sales_employee__istartswith='E.'))
    if category_filter:
        clean_cats = [c for c in category_filter if c.strip()]
        if clean_cats:
            cat_conditions = Q()
            for cat in clean_cats:
                if cat == 'Trading':
                    cat_conditions |= Q(sales_employee__istartswith='A.')
                elif cat == 'Project':
                    cat_conditions |= Q(sales_employee__istartswith='B.')
                elif cat == 'Retail':
                    cat_conditions |= Q(sales_employee__istartswith='R.')
                elif cat == 'Export':
                    cat_conditions |= Q(sales_employee__istartswith='E.')
                elif cat == 'Others':
                    cat_conditions |= ~Q(sales_employee__istartswith='A.') & ~Q(sales_employee__istartswith='B.') & ~Q(sales_employee__istartswith='R.') & ~Q(sales_employee__istartswith='E.')
            qs = qs.filter(cat_conditions)
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

    all_salesmen = list(
        HistoricalSalesLine.objects.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )
    salesmen = _get_salesmen_for_store(all_salesmen, store_filter)
    if store_filter == 'HO':
        categories_for_tiles = ['Project', 'Trading', 'Others']
    elif store_filter == 'Others':
        categories_for_tiles = ['Retail', 'Export']
    else:
        categories_for_tiles = [c[0] for c in SALESMAN_CATEGORIES]
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
            table_html = render_to_string('historical_sales/_item_analysis_table_rows.html', {
                'items': page_obj,
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
                'page_length': len(page_obj),
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
        'categories_for_tiles': categories_for_tiles,
        'firms': firms,
        'totals_list': totals_list,
        'filters': {
            'q': search_query,
            'salesman': salesmen_filter,
            'category': category_filter,
            'store': store_filter,
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
    category_filter = request.GET.getlist('category')
    store_filter = request.GET.get('store', '').strip()
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
    if store_filter == 'HO':
        qs = qs.exclude(sales_employee__istartswith='R.').exclude(sales_employee__istartswith='E.')
    elif store_filter == 'Others':
        qs = qs.filter(Q(sales_employee__istartswith='R.') | Q(sales_employee__istartswith='E.'))
    if category_filter:
        clean_cats = [c for c in category_filter if c.strip()]
        if clean_cats:
            cat_conditions = Q()
            for cat in clean_cats:
                if cat == 'Trading':
                    cat_conditions |= Q(sales_employee__istartswith='A.')
                elif cat == 'Project':
                    cat_conditions |= Q(sales_employee__istartswith='B.')
                elif cat == 'Retail':
                    cat_conditions |= Q(sales_employee__istartswith='R.')
                elif cat == 'Export':
                    cat_conditions |= Q(sales_employee__istartswith='E.')
                elif cat == 'Others':
                    cat_conditions |= ~Q(sales_employee__istartswith='A.') & ~Q(sales_employee__istartswith='B.') & ~Q(sales_employee__istartswith='R.') & ~Q(sales_employee__istartswith='E.')
            qs = qs.filter(cat_conditions)
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

    all_salesmen = list(
        HistoricalSalesLine.objects.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )
    salesmen = _get_salesmen_for_store(all_salesmen, store_filter)
    if store_filter == 'HO':
        categories_for_tiles = ['Project', 'Trading', 'Others']
    elif store_filter == 'Others':
        categories_for_tiles = ['Retail', 'Export']
    else:
        categories_for_tiles = [c[0] for c in SALESMAN_CATEGORIES]
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
            'category': category_filter,
            'store': store_filter,
            'firm': firm_filter,
            'item': item_filter,
            'month': month_filter,
            'start': start_str,
            'end': end_str,
        },
        'categories_for_tiles': categories_for_tiles,
    }
    return render(request, 'historical_sales/customer_analysis.html', context)


def _build_historical_item_analysis_data(request):
    """Build filtered item analysis data for PDF export. Returns (items_list, totals_list, is_admin)."""
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    category_filter = request.GET.getlist('category')
    store_filter = request.GET.get('store', '').strip()
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
    if store_filter == 'HO':
        qs = qs.exclude(sales_employee__istartswith='R.').exclude(sales_employee__istartswith='E.')
    elif store_filter == 'Others':
        qs = qs.filter(Q(sales_employee__istartswith='R.') | Q(sales_employee__istartswith='E.'))
    if category_filter:
        clean_cats = [c for c in category_filter if c.strip()]
        if clean_cats:
            cat_conditions = Q()
            for cat in clean_cats:
                if cat == 'Trading':
                    cat_conditions |= Q(sales_employee__istartswith='A.')
                elif cat == 'Project':
                    cat_conditions |= Q(sales_employee__istartswith='B.')
                elif cat == 'Retail':
                    cat_conditions |= Q(sales_employee__istartswith='R.')
                elif cat == 'Export':
                    cat_conditions |= Q(sales_employee__istartswith='E.')
                elif cat == 'Others':
                    cat_conditions |= ~Q(sales_employee__istartswith='A.') & ~Q(sales_employee__istartswith='B.') & ~Q(sales_employee__istartswith='R.') & ~Q(sales_employee__istartswith='E.')
            qs = qs.filter(cat_conditions)
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

    return items_list, totals_list, is_admin


def _build_historical_customer_analysis_data(request):
    """Build filtered customer analysis data for PDF export. Returns (customers_list, totals_list, is_admin)."""
    search_query = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    category_filter = request.GET.getlist('category')
    store_filter = request.GET.get('store', '').strip()
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
    if store_filter == 'HO':
        qs = qs.exclude(sales_employee__istartswith='R.').exclude(sales_employee__istartswith='E.')
    elif store_filter == 'Others':
        qs = qs.filter(Q(sales_employee__istartswith='R.') | Q(sales_employee__istartswith='E.'))
    if category_filter:
        clean_cats = [c for c in category_filter if c.strip()]
        if clean_cats:
            cat_conditions = Q()
            for cat in clean_cats:
                if cat == 'Trading':
                    cat_conditions |= Q(sales_employee__istartswith='A.')
                elif cat == 'Project':
                    cat_conditions |= Q(sales_employee__istartswith='B.')
                elif cat == 'Retail':
                    cat_conditions |= Q(sales_employee__istartswith='R.')
                elif cat == 'Export':
                    cat_conditions |= Q(sales_employee__istartswith='E.')
                elif cat == 'Others':
                    cat_conditions |= ~Q(sales_employee__istartswith='A.') & ~Q(sales_employee__istartswith='B.') & ~Q(sales_employee__istartswith='R.') & ~Q(sales_employee__istartswith='E.')
            qs = qs.filter(cat_conditions)
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

    return customers_list, totals_list, is_admin


@login_required
def export_historical_item_analysis_pdf(request):
    """Export Historical Item Analysis to PDF with all current filters applied."""
    from io import BytesIO
    from django.http import HttpResponse
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    items_list, totals_list, is_admin = _build_historical_item_analysis_data(request)

    response = HttpResponse(content_type='application/pdf')
    filename = f"Historical_Item_Analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.5 * inch
    )

    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#2C3E50'),
        spaceAfter=12,
        alignment=TA_CENTER
    )
    elements.append(Paragraph("Historical Item Analysis (2020-2023)", title_style))
    elements.append(Spacer(1, 0.15 * inch))

    filter_info = []
    store_filter = request.GET.get('store', '').strip()
    category_filter = request.GET.getlist('category')
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    month_filter = request.GET.getlist('month')
    start_str = request.GET.get('start', '').strip()
    end_str = request.GET.get('end', '').strip()
    search_query = request.GET.get('q', '').strip()
    if store_filter:
        filter_info.append(f"Store: {store_filter}")
    if category_filter:
        filter_info.append(f"Category: {', '.join(category_filter[:3])}{'...' if len(category_filter) > 3 else ''}")
    if salesmen_filter:
        filter_info.append(f"Salesmen: {', '.join(salesmen_filter[:2])}{'...' if len(salesmen_filter) > 2 else ''}")
    if firm_filter:
        filter_info.append(f"Firms: {', '.join(firm_filter[:2])}{'...' if len(firm_filter) > 2 else ''}")
    if month_filter:
        filter_info.append(f"Months: {', '.join(month_filter)}")
    if start_str or end_str:
        filter_info.append(f"Date: {start_str or 'Start'} to {end_str or 'End'}")
    if search_query:
        filter_info.append(f"Search: {search_query}")

    if filter_info:
        filter_style = ParagraphStyle(
            'FilterStyle',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor('#666666'),
            alignment=TA_LEFT
        )
        elements.append(Paragraph("Filters: " + " | ".join(filter_info), filter_style))
        elements.append(Spacer(1, 0.15 * inch))

    header_style = ParagraphStyle(
        'HeaderStyle',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.whitesmoke,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    wrap_style = ParagraphStyle(
        'WrapStyle',
        parent=styles['Normal'],
        fontSize=6.5,
        leading=8,
        alignment=TA_LEFT
    )

    header_row = [
        Paragraph('Item Code', header_style),
        Paragraph('Description', header_style),
    ]
    for year in ALLOWED_YEARS:
        if is_admin:
            header_row.extend([
                Paragraph(f'{year} Sales', header_style),
                Paragraph(f'{year} GP', header_style),
                Paragraph(f'{year} GP%', header_style),
                Paragraph(f'{year} Qty', header_style),
                Paragraph(f'{year} Avg Rate', header_style),
            ])
        else:
            header_row.extend([
                Paragraph(f'{year} Sales', header_style),
                Paragraph(f'{year} Qty', header_style),
                Paragraph(f'{year} Avg Rate', header_style),
            ])

    table_data = [header_row]

    for item in items_list:
        row = [
            Paragraph(str(item['item_code'])[:30], wrap_style),
            Paragraph((item['item_description'] or '')[:50], wrap_style),
        ]
        for year_data in item['year_list']:
            sales = year_data.get('total_sales', Decimal('0'))
            gp = year_data.get('total_gp', Decimal('0'))
            qty = year_data.get('total_quantity', Decimal('0'))
            avg_rate = year_data.get('avg_rate', Decimal('0'))
            row.append(Paragraph(f"{sales:,.2f}", wrap_style))
            if is_admin:
                row.append(Paragraph(f"{gp:,.2f}", wrap_style))
                gp_pct = year_data.get('gp_percent', Decimal('0'))
                row.append(Paragraph(f"{gp_pct:.2f}%", wrap_style))
            row.append(Paragraph(f"{qty:,.0f}", wrap_style))
            row.append(Paragraph(f"{avg_rate:,.2f}", wrap_style))
        table_data.append(row)

    totals_row = [
        Paragraph('TOTAL', wrap_style),
        Paragraph('', wrap_style),
    ]
    for totals in totals_list:
        totals_row.append(Paragraph(f"{totals.get('total_sales', 0):,.2f}", wrap_style))
        if is_admin:
            totals_row.append(Paragraph(f"{totals.get('total_gp', 0):,.2f}", wrap_style))
            totals_row.append(Paragraph(f"{totals.get('total_gp_percent', 0):.2f}%", wrap_style))
        totals_row.append(Paragraph('-', wrap_style))
        totals_row.append(Paragraph('-', wrap_style))
    table_data.append(totals_row)

    col_widths = [0.7 * inch, 1.8 * inch]
    for _ in ALLOWED_YEARS:
        col_widths.append(0.7 * inch)
        if is_admin:
            col_widths.append(0.6 * inch)
            col_widths.append(0.5 * inch)
        col_widths.append(0.5 * inch)
        col_widths.append(0.6 * inch)

    total_width = sum(col_widths)
    available_width = 10.69 * inch
    if total_width > available_width:
        scale_factor = available_width / total_width
        col_widths = [w * scale_factor for w in col_widths]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (1, -1), 'LEFT'),
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f8f9fa')]),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#eff6ff')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
    ])
    table.setStyle(table_style)
    elements.append(table)

    doc.build(elements)
    response.write(buffer.getvalue())
    buffer.close()
    return response


@login_required
def export_historical_customer_analysis_pdf(request):
    """Export Historical Customer Analysis to PDF with all current filters applied."""
    from io import BytesIO
    from django.http import HttpResponse
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    customers_list, totals_list, is_admin = _build_historical_customer_analysis_data(request)

    response = HttpResponse(content_type='application/pdf')
    filename = f"Historical_Customer_Analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.5 * inch
    )

    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#2C3E50'),
        spaceAfter=12,
        alignment=TA_CENTER
    )
    elements.append(Paragraph("Historical Customer Analysis (2020-2023)", title_style))
    elements.append(Spacer(1, 0.15 * inch))

    filter_info = []
    store_filter = request.GET.get('store', '').strip()
    category_filter = request.GET.getlist('category')
    salesmen_filter = request.GET.getlist('salesman')
    firm_filter = request.GET.getlist('firm')
    item_filter = request.GET.getlist('item')
    month_filter = request.GET.getlist('month')
    start_str = request.GET.get('start', '').strip()
    end_str = request.GET.get('end', '').strip()
    search_query = request.GET.get('q', '').strip()
    if store_filter:
        filter_info.append(f"Store: {store_filter}")
    if category_filter:
        filter_info.append(f"Category: {', '.join(category_filter[:3])}{'...' if len(category_filter) > 3 else ''}")
    if salesmen_filter:
        filter_info.append(f"Salesmen: {', '.join(salesmen_filter[:2])}{'...' if len(salesmen_filter) > 2 else ''}")
    if firm_filter:
        filter_info.append(f"Firms: {', '.join(firm_filter[:2])}{'...' if len(firm_filter) > 2 else ''}")
    if item_filter:
        filter_info.append(f"Items: {', '.join(item_filter[:2])}{'...' if len(item_filter) > 2 else ''}")
    if month_filter:
        filter_info.append(f"Months: {', '.join(month_filter)}")
    if start_str or end_str:
        filter_info.append(f"Date: {start_str or 'Start'} to {end_str or 'End'}")
    if search_query:
        filter_info.append(f"Search: {search_query}")

    if filter_info:
        filter_style = ParagraphStyle(
            'FilterStyle',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor('#666666'),
            alignment=TA_LEFT
        )
        elements.append(Paragraph("Filters: " + " | ".join(filter_info), filter_style))
        elements.append(Spacer(1, 0.15 * inch))

    header_style = ParagraphStyle(
        'HeaderStyle',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.whitesmoke,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    wrap_style = ParagraphStyle(
        'WrapStyle',
        parent=styles['Normal'],
        fontSize=6.5,
        leading=8,
        alignment=TA_LEFT
    )

    header_row = [
        Paragraph('Customer', header_style),
        Paragraph('Salesman', header_style),
    ]
    for year in ALLOWED_YEARS:
        header_row.append(Paragraph(f'{year} Sales', header_style))
        if is_admin:
            header_row.extend([
                Paragraph(f'{year} GP', header_style),
                Paragraph(f'{year} GP%', header_style),
            ])

    table_data = [header_row]

    for customer in customers_list:
        first_salesman = customer['year_list'][0].get('salesman', '') if customer['year_list'] else ''
        row = [
            Paragraph(f"{customer['customer_name'] or 'Unknown'} ({customer['customer_code']})"[:60], wrap_style),
            Paragraph(str(first_salesman)[:25], wrap_style),
        ]
        for year_data in customer['year_list']:
            sales = year_data.get('total_sales', Decimal('0'))
            gp = year_data.get('total_gp', Decimal('0'))
            gp_pct = year_data.get('gp_percent', Decimal('0'))
            row.append(Paragraph(f"{sales:,.2f}", wrap_style))
            if is_admin:
                row.append(Paragraph(f"{gp:,.2f}", wrap_style))
                row.append(Paragraph(f"{gp_pct:.2f}%", wrap_style))
        table_data.append(row)

    totals_row = [
        Paragraph('TOTAL', wrap_style),
        Paragraph('', wrap_style),
    ]
    for totals in totals_list:
        totals_row.append(Paragraph(f"{totals.get('total_sales', 0):,.2f}", wrap_style))
        if is_admin:
            totals_row.append(Paragraph(f"{totals.get('total_gp', 0):,.2f}", wrap_style))
            totals_row.append(Paragraph(f"{totals.get('total_gp_percent', 0):.2f}%", wrap_style))
    table_data.append(totals_row)

    col_widths = [2.2 * inch, 1.2 * inch]
    for _ in ALLOWED_YEARS:
        col_widths.append(0.85 * inch)
        if is_admin:
            col_widths.append(0.7 * inch)
            col_widths.append(0.55 * inch)

    total_width = sum(col_widths)
    available_width = 10.69 * inch
    if total_width > available_width:
        scale_factor = available_width / total_width
        col_widths = [w * scale_factor for w in col_widths]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (1, -1), 'LEFT'),
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f8f9fa')]),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#eff6ff')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
    ])
    table.setStyle(table_style)
    elements.append(table)

    doc.build(elements)
    response.write(buffer.getvalue())
    buffer.close()
    return response
